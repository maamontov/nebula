"""
Preconfigured profiles for Nebula AI and STT providers.
Adheres strictly to docs/implementation-plan.md Section 4:
No GPT models, explicit upstream model IDs, verified structured output modes.
"""
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


def get_default_task_routes() -> list[TaskRoute]:
    """Default routes mapping tasks to verified models."""
    return [
        TaskRoute(
            task=TaskType.QUESTION_EXTRACTION,
            model_profile_id="plusvibe-deepseek-v4",
            prompt_version="1.0.0",
            schema_version="1.0.0",
            request_budget_tokens=2048,
        ),
        TaskRoute(
            task=TaskType.ANSWER_ASSESSMENT,
            model_profile_id="plusvibe-deepseek-v4",
            prompt_version="1.0.0",
            schema_version="1.0.0",
            request_budget_tokens=4096,
        ),
        TaskRoute(
            task=TaskType.FINAL_SUMMARY,
            model_profile_id="plusvibe-deepseek-v4",
            prompt_version="1.0.0",
            schema_version="1.0.0",
            request_budget_tokens=4096,
        ),
    ]
