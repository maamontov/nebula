"""
Universal OpenAI-compatible LLM Adapter.
Adheres strictly to docs/implementation-plan.md Section 4:
- Independent of proprietary OpenAI services and GPT naming.
- Flexible structured output: json_schema, json_object, or prompt-enforced.
- Explicit retry policy with exponential backoff and Retry-After handling.
- Strips reasoning traces (<think>...</think>) from public evaluation output.
- Never converts technical failures into candidate zero scores.
"""
import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx

from contracts.provider import (
    ModelProfile,
    ProviderProfile,
    StructuredOutputMode,
)

logger = logging.getLogger(__name__)


class LLMAdapterError(Exception):
    """Base error for LLM adapter failures."""


class LLMAuthenticationError(LLMAdapterError):
    """Authentication or authorization failure (HTTP 401/403). Non-retryable."""


class LLMContextLengthExceededError(LLMAdapterError):
    """Context window exceeded upstream. Non-retryable without truncation."""


class LLMRateLimitError(LLMAdapterError):
    """Rate limit encountered (HTTP 429)."""
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMTransientError(LLMAdapterError):
    """Temporary server failure or connection timeout (HTTP 5xx, timeouts)."""


class LLMInvalidResponseFormatError(LLMAdapterError):
    """Model output failed JSON parsing and validation after repair attempts."""


def extract_clean_json_content(raw_text: str) -> str:
    """
    Strips reasoning blocks (<think>...</think>) and markdown code fences (```json ... ```).
    """
    # Remove reasoning blocks
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL)
    # Remove markdown code block fences if present
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1)
    return cleaned.strip()


