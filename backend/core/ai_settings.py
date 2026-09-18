"""
AI Settings resolver and runtime profile mapping for Nebula.
Provides effective configuration resolving DB settings or environment defaults.
"""
from __future__ import annotations

import hashlib
import logging
import os
import urllib.parse
from dataclasses import dataclass

from backend.core.credential_store import CredentialStore
from backend.db.repository import Repository
from contracts.provider import (
    ModelProfile,
    ProviderProfile,
    StructuredOutputMode,
    STTProfile,
    STTProtocol,
)
from contracts.settings import (
    AiSettingsResponse,
    AnalysisModelSettings,
    AuthMode,
    ProviderPreset,
    ReasoningPolicy,
    TextAnalysisSettings,
    TranscriptionSettings,
)

logger = logging.getLogger("nebula.ai_settings")


@dataclass(frozen=True)
class ResolvedCredentials:
    api_key: str | None
    source: str


@dataclass(frozen=True)
class RuntimeAiConfiguration:
    revision: int
    stt_provider: ProviderProfile
    stt_profile: STTProfile
    stt_credentials: ResolvedCredentials
    llm_provider: ProviderProfile
    llm_primary: ModelProfile
    llm_fallbacks: tuple[ModelProfile, ...]
    llm_credentials: ResolvedCredentials
    language: str


def get_preset_transcription_settings(preset: ProviderPreset) -> TranscriptionSettings:
    """Returns factory default TranscriptionSettings for a given preset."""
    if preset == ProviderPreset.PLUSVIBE:
        return TranscriptionSettings(
            preset=ProviderPreset.PLUSVIBE,
            provider_id="plusvibe",
            provider_name="PlusVibe API (RF)",
            endpoint_url="https://plusvibeapi.ru/v1/audio/transcriptions",
            model_id="whisper-large-v3-turbo",
            auth_mode=AuthMode.BEARER,
            language="ru",
            timeout_seconds=60.0,
            max_concurrency=2,
        )
    if preset == ProviderPreset.CUSTOM:
        return TranscriptionSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom STT",
            endpoint_url="http://127.0.0.1:8000/v1/audio/transcriptions",
            model_id="whisper-1",
            auth_mode=AuthMode.BEARER,
            language="ru",
            timeout_seconds=60.0,
            max_concurrency=2,
        )
    # Default to RouterAI
    return TranscriptionSettings(
        preset=ProviderPreset.ROUTERAI,
        provider_id="routerai",
        provider_name="RouterAI (RF)",
        endpoint_url="https://routerai.ru/api/v1/audio/transcriptions",
        model_id="openai/gpt-transcribe",
        auth_mode=AuthMode.BEARER,
        language="ru",
        timeout_seconds=60.0,
        max_concurrency=2,
    )


