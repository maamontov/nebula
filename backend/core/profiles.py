"""
Preconfigured profiles for Nebula AI and STT providers.
Adheres strictly to docs/implementation-plan.md Section 4:
No GPT models, explicit upstream model IDs, verified structured output modes.
"""
import os

from contracts.provider import (
    ModelProfile,
    ProviderProfile,
    StructuredOutputMode,
    STTProfile,
    STTProtocol,
    TaskRoute,
    TaskType,
)


def get_plusvibe_provider() -> ProviderProfile:
    """OpenAI-compatible provider profile for PlusVibe API."""
    return ProviderProfile(
        id="plusvibe",
        name="PlusVibe API (RF)",
        base_url="https://plusvibeapi.ru/v1",
        api_key_env="PLUSVIBE_API_KEY",
        allowed_endpoints=[
            "/v1/chat/completions",
            "/v1/audio/transcriptions",
            "/v1/models",
        ],
        timeout_seconds=60.0,
        max_concurrency=10,
    )


def get_plusvibe_deepseek_model() -> ModelProfile:
    """DeepSeek non-GPT model on PlusVibe for assessment and summary."""
    return ModelProfile(
        id="plusvibe-deepseek-v4",
        provider_id="plusvibe",
        upstream_model_id="deepseek/deepseek-v4-flash-0731",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        context_window_tokens=65536,
        max_output_tokens=4096,
        supports_reasoning=True,
        supports_temperature=True,
        default_temperature=0.0,
    )


def get_plusvibe_qwen_model() -> ModelProfile:
    """Qwen non-GPT model on PlusVibe."""
    return ModelProfile(
        id="plusvibe-qwen3.7",
        provider_id="plusvibe",
        upstream_model_id="qwen/qwen3.7-plus",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        context_window_tokens=32768,
        max_output_tokens=4096,
        supports_reasoning=False,
        supports_temperature=True,
        default_temperature=0.0,
    )


def get_plusvibe_gemini_model() -> ModelProfile:
    """Google Gemini 3.8 Flash non-GPT model on PlusVibe."""
    return ModelProfile(
        id="plusvibe-gemini-3.8-flash",
        provider_id="plusvibe",
        upstream_model_id="google/gemini-3.8-flash",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        context_window_tokens=1048576,
        max_output_tokens=4096,
        supports_reasoning=True,
        supports_temperature=True,
        default_temperature=0.0,
    )


def get_plusvibe_glm_model() -> ModelProfile:
    """GLM-5 non-GPT model on PlusVibe."""
    return ModelProfile(
        id="plusvibe-glm-5",
        provider_id="plusvibe",
        upstream_model_id="z-ai/glm-5.3-flash",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        context_window_tokens=65536,
        max_output_tokens=4096,
        supports_reasoning=True,
        supports_temperature=True,
        default_temperature=0.0,
    )


def get_plusvibe_whisper_stt() -> STTProfile:
    """OpenAI-compatible Whisper STT on PlusVibe."""
    return STTProfile(
        id="plusvibe-whisper-turbo",
        name="PlusVibe Whisper Large v3 Turbo",
        endpoint_url="https://plusvibeapi.ru/v1/audio/transcriptions",
        protocol=STTProtocol.BATCH,
        model_id="whisper-large-v3-turbo",
        supported_languages=["ru", "en"],
        supported_audio_formats=["wav", "mp3", "m4a", "ogg"],
        provides_timestamps=False,
        provides_partials=False,
        timeout_seconds=60.0,
    )


def get_routerai_provider() -> ProviderProfile:
    """OpenAI-compatible provider profile for RouterAI API."""
    base_url = os.getenv("NEBULA_LLM_BASE_URL", "https://routerai.ru/api/v1")
    return ProviderProfile(
        id="routerai",
        name="RouterAI (RF)",
        base_url=base_url,
        api_key_env="ROUTERAI_API_KEY",
        allowed_endpoints=[
            "/api/v1/chat/completions",
            "/api/v1/audio/transcriptions",
            "/api/v1/models",
            "/v1/chat/completions",
            "/v1/audio/transcriptions",
        ],
        timeout_seconds=60.0,
        max_concurrency=10,
    )