class OpenAICompatibleAdapter:
    def __init__(
        self,
        provider: ProviderProfile,
        model: ModelProfile,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.provider = provider
        self.model = model
        self._external_client = http_client

    def _get_api_key(self) -> str:
        api_key = os.getenv(self.provider.api_key_env, "")
        return api_key

    def _build_url(self, endpoint: str) -> str:
        base = self.provider.base_url.rstrip("/")
        path = endpoint.lstrip("/")
        return f"{base}/{path}"

    def build_payload(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "output_schema",
    ) -> dict[str, Any]:
        """
        Builds the request payload matching the model profile capabilities.
        Does NOT inject unverified parameters (seed, etc.).
        """
        payload: dict[str, Any] = {
            "model": self.model.upstream_model_id,
            "messages": list(messages),
        }

        if self.model.supports_temperature:
            payload["temperature"] = self.model.default_temperature

        if self.model.max_output_tokens:
            payload["max_tokens"] = self.model.max_output_tokens

        # Handle structured output
        if json_schema is not None:
            if self.model.structured_output_mode == StructuredOutputMode.JSON_SCHEMA:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": json_schema,
                    },
                }
            elif self.model.structured_output_mode == StructuredOutputMode.JSON_OBJECT:
                payload["response_format"] = {"type": "json_object"}
                # In JSON_OBJECT mode, models require schema description in the prompt
                instruction = (
                    f"\nYou must respond STRICTLY with a valid JSON object conforming to this schema:\n"
                    f"{json.dumps(json_schema, ensure_ascii=False)}"
                )
                if payload["messages"] and payload["messages"][0]["role"] == "system":
                    payload["messages"][0] = {
                        "role": "system",
                        "content": payload["messages"][0]["content"] + "\n" + instruction,
                    }
                else:
                    payload["messages"].insert(0, {"role": "system", "content": instruction})
            elif self.model.structured_output_mode == StructuredOutputMode.PROMPT_INSTRUCTION:
                # Append instruction to system message
                instruction = (
                    f"\nYou must respond STRICTLY with a valid JSON object conforming to this schema:\n"
                    f"{json.dumps(json_schema, ensure_ascii=False)}"
                )
                if payload["messages"] and payload["messages"][0]["role"] == "system":
                    payload["messages"][0] = {
                        "role": "system",
                        "content": payload["messages"][0]["content"] + "\n" + instruction,
                    }
                else:
                    payload["messages"].insert(0, {"role": "system", "content": instruction})

        return payload

    async def execute_request(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "output_schema",
        max_retries: int = 3,
        initial_backoff: float = 1.0,
    ) -> dict[str, Any]:
        """
        Executes request with retries, backoff, and JSON response verification.
        Returns parsed JSON dict if schema requested, or dict with 'content'.
        """
        api_key = self._get_api_key()
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = self._build_url("/chat/completions")
        payload = self.build_payload(messages, json_schema=json_schema, schema_name=schema_name)

        client = self._external_client or httpx.AsyncClient(timeout=self.provider.timeout_seconds)
        close_client = self._external_client is None

        attempt = 0
        backoff = initial_backoff

        try:
            while attempt < max_retries:
                attempt += 1
                try:
                    resp = await client.post(url, json=payload, headers=headers)

                    if resp.status_code in (401, 403):
                        raise LLMAuthenticationError(
                            f"Authentication error {resp.status_code}: {resp.text}"
                        )

                    if resp.status_code == 429:
                        retry_header = resp.headers.get("Retry-After")
                        retry_after = float(retry_header) if retry_header and retry_header.isdigit() else backoff
                        if attempt >= max_retries:
                            raise LLMRateLimitError(
                                f"Rate limit exceeded after {attempt} attempts: {resp.text}",
                                retry_after=retry_after,
                            )
                        logger.warning("Rate limit hit, sleeping for %.2fs", retry_after)
                        await asyncio.sleep(retry_after)
                        backoff *= 2.0
                        continue

                    if resp.status_code >= 500:
                        if attempt >= max_retries:
                            raise LLMTransientError(f"Server error {resp.status_code}: {resp.text}")
                        logger.warning("Upstream server error %d, retry %d/%d", resp.status_code, attempt, max_retries)
                        await asyncio.sleep(backoff)
                        backoff *= 2.0
                        continue

                    if resp.status_code != 200:
                        err_body = resp.text
                        if "context_length_exceeded" in err_body.lower() or "maximum context length" in err_body.lower():
                            raise LLMContextLengthExceededError(f"Context length exceeded: {err_body}")
                        raise LLMAdapterError(f"Unexpected response status {resp.status_code}: {err_body}")

                    data = resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        raise LLMTransientError(f"Empty choices in response: {data}")

                    raw_content = choices[0].get("message", {}).get("content", "")
                    if not raw_content:
                        raise LLMTransientError("Received empty content from upstream model")

                    cleaned = extract_clean_json_content(raw_content)

                    # If structured output was requested, parse JSON
                    if json_schema is not None:
                        try:
                            parsed = json.loads(cleaned)
                            return {
                                "data": parsed,
                                "raw_content": raw_content,
                                "usage": data.get("usage", {}),
                                "model": data.get("model", self.model.upstream_model_id),
                            }
                        except json.JSONDecodeError as json_err:
                            logger.warning("Failed to decode JSON on attempt %d: %s", attempt, json_err)
                            if attempt < max_retries:
                                # Attempt one-time repair instruction in conversation
                                payload["messages"].append({"role": "assistant", "content": raw_content})
                                payload["messages"].append({
                                    "role": "user",
                                    "content": f"The response was not valid JSON ({json_err}). Fix and output valid JSON ONLY."
                                })
                                continue
                            raise LLMInvalidResponseFormatError(
                                f"Failed to obtain valid JSON from model after {attempt} attempts: {raw_content}"
                            )

                    return {
                        "content": cleaned,
                        "raw_content": raw_content,
                        "usage": data.get("usage", {}),
                        "model": data.get("model", self.model.upstream_model_id),
                    }

                except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as net_err:
                    if attempt >= max_retries:
                        raise LLMTransientError(f"Network error after {attempt} attempts: {net_err}")
                    logger.warning("Network failure %s on attempt %d/%d, retrying", net_err, attempt, max_retries)
                    await asyncio.sleep(backoff)
                    backoff *= 2.0

            raise LLMTransientError(f"Exceeded max retries ({max_retries})")

        finally:
            if close_client:
                await client.aclose()