def get_preset_text_analysis_settings(preset: ProviderPreset) -> TextAnalysisSettings:
    """Returns factory default TextAnalysisSettings for a given preset."""
    if preset == ProviderPreset.PLUSVIBE:
        return TextAnalysisSettings(
            preset=ProviderPreset.PLUSVIBE,
            provider_id="plusvibe",
            provider_name="PlusVibe API (RF)",
            base_url="https://plusvibeapi.ru/v1",
            auth_mode=AuthMode.BEARER,
            timeout_seconds=60.0,
            max_concurrency=10,
            primary_model=AnalysisModelSettings(
                model_id="deepseek/deepseek-v4-flash-0731",
                structured_output_mode=StructuredOutputMode.JSON_OBJECT,
                reasoning_policy=ReasoningPolicy.PROVIDER_DEFAULT,
                supports_temperature=True,
                temperature=0.0,
                max_output_tokens=4096,
                context_window_tokens=65536,
            ),
            fallback_models=[
                AnalysisModelSettings(
                    model_id="qwen/qwen3.7-plus",
                    structured_output_mode=StructuredOutputMode.JSON_OBJECT,
                    reasoning_policy=ReasoningPolicy.PROVIDER_DEFAULT,
                    supports_temperature=True,
                    temperature=0.0,
                    max_output_tokens=4096,
                    context_window_tokens=32768,
                ),
                AnalysisModelSettings(
                    model_id="google/gemini-3.8-flash",
                    structured_output_mode=StructuredOutputMode.JSON_OBJECT,
                    reasoning_policy=ReasoningPolicy.PROVIDER_DEFAULT,
                    supports_temperature=True,
                    temperature=0.0,
                    max_output_tokens=4096,
                    context_window_tokens=1048576,
                ),
            ],
        )
    if preset == ProviderPreset.CUSTOM:
        return TextAnalysisSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom OpenAI-Compatible",
            base_url="http://127.0.0.1:8000/v1",
            auth_mode=AuthMode.BEARER,
            timeout_seconds=60.0,
            max_concurrency=10,
            primary_model=AnalysisModelSettings(
                model_id="custom-model",
                structured_output_mode=StructuredOutputMode.JSON_OBJECT,
                reasoning_policy=ReasoningPolicy.PROVIDER_DEFAULT,
                supports_temperature=True,
                temperature=0.0,
                max_output_tokens=4096,
                context_window_tokens=32768,
            ),
            fallback_models=[],
        )
    # Default to RouterAI
    return TextAnalysisSettings(
        preset=ProviderPreset.ROUTERAI,
        provider_id="routerai",
        provider_name="RouterAI (RF)",
        base_url="https://routerai.ru/api/v1",
        auth_mode=AuthMode.BEARER,
        timeout_seconds=60.0,
        max_concurrency=10,
        primary_model=AnalysisModelSettings(
            model_id="qwen/qwen3.7-flash",
            structured_output_mode=StructuredOutputMode.JSON_OBJECT,
            reasoning_policy=ReasoningPolicy.DISABLE_ENABLE_THINKING,
            supports_temperature=True,
            temperature=0.0,
            max_output_tokens=4096,
            context_window_tokens=1000000,
        ),
        fallback_models=[
            AnalysisModelSettings(
                model_id="qwen/qwen3.7-plus",
                structured_output_mode=StructuredOutputMode.JSON_OBJECT,
                reasoning_policy=ReasoningPolicy.DISABLE_ENABLE_THINKING,
                supports_temperature=True,
                temperature=0.0,
                max_output_tokens=4096,
                context_window_tokens=1000000,
            ),
            AnalysisModelSettings(
                model_id="deepseek/deepseek-v4-flash-0731",
                structured_output_mode=StructuredOutputMode.JSON_OBJECT,
                reasoning_policy=ReasoningPolicy.DISABLE_REASONING_EFFORT,
                supports_temperature=True,
                temperature=0.0,
                max_output_tokens=4096,
                context_window_tokens=65536,
            ),
        ],
    )


def get_environment_transcription_settings() -> TranscriptionSettings:
    """Builds effective TranscriptionSettings from process environment variables."""
    endpoint = os.getenv("NEBULA_STT_ENDPOINT")
    model = os.getenv("NEBULA_STT_MODEL")
    language = os.getenv("NEBULA_STT_LANGUAGE", "ru").strip() or "ru"

    preset_env = os.getenv("NEBULA_LLM_PROVIDER", "routerai").lower().strip()
    if preset_env == "plusvibe":
        default_settings = get_preset_transcription_settings(ProviderPreset.PLUSVIBE)
    else:
        default_settings = get_preset_transcription_settings(ProviderPreset.ROUTERAI)

    endpoint_url = endpoint.strip() if endpoint and endpoint.strip() else default_settings.endpoint_url
    model_id = model.strip() if model and model.strip() else default_settings.model_id

    # If endpoint or provider is customized beyond preset, determine preset
    preset = default_settings.preset
    provider_id = default_settings.provider_id
    provider_name = default_settings.provider_name

    auth_mode = AuthMode.BEARER
    return TranscriptionSettings(
        preset=preset,
        provider_id=provider_id,
        provider_name=provider_name,
        endpoint_url=endpoint_url,
        model_id=model_id,
        auth_mode=auth_mode,
        language=language,
        timeout_seconds=default_settings.timeout_seconds,
        max_concurrency=default_settings.max_concurrency,
    )


