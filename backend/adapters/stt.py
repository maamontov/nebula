"""
Universal OpenAI-compatible Speech-to-Text (STT) Adapter.
Implements POST /v1/audio/transcriptions adhering to docs/implementation-plan.md Section 4.5.
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import BinaryIO

import httpx

from contracts.provider import STTProfile
from contracts.settings import AuthMode

logger = logging.getLogger(__name__)


def _safe_upstream_text(text: str, api_key: str, limit: int = 1000) -> str:
    """Redacts the active credential from untrusted upstream error content."""
    safe = text.replace(api_key, "[REDACTED]") if api_key else text
    return safe[:limit]


class STTAdapterError(Exception):
    """Base exception for STT failures."""


class STTAuthenticationError(STTAdapterError):
    """401/403 auth failure."""


class STTRateLimitError(STTAdapterError):
    """429 rate limit exceeded."""
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class STTTransientError(STTAdapterError):
    """Temporary 5xx or network failure."""


@dataclass(frozen=True)
class STTTranscriptionResult:
    text: str
    model_id: str
    latency_seconds: float
    raw_response: dict


class OpenAICompatibleSTTAdapter:
    def __init__(
        self,
        profile: STTProfile,
        api_key_env: str = "ROUTERAI_API_KEY",
        api_key: str | None = None,
        auth_mode: AuthMode = AuthMode.BEARER,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.profile = profile
        self.api_key_env = api_key_env
        self._api_key = api_key
        self.auth_mode = auth_mode
        self._external_client = http_client

    def _get_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        key = os.getenv("NEBULA_STT_API_KEY", "")
        if not key:
            key = os.getenv(self.api_key_env, "")
        if not key:
            try:
                from dotenv import load_dotenv
                load_dotenv()
                key = os.getenv("NEBULA_STT_API_KEY", "") or os.getenv(self.api_key_env, "")
            except Exception:
                pass
        return key

    async def transcribe_audio(
        self,
        audio_data: bytes | BinaryIO,
        filename: str = "chunk.wav",
        content_type: str = "audio/wav",
        language: str | None = "ru",
        max_retries: int = 3,
        initial_backoff: float = 1.0,
        timeout_seconds: float | None = None,
    ) -> STTTranscriptionResult:
        """
        Transcribes an audio chunk or file via the standard multipart/form-data endpoint.

        `timeout_seconds` bounds a single HTTP attempt and overrides the client/profile default
        for this call only. Callers that run under an outer job deadline must pass a per-attempt
        timeout smaller than that deadline, otherwise the outer cancellation fires first and this
        method's retry loop never runs (the request is cancelled, not timed out).

        `timeout_seconds` ограничивает одну HTTP-попытку и переопределяет таймаут клиента только
        для этого вызова. Если снаружи есть дедлайн задания, таймаут попытки обязан быть меньше
        дедлайна, иначе внешняя отмена сработает раньше и ретраи этого метода не выполнятся.
        """
        headers = {}
        api_key = ""
        if self.auth_mode == AuthMode.BEARER:
            api_key = self._get_api_key()
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            else:
                raise STTAuthenticationError(
                    f"Missing STT API key: provider '{self.profile.name}' requires Bearer authentication."
                )

        request_timeout = timeout_seconds if timeout_seconds is not None else self.profile.timeout_seconds
        client = self._external_client or httpx.AsyncClient(timeout=request_timeout)
        close_client = self._external_client is None

        attempt = 0
        backoff = initial_backoff
        t0 = time.perf_counter()

        try:
            while attempt < max_retries:
                attempt += 1
                try:
                    # Prepare multipart form
                    if isinstance(audio_data, bytes):
                        file_payload = (filename, audio_data, content_type)
                    else:
                        file_payload = (filename, audio_data.read(), content_type)

                    files = {"file": file_payload}
                    data = {"model": self.profile.model_id}
                    if language:
                        data["language"] = language

                    resp = await client.post(
                        self.profile.endpoint_url,
                        files=files,
                        data=data,
                        headers=headers,
                        timeout=request_timeout,
                    )

                    if resp.status_code in (401, 403):
                        raise STTAuthenticationError(
                            f"STT Authentication error {resp.status_code}: "
                            f"{_safe_upstream_text(resp.text, api_key)}"
                        )

                    if resp.status_code == 429:
                        retry_h = resp.headers.get("Retry-After")
                        retry_sec = float(retry_h) if retry_h and retry_h.isdigit() else 10.0
                        raise STTRateLimitError(
                            f"STT Rate limit exceeded (retry_after={retry_sec}s): "
                            f"{_safe_upstream_text(resp.text, api_key)}",
                            retry_after=retry_sec,
                        )

                    if resp.status_code == 502 and "media_submit_failed" in resp.text:
                        logger.info(
                            "STT upstream returned media_submit_failed on silent/unsegmentable audio chunk, treating as empty speech."
                        )
                        return STTTranscriptionResult(
                            text="",
                            model_id=self.profile.model_id,
                            latency_seconds=time.perf_counter() - t0,
                            raw_response={"text": "", "note": "media_submit_failed"},
                        )

                    if resp.status_code >= 500:
                        if attempt >= max_retries:
                            raise STTTransientError(
                                f"STT Server error {resp.status_code}: "
                                f"{_safe_upstream_text(resp.text, api_key)}"
                            )
                        logger.warning("STT upstream error %d, retrying (%d/%d)", resp.status_code, attempt, max_retries)
                        await asyncio.sleep(backoff)
                        backoff *= 2.0
                        continue

                    if resp.status_code != 200:
                        raise STTAdapterError(
                            f"Unexpected STT response status {resp.status_code}: "
                            f"{_safe_upstream_text(resp.text, api_key)}"
                        )

                    latency = time.perf_counter() - t0
                    res_json = resp.json()
                    text = res_json.get("text", "").strip()

                    return STTTranscriptionResult(
                        text=text,
                        model_id=self.profile.model_id,
                        latency_seconds=latency,
                        raw_response=res_json,
                    )

                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as net_err:
                    if attempt >= max_retries:
                        raise STTTransientError(f"STT network error: {net_err}")
                    logger.warning("STT network error on attempt %d: %s", attempt, net_err)
                    await asyncio.sleep(backoff)
                    backoff *= 2.0

            raise STTTransientError(f"Exceeded max retries ({max_retries})")

        finally:
            if close_client:
                await client.aclose()