def get_routerai_qwen_flash_model() -> ModelProfile:
    """Qwen 3.7 Flash model on RouterAI for text analysis and assessment."""
    return ModelProfile(
        id="routerai-qwen3.7-flash",
        provider_id="routerai",
        upstream_model_id="qwen/qwen3.7-flash",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        context_window_tokens=1000000,
        max_output_tokens=4096,
        supports_reasoning=True,
        # Verified 2026-09-15 on RouterAI: reasoning_tokens 2604 -> 0, assessment JSON stayed
        # valid (3/3 criteria, 5/5 verbatim evidence quotes), ~5x faster per question.
        thinking_disable_payload={"enable_thinking": False},
        supports_temperature=True,
        default_temperature=0.0,
    )


def get_routerai_qwen_plus_model() -> ModelProfile:
    """Qwen 3.7 Plus model on RouterAI as secondary fallback."""
    return ModelProfile(
        id="routerai-qwen3.7-plus",
        provider_id="routerai",
        upstream_model_id="qwen/qwen3.7-plus",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        context_window_tokens=1000000,
        max_output_tokens=4096,
        # Measured 2026-09-15: this model does emit reasoning tokens by default (2619), so the
        # previous supports_reasoning=False was inaccurate metadata.
        supports_reasoning=True,
        # Verified 2026-09-15 on RouterAI: reasoning_tokens 2619 -> 0 with the same switch as flash.
        thinking_disable_payload={"enable_thinking": False},
        supports_temperature=True,
        default_temperature=0.0,
    )


def get_routerai_deepseek_model() -> ModelProfile:
    """DeepSeek v4 Flash model on RouterAI as tertiary fallback."""
    return ModelProfile(
        id="routerai-deepseek-v4",
        provider_id="routerai",
        upstream_model_id="deepseek/deepseek-v4-flash-0731",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        context_window_tokens=65536,
        max_output_tokens=4096,
        supports_reasoning=True,
        # Verified 2026-09-15: this model IGNORES {"enable_thinking": false} (reasoning_tokens
        # stayed at 1061) and needs the [OI]-style effort switch instead. With reasoning_effort
        # 'none' it emitted 0 reasoning tokens and a valid assessment JSON (3/3 criteria).
        # This is exactly why the capability is a dict and not a boolean flag.
        thinking_disable_payload={"reasoning_effort": "none"},
        supports_temperature=True,
        default_temperature=0.0,
    )


def get_routerai_qwen_asr_stt(model_id: str | None = None) -> STTProfile:
    """
    OpenAI-compatible Qwen3 ASR STT on RouterAI.

    `model_id` overrides the exact upstream model when the operator configures another
    Qwen3 ASR variant. It defaults to the profile's own model rather than reading the
    environment, so this factory is deterministic in tests.
    """
    endpoint = os.getenv("NEBULA_STT_ENDPOINT", "https://routerai.ru/api/v1/audio/transcriptions")
    return STTProfile(
        id="routerai-qwen3-asr-1.7b",
        name="RouterAI Qwen3 ASR 1.7B",
        endpoint_url=endpoint,
        protocol=STTProtocol.BATCH,
        model_id=model_id or "qwen/qwen3-asr-1.7b",
        supported_languages=["ru", "en"],
        supported_audio_formats=["wav", "mp3", "m4a", "ogg"],
        provides_timestamps=False,
        provides_partials=False,
        timeout_seconds=60.0,
    )