def get_environment_text_analysis_settings() -> TextAnalysisSettings:
    """Builds effective TextAnalysisSettings from process environment variables."""
    provider_name = os.getenv("NEBULA_LLM_PROVIDER", "routerai").lower().strip()
    base_url_env = os.getenv("NEBULA_LLM_BASE_URL")
    model_env = os.getenv("NEBULA_LLM_MODEL")

    if provider_name == "plusvibe":
        default_settings = get_preset_text_analysis_settings(ProviderPreset.PLUSVIBE)
    else:
        default_settings = get_preset_text_analysis_settings(ProviderPreset.ROUTERAI)

    base_url = base_url_env.strip() if base_url_env and base_url_env.strip() else default_settings.base_url

    primary = default_settings.primary_model
    if model_env and model_env.strip():
        # User specified a custom model ID in env: keep exact string and case!
        primary = AnalysisModelSettings(
            model_id=model_env.strip(),
            structured_output_mode=default_settings.primary_model.structured_output_mode,
            reasoning_policy=default_settings.primary_model.reasoning_policy,
            supports_temperature=default_settings.primary_model.supports_temperature,
            temperature=default_settings.primary_model.temperature,
            max_output_tokens=default_settings.primary_model.max_output_tokens,
            context_window_tokens=default_settings.primary_model.context_window_tokens,
        )

    return TextAnalysisSettings(
        preset=default_settings.preset,
        provider_id=default_settings.provider_id,
        provider_name=default_settings.provider_name,
        base_url=base_url,
        auth_mode=default_settings.auth_mode,
        timeout_seconds=default_settings.timeout_seconds,
        max_concurrency=default_settings.max_concurrency,
        primary_model=primary,
        fallback_models=default_settings.fallback_models,
    )


def to_model_profile(provider_id: str, m_settings: AnalysisModelSettings) -> ModelProfile:
    """Converts AnalysisModelSettings to domain ModelProfile with deterministic ID and reasoning payload."""
    thinking_payload = None
    supports_reasoning = False
    if m_settings.reasoning_policy == ReasoningPolicy.DISABLE_ENABLE_THINKING:
        thinking_payload = {"enable_thinking": False}
        supports_reasoning = True
    elif m_settings.reasoning_policy == ReasoningPolicy.DISABLE_REASONING_EFFORT:
        thinking_payload = {"reasoning_effort": "none"}
        supports_reasoning = True
    elif m_settings.reasoning_policy == ReasoningPolicy.PROVIDER_DEFAULT:
        thinking_payload = None
        supports_reasoning = False

    h = hashlib.sha256(
        f"{provider_id}:{m_settings.model_id}:{m_settings.structured_output_mode.value}:{m_settings.reasoning_policy.value}".encode()
    ).hexdigest()[:12]
    clean_model_id = m_settings.model_id.replace("/", "_").replace(":", "_")
    profile_id = f"{provider_id}-{clean_model_id}-{h}"

    return ModelProfile(
        id=profile_id,
        provider_id=provider_id,
        upstream_model_id=m_settings.model_id,
        structured_output_mode=m_settings.structured_output_mode,
        context_window_tokens=m_settings.context_window_tokens,
        max_output_tokens=m_settings.max_output_tokens,
        supports_reasoning=supports_reasoning,
        supports_temperature=m_settings.supports_temperature,
        default_temperature=m_settings.temperature,
        thinking_disable_payload=thinking_payload,
    )


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlparse(url)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port


def _credential_preset_for_url(
    preset: ProviderPreset,
    configured_url: str,
    *,
    transcription: bool,
) -> ProviderPreset:
    """Only use a legacy provider key for that preset's canonical network origin."""
    if preset == ProviderPreset.CUSTOM:
        return preset
    defaults = (
        get_preset_transcription_settings(preset)
        if transcription
        else get_preset_text_analysis_settings(preset)
    )
    canonical_url = defaults.endpoint_url if transcription else defaults.base_url
    return preset if _origin(configured_url) == _origin(canonical_url) else ProviderPreset.CUSTOM


