import pytest
from pydantic import SecretStr, ValidationError

from contracts.settings import (
    AnalysisModelSettings,
    ApiKeyAction,
    ApiKeyUpdate,
    AuthMode,
    ProviderPreset,
    ReasoningPolicy,
    TestAiSettingsRequest,
    TestTarget,
    TextAnalysisSettings,
    TranscriptionSettings,
)


def test_enum_values():
    assert [p.value for p in ProviderPreset] == ["routerai", "plusvibe", "custom"]
    assert [a.value for a in AuthMode] == ["bearer", "none"]
    assert [r.value for r in ReasoningPolicy] == [
        "provider_default",
        "disable_enable_thinking",
        "disable_reasoning_effort",
    ]
    assert [k.value for k in ApiKeyAction] == ["preserve", "replace", "clear"]


def test_transcription_settings_valid():
    settings = TranscriptionSettings(
        preset=ProviderPreset.ROUTERAI,
        provider_id="routerai",
        provider_name="RouterAI",
        endpoint_url="https://routerai.ru/api/v1/audio/transcriptions",
        model_id="openai/gpt-transcribe",
        auth_mode=AuthMode.BEARER,
        language="ru",
        timeout_seconds=60.0,
        max_concurrency=2,
    )
    assert settings.model_id == "openai/gpt-transcribe"
    assert settings.preset == ProviderPreset.ROUTERAI


def test_transcription_settings_bounds():
    # Timeout bounds: 1.0 to 300.0
    with pytest.raises(ValidationError):
        TranscriptionSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            endpoint_url="https://example.com/v1/audio",
            model_id="whisper-1",
            timeout_seconds=0.5,
        )

    with pytest.raises(ValidationError):
        TranscriptionSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            endpoint_url="https://example.com/v1/audio",
            model_id="whisper-1",
            timeout_seconds=301.0,
        )

    # Concurrency bounds: 1 to 20
    with pytest.raises(ValidationError):
        TranscriptionSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            endpoint_url="https://example.com/v1/audio",
            model_id="whisper-1",
            max_concurrency=0,
        )

    with pytest.raises(ValidationError):
        TranscriptionSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            endpoint_url="https://example.com/v1/audio",
            model_id="whisper-1",
            max_concurrency=21,
        )


def test_analysis_model_settings_bounds():
    # Temperature 0.0 to 2.0
    with pytest.raises(ValidationError):
        AnalysisModelSettings(model_id="m", temperature=-0.1)

    with pytest.raises(ValidationError):
        AnalysisModelSettings(model_id="m", temperature=2.1)

    # Output tokens 256 to 65536
    with pytest.raises(ValidationError):
        AnalysisModelSettings(model_id="m", max_output_tokens=100)

    with pytest.raises(ValidationError):
        AnalysisModelSettings(model_id="m", max_output_tokens=70000)

    # Context window >= 2048
    with pytest.raises(ValidationError):
        AnalysisModelSettings(model_id="m", context_window_tokens=1024)


def test_analysis_settings_max_two_fallbacks():
    primary = AnalysisModelSettings(model_id="primary-model")
    fb1 = AnalysisModelSettings(model_id="fallback-1")
    fb2 = AnalysisModelSettings(model_id="fallback-2")
    fb3 = AnalysisModelSettings(model_id="fallback-3")

    # 0, 1, 2 fallbacks are valid
    valid_settings = TextAnalysisSettings(
        preset=ProviderPreset.CUSTOM,
        provider_id="custom",
        provider_name="Custom",
        base_url="https://api.openai.com/v1",
        primary_model=primary,
        fallback_models=[fb1, fb2],
    )
    assert len(valid_settings.fallback_models) == 2

    # 3 fallbacks must fail validation
    with pytest.raises(ValidationError):
        TextAnalysisSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            base_url="https://api.openai.com/v1",
            primary_model=primary,
            fallback_models=[fb1, fb2, fb3],
        )


def test_model_id_case_and_exact_preservation():
    exact_id = "Qwen/Qwen2.5-72B-Instruct"
    model = AnalysisModelSettings(model_id=exact_id)
    assert model.model_id == exact_id
    assert model.model_id != exact_id.lower()


