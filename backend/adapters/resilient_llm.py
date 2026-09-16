"""
Resilient LLM Adapter with Automatic Model Fallback.
Adheres to docs/implementation-plan.md Section 4, Section 8, and Section 9:
- Primary model with configurable fallback models (0 to 2 models).
- Uses exact vendor model capabilities for each model profile.
- Authentication errors fail fast without attempting fallback on the same provider.
- Logs and records model transitions transparently.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from backend.adapters.llm import LLMAuthenticationError, OpenAICompatibleAdapter
from backend.core.profiles import (
    get_default_provider,
    get_plusvibe_deepseek_model,
    get_plusvibe_gemini_model,
    get_plusvibe_qwen_model,
    get_routerai_deepseek_model,
    get_routerai_qwen_flash_model,
    get_routerai_qwen_plus_model,
)
from contracts.provider import ModelProfile, ProviderProfile
from contracts.settings import AuthMode

logger = logging.getLogger("nebula.resilient_llm")


class ResilientLLMAdapter:
    def __init__(
        self,
        force_primary_fail: bool = False,
        http_client: httpx.AsyncClient | None = None,
        provider: ProviderProfile | None = None,
        primary_model: ModelProfile | None = None,
        fallback_models: list[ModelProfile] | tuple[ModelProfile, ...] | None = None,
        fallback_model_1: ModelProfile | None = None,
        fallback_model_2: ModelProfile | None = None,
        api_key: str | None = None,
        auth_mode: AuthMode = AuthMode.BEARER,
    ):
        self.provider = provider or get_default_provider()
        self.api_key = api_key
        self.auth_mode = auth_mode
        self._external_client = http_client

        is_router = self.provider.id == "routerai"
        self.primary_model = primary_model or (
            get_routerai_qwen_flash_model() if is_router else get_plusvibe_gemini_model()
        )

        if fallback_models is not None:
            self.fallback_models = list(fallback_models)
        else:
            fb: list[ModelProfile] = []
            if fallback_model_1 is not None or fallback_model_2 is not None:
                if fallback_model_1 is not None:
                    fb.append(fallback_model_1)
                if fallback_model_2 is not None:
                    fb.append(fallback_model_2)
            else:
                fb1 = get_routerai_qwen_plus_model() if is_router else get_plusvibe_qwen_model()
                fb2 = get_routerai_deepseek_model() if is_router else get_plusvibe_deepseek_model()
                fb = [fb1, fb2]
            self.fallback_models = fb

        self.primary_adapter = OpenAICompatibleAdapter(
            self.provider,
            self.primary_model,
            api_key=self.api_key,
            auth_mode=self.auth_mode,
            http_client=http_client,
        )

        self.fallback_adapters: list[tuple[ModelProfile, OpenAICompatibleAdapter]] = [
            (
                m,
                OpenAICompatibleAdapter(
                    self.provider,
                    m,
                    api_key=self.api_key,
                    auth_mode=self.auth_mode,
                    http_client=http_client,
                ),
            )
            for m in self.fallback_models
        ]

        # For backwards compatibility with tests inspecting attributes
        self.fallback_model_1 = self.fallback_models[0] if len(self.fallback_models) > 0 else None
        self.fallback_model_2 = self.fallback_models[1] if len(self.fallback_models) > 1 else None

        self.force_primary_fail = force_primary_fail
        self.last_fallback_event: dict[str, Any] | None = None

    @property
    def fallback_adapter_1(self) -> OpenAICompatibleAdapter | None:
        return self.fallback_adapters[0][1] if len(self.fallback_adapters) > 0 else None

    @fallback_adapter_1.setter
    def fallback_adapter_1(self, adapter: OpenAICompatibleAdapter) -> None:
        if len(self.fallback_adapters) > 0:
            self.fallback_adapters[0] = (self.fallback_adapters[0][0], adapter)
        elif len(self.fallback_models) > 0:
            self.fallback_adapters.append((self.fallback_models[0], adapter))

    @property
    def fallback_adapter_2(self) -> OpenAICompatibleAdapter | None:
        return self.fallback_adapters[1][1] if len(self.fallback_adapters) > 1 else None

    @fallback_adapter_2.setter
    def fallback_adapter_2(self, adapter: OpenAICompatibleAdapter) -> None:
        if len(self.fallback_adapters) > 1:
            self.fallback_adapters[1] = (self.fallback_adapters[1][0], adapter)
        elif len(self.fallback_models) > 1:
            self.fallback_adapters.append((self.fallback_models[1], adapter))

    async def execute_request(
        self,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """
        Executes request with automatic fallback.
        Resets last_fallback_event on each call and fails fast without fallback on LLMAuthenticationError.
        Returns: (response_data_dict, actual_model_id_used)
        """
        self.last_fallback_event = None

        if not self.force_primary_fail:
            try:
                res = await self.primary_adapter.execute_request(
                    messages=messages,
                    json_schema=json_schema,
                    schema_name=schema_name,
                )
                return res, self.primary_model.upstream_model_id
            except LLMAuthenticationError:
                # Shared auth failure cannot be resolved by fallback on same provider
                raise
            except Exception as e:
                prev_model_id = self.primary_model.upstream_model_id
                logger.warning(
                    "Primary model %s failed: %s. Initiating fallback...",
                    prev_model_id,
                    e,
                )
                if not self.fallback_adapters:
                    raise
                self.last_fallback_event = {
                    "primary_model": prev_model_id,
                    "target_model": self.fallback_adapters[0][0].upstream_model_id,
                    "reason": str(e),
                }
                last_error = e
        else:
            prev_model_id = self.primary_model.upstream_model_id
            logger.warning(
                "Primary model %s simulated outage (force_primary_fail=True). Initiating fallback...",
                prev_model_id,
            )
            if not self.fallback_adapters:
                raise RuntimeError("force_primary_fail=True but no fallback models configured")
            self.last_fallback_event = {
                "primary_model": prev_model_id,
                "target_model": self.fallback_adapters[0][0].upstream_model_id,
                "reason": "Simulated primary outage (force_primary_fail=True)",
            }
            last_error = RuntimeError("Simulated primary outage (force_primary_fail=True)")

        # Try fallbacks in order
        for idx, (fb_model, fb_adapter) in enumerate(self.fallback_adapters):
            try:
                logger.info("Executing via Fallback %d: %s", idx + 1, fb_model.upstream_model_id)
                res = await fb_adapter.execute_request(
                    messages=messages,
                    json_schema=json_schema,
                    schema_name=schema_name,
                )
                return res, fb_model.upstream_model_id
            except LLMAuthenticationError:
                raise
            except Exception as e:
                logger.warning(
                    "Fallback %d model %s failed: %s",
                    idx + 1,
                    fb_model.upstream_model_id,
                    e,
                )
                last_error = e
                if idx + 1 < len(self.fallback_adapters):
                    next_model = self.fallback_adapters[idx + 1][0]
                    self.last_fallback_event = {
                        "primary_model": fb_model.upstream_model_id,
                        "target_model": next_model.upstream_model_id,
                        "reason": str(e),
                    }

        raise last_error