def resolve_ai_settings(
    repo: Repository,
    cred_store: CredentialStore,
) -> tuple[AiSettingsResponse, RuntimeAiConfiguration]:
    """
    Resolves the effective AI settings response (for UI/API) and the runtime configuration
    (for workers and adapters).
    """
    db_row = repo.get_ai_settings()
    if db_row is not None:
        revision = db_row["revision"]
        source = "database"
        transcription = TranscriptionSettings.model_validate(db_row["transcription_config"])
        text_analysis = TextAnalysisSettings.model_validate(db_row["text_analysis_config"])
    else:
        revision = 0
        source = "environment"
        transcription = get_environment_transcription_settings()
        text_analysis = get_environment_text_analysis_settings()

    # Resolve credentials
    stt_key, stt_status = cred_store.resolve_credential(
        "stt",
        revision,
        _credential_preset_for_url(
            transcription.preset,
            transcription.endpoint_url,
            transcription=True,
        ),
    )
    llm_key, llm_status = cred_store.resolve_credential(
        "llm",
        revision,
        _credential_preset_for_url(
            text_analysis.preset,
            text_analysis.base_url,
            transcription=False,
        ),
    )

    blocker = repo.get_ai_settings_update_blocker()
    can_update = blocker is None

    response = AiSettingsResponse(
        revision=revision,
        source=source,
        transcription=transcription,
        text_analysis=text_analysis,
        stt_credentials=stt_status,
        llm_credentials=llm_status,
        can_update=can_update,
        update_blocker=blocker,
    )

    # Build runtime profiles
    stt_path = urllib.parse.urlparse(transcription.endpoint_url).path
    stt_provider = ProviderProfile(
        id=transcription.provider_id,
        name=transcription.provider_name,
        base_url=transcription.endpoint_url,
        api_key_env="NEBULA_STT_API_KEY",
        allowed_endpoints=[stt_path] if stt_path else ["/v1/audio/transcriptions"],
        timeout_seconds=transcription.timeout_seconds,
        max_concurrency=transcription.max_concurrency,
    )

    stt_profile = STTProfile(
        id=f"{transcription.provider_id}-{transcription.model_id.replace('/', '_')}",
        name=f"{transcription.provider_name} {transcription.model_id}",
        endpoint_url=transcription.endpoint_url,
        protocol=STTProtocol.BATCH,
        model_id=transcription.model_id,
        supported_languages=[transcription.language, "en"] if transcription.language != "en" else ["en"],
        timeout_seconds=transcription.timeout_seconds,
    )

    llm_provider = ProviderProfile(
        id=text_analysis.provider_id,
        name=text_analysis.provider_name,
        base_url=text_analysis.base_url,
        api_key_env="NEBULA_LLM_API_KEY",
        allowed_endpoints=[
            "/chat/completions",
            "/v1/chat/completions",
            "/api/v1/chat/completions",
            "/models",
            "/v1/models",
            "/api/v1/models",
        ],
        timeout_seconds=text_analysis.timeout_seconds,
        max_concurrency=text_analysis.max_concurrency,
    )

    llm_primary = to_model_profile(text_analysis.provider_id, text_analysis.primary_model)
    llm_fallbacks = tuple(
        to_model_profile(text_analysis.provider_id, fb) for fb in text_analysis.fallback_models
    )

    runtime_config = RuntimeAiConfiguration(
        revision=revision,
        stt_provider=stt_provider,
        stt_profile=stt_profile,
        stt_credentials=ResolvedCredentials(api_key=stt_key, source=stt_status.source),
        llm_provider=llm_provider,
        llm_primary=llm_primary,
        llm_fallbacks=llm_fallbacks,
        llm_credentials=ResolvedCredentials(api_key=llm_key, source=llm_status.source),
        language=transcription.language,
    )

    return response, runtime_config