def test_url_validation_rejections():
    # Credentials in URL rejected
    with pytest.raises(ValidationError, match="credentials"):
        TextAnalysisSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            base_url="https://user:pass@api.openai.com/v1",
            primary_model=AnalysisModelSettings(model_id="m"),
        )

    # Fragment in URL rejected
    with pytest.raises(ValidationError, match="fragment"):
        TextAnalysisSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            base_url="https://api.openai.com/v1#section",
            primary_model=AnalysisModelSettings(model_id="m"),
        )

    # Base URL ending with /chat/completions rejected
    with pytest.raises(ValidationError, match="/chat/completions"):
        TextAnalysisSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            base_url="https://api.openai.com/v1/chat/completions",
            primary_model=AnalysisModelSettings(model_id="m"),
        )

    # STT endpoint without path rejected
    with pytest.raises(ValidationError, match="path"):
        TranscriptionSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            endpoint_url="https://api.openai.com",
            model_id="whisper-1",
        )


def test_bearer_over_remote_http_forbidden():
    # Plain HTTP to remote host with Bearer is forbidden
    with pytest.raises(ValidationError, match="remote plain HTTP"):
        TextAnalysisSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            base_url="http://remote-server.com/v1",
            auth_mode=AuthMode.BEARER,
            primary_model=AnalysisModelSettings(model_id="m"),
        )

    with pytest.raises(ValidationError, match="remote plain HTTP"):
        TranscriptionSettings(
            preset=ProviderPreset.CUSTOM,
            provider_id="custom",
            provider_name="Custom",
            endpoint_url="http://remote-server.com/v1/audio/transcriptions",
            auth_mode=AuthMode.BEARER,
            model_id="whisper-1",
        )

    # Plain HTTP to localhost/127.0.0.1 with Bearer is permitted
    local_settings = TextAnalysisSettings(
        preset=ProviderPreset.CUSTOM,
        provider_id="custom",
        provider_name="Custom",
        base_url="http://127.0.0.1:8000/v1",
        auth_mode=AuthMode.BEARER,
        primary_model=AnalysisModelSettings(model_id="m"),
    )
    assert local_settings.base_url == "http://127.0.0.1:8000/v1"

    local_stt = TranscriptionSettings(
        preset=ProviderPreset.CUSTOM,
        provider_id="custom",
        provider_name="Custom",
        endpoint_url="http://localhost:8000/v1/audio",
        auth_mode=AuthMode.BEARER,
        model_id="m",
    )
    assert local_stt.endpoint_url == "http://localhost:8000/v1/audio"

    # Remote plain HTTP with auth_mode=NONE is permitted (e.g. internal LAN Ollama/vLLM)
    lan_settings = TextAnalysisSettings(
        preset=ProviderPreset.CUSTOM,
        provider_id="custom",
        provider_name="Custom",
        base_url="http://192.168.1.100:11434/v1",
        auth_mode=AuthMode.NONE,
        primary_model=AnalysisModelSettings(model_id="m"),
    )
    assert lan_settings.auth_mode == AuthMode.NONE


def test_api_key_update_validations():
    # Preserve must not have value
    with pytest.raises(ValidationError, match="value must be None"):
        ApiKeyUpdate(action=ApiKeyAction.PRESERVE, value=SecretStr("secret"))

    # Clear must not have value
    with pytest.raises(ValidationError, match="value must be None"):
        ApiKeyUpdate(action=ApiKeyAction.CLEAR, value=SecretStr("secret"))

    # Replace requires value
    with pytest.raises(ValidationError, match="value is required"):
        ApiKeyUpdate(action=ApiKeyAction.REPLACE, value=None)

    # Replace cannot be empty/whitespace
    with pytest.raises(ValidationError, match="empty or whitespace"):
        ApiKeyUpdate(action=ApiKeyAction.REPLACE, value=SecretStr("   "))

    # Replace cannot contain newlines
    with pytest.raises(ValidationError, match="newline"):
        ApiKeyUpdate(action=ApiKeyAction.REPLACE, value=SecretStr("key\nwith_newline"))


def test_test_ai_settings_request_validation():
    # STT target requires transcription
    with pytest.raises(ValidationError, match="transcription must be provided"):
        TestAiSettingsRequest(target=TestTarget.STT)

    # LLM target requires text_analysis
    with pytest.raises(ValidationError, match="text_analysis must be provided"):
        TestAiSettingsRequest(target=TestTarget.LLM)

    # API key with newlines rejected
    stt_conf = TranscriptionSettings(
        preset=ProviderPreset.ROUTERAI,
        provider_id="routerai",
        provider_name="RouterAI",
        endpoint_url="https://routerai.ru/api/v1/audio/transcriptions",
        model_id="openai/gpt-transcribe",
    )
    with pytest.raises(ValidationError, match="newline"):
        TestAiSettingsRequest(
            target=TestTarget.STT,
            transcription=stt_conf,
            api_key=SecretStr("bad\nkey"),
        )
