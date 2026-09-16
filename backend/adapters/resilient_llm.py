"""
Resilient LLM Adapter with Automatic Model Fallback.
Adheres to docs/implementation-plan.md Section 4 and Section 8:
- Primary: google/gemini-3.8-flash (via PlusVibe API).
- Secondary Fallback: qwen/qwen3.7-plus.
- Tertiary Fallback: deepseek/deepseek-v4-flash-0731.
- No GPT models used.
- Logs and records model transitions transparently.
"""

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

logger = logging.getLogger("nebula.resilient_llm")


class ResilientLLMAdapter:
    def __init__(
        self,
        force_primary_fail: bool = False,
        http_client: httpx.AsyncClient | None = None,
        provider: ProviderProfile | None = None,
        primary_model: ModelProfile | None = None,
        fallback_model_1: ModelProfile | None = None,
        fallback_model_2: ModelProfile | None = None,
    ):
        self.provider = provider or get_default_provider()
        is_router = (self.provider.id == "routerai")
        self.primary_model = primary_model or (
            get_routerai_qwen_flash_model() if is_router else get_plusvibe_gemini_model()
        )
        self.fallback_model_1 = fallback_model_1 or (
            get_routerai_qwen_plus_model() if is_router else get_plusvibe_qwen_model()
        )
        self.fallback_model_2 = fallback_model_2 or (
            get_routerai_deepseek_model() if is_router else get_plusvibe_deepseek_model()
        )

        self.primary_adapter = OpenAICompatibleAdapter(
            self.provider, self.primary_model, http_client=http_client
        )
        self.fallback_adapter_1 = OpenAICompatibleAdapter(
            self.provider, self.fallback_model_1, http_client=http_client
        )
        self.fallback_adapter_2 = OpenAICompatibleAdapter(
            self.provider, self.fallback_model_2, http_client=http_client
        )

        self.force_primary_fail = force_primary_fail
        self.last_fallback_event: dict[str, Any] | None = None

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

        # Try Primary
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
                logger.warning(
                    "Primary model %s failed: %s. Initiating fallback...",
                    self.primary_model.upstream_model_id,
                    e,
                )
                self.last_fallback_event = {
                    "primary_model": self.primary_model.upstream_model_id,
                    "target_model": self.fallback_model_1.upstream_model_id,
                    "reason": str(e),
                }
        else:
            logger.warning(
                "Primary model %s simulated outage (force_primary_fail=True). Initiating fallback...",
                self.primary_model.upstream_model_id,
            )
            self.last_fallback_event = {
                "primary_model": self.primary_model.upstream_model_id,
                "target_model": self.fallback_model_1.upstream_model_id,
                "reason": "Simulated primary outage (force_primary_fail=True)",
            }

        # Try Fallback 1 (Qwen)
        try:
            logger.info("Executing via Fallback 1: %s", self.fallback_model_1.upstream_model_id)
            res = await self.fallback_adapter_1.execute_request(
                messages=messages,
                json_schema=json_schema,
                schema_name=schema_name,
            )
            return res, self.fallback_model_1.upstream_model_id
        except LLMAuthenticationError:
            raise
        except Exception as e:
            logger.warning(
                "Fallback 1 model %s failed: %s. Initiating Fallback 2...",
                self.fallback_model_1.upstream_model_id,
                e,
            )
            self.last_fallback_event = {
                "primary_model": self.fallback_model_1.upstream_model_id,
                "target_model": self.fallback_model_2.upstream_model_id,
                "reason": str(e),
            }

        # Try Fallback 2 (DeepSeek)
        logger.info("Executing via Fallback 2: %s", self.fallback_model_2.upstream_model_id)
        res = await self.fallback_adapter_2.execute_request(
            messages=messages,
            json_schema=json_schema,
            schema_name=schema_name,
        )
        return res, self.fallback_model_2.upstream_model_id