def get_routerai_gpt_transcribe_stt(model_id: str | None = None) -> STTProfile:
    """
    GPT Transcribe STT on RouterAI.

    Selected over the cheaper Qwen3 ASR profile because measured transcription of technical
    Russian speech is materially more accurate: Qwen3 ASR mangled domain terms (latin
    characters inside Russian words, "postgres" rendered as a garbled phonetic string), and
    those strings become candidate evidence. GPT Transcribe also returned empty text on room
    noise where Qwen3 ASR invented a word.

    Both profiles were measured in this environment against the same synthetic speech: latency
    was comparable (GPT Transcribe ~1.3-1.8s vs Qwen3 ASR ~0.6-1.5s per live turn), so the
    choice is about transcription quality, not speed. Cost is ~10x higher
    (~0.49 RUB/min vs ~0.05 RUB/min).

    Профиль выбран вместо более дешёвого Qwen3 ASR из-за точности на технической русской речи:
    искажённые термины попадают в evidence оценки, а на шуме Qwen3 ASR выдумывал слова.
    Латентность сопоставима, поэтому выбор сделан по качеству, а не по скорости.
    """
    endpoint = os.getenv("NEBULA_STT_ENDPOINT", "https://routerai.ru/api/v1/audio/transcriptions")
    return STTProfile(
        id="routerai-gpt-transcribe",
        name="RouterAI GPT Transcribe",
        endpoint_url=endpoint,
        protocol=STTProtocol.BATCH,
        model_id=model_id or "openai/gpt-transcribe",
        supported_languages=["ru", "en"],
        supported_audio_formats=["wav", "mp3", "m4a", "ogg"],
        provides_timestamps=False,
        provides_partials=False,
        timeout_seconds=60.0,
    )


def get_default_provider() -> ProviderProfile:
    """Resolve active provider based on NEBULA_LLM_PROVIDER environment variable."""
    provider_name = os.getenv("NEBULA_LLM_PROVIDER", "routerai").lower()
    if provider_name == "plusvibe":
        return get_plusvibe_provider()
    return get_routerai_provider()


def get_default_llm_model() -> ModelProfile:
    """Resolve active LLM model based on NEBULA_LLM_MODEL environment variable."""
    provider_name = os.getenv("NEBULA_LLM_PROVIDER", "routerai").lower()
    model_name = os.getenv("NEBULA_LLM_MODEL", "qwen/qwen3.7-flash").lower()
    if provider_name == "plusvibe":
        if "deepseek" in model_name:
            return get_plusvibe_deepseek_model()
        if "qwen" in model_name:
            return get_plusvibe_qwen_model()
        return get_plusvibe_gemini_model()

    # Default: RouterAI
    if "qwen3.7-flash" in model_name or "flash" in model_name:
        return get_routerai_qwen_flash_model()
    if "qwen3.7-plus" in model_name or "plus" in model_name:
        return get_routerai_qwen_plus_model()
    if "deepseek" in model_name:
        return get_routerai_deepseek_model()
    return get_routerai_qwen_flash_model()


def get_default_stt_profile() -> STTProfile:
    """
    Resolve the active STT profile from NEBULA_STT_MODEL or NEBULA_LLM_PROVIDER.

    Unknown model names fall back to the Qwen3 ASR profile, which passes the configured
    NEBULA_STT_MODEL through as the exact upstream model id rather than guessing.
    """
    provider_name = os.getenv("NEBULA_LLM_PROVIDER", "routerai").lower()
    configured_model = os.getenv("NEBULA_STT_MODEL", "openai/gpt-transcribe")
    model_name = configured_model.lower()
    if provider_name == "plusvibe" or "whisper" in model_name:
        return get_plusvibe_whisper_stt()
    if "gpt-transcribe" in model_name:
        return get_routerai_gpt_transcribe_stt(configured_model)
    return get_routerai_qwen_asr_stt(configured_model)


def get_default_task_routes() -> list[TaskRoute]:
    """Default routes mapping tasks to verified models."""
    primary_model = get_default_llm_model()
    return [
        TaskRoute(
            task=TaskType.QUESTION_EXTRACTION,
            model_profile_id=primary_model.id,
            prompt_version="1.0.0",
            schema_version="1.0.0",
            request_budget_tokens=2048,
        ),
        TaskRoute(
            task=TaskType.ANSWER_ASSESSMENT,
            model_profile_id=primary_model.id,
            prompt_version="1.0.0",
            schema_version="1.0.0",
            request_budget_tokens=4096,
        ),
        TaskRoute(
            task=TaskType.FINAL_SUMMARY,
            model_profile_id=primary_model.id,
            prompt_version="1.0.0",
            schema_version="1.0.0",
            request_budget_tokens=4096,
        ),
    ]
