import pytest

from backend.core.ai_settings import (
    get_preset_text_analysis_settings,
    resolve_ai_settings,
)
from backend.core.credential_store import CredentialStore
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.settings import (
    AnalysisModelSettings,
    AuthMode,
    ProviderPreset,
    ReasoningPolicy,
    TextAnalysisSettings,
    TranscriptionSettings,
)


@pytest.fixture
def repo(tmp_path):
    db_file = tmp_path / "resolver_test.db"
    db = Database(str(db_file))
    db.init_schema()
    return Repository(db)


@pytest.fixture
def cred_store(tmp_path):
    return CredentialStore(data_dir=tmp_path / "credentials")


def test_resolver_default_environment(repo, cred_store, monkeypatch):
    monkeypatch.delenv("NEBULA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("NEBULA_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("NEBULA_LLM_MODEL", raising=False)
    monkeypatch.delenv("NEBULA_STT_ENDPOINT", raising=False)
    monkeypatch.delenv("NEBULA_STT_MODEL", raising=False)
    monkeypatch.delenv("NEBULA_STT_LANGUAGE", raising=False)
    monkeypatch.delenv("NEBULA_STT_API_KEY", raising=False)
    monkeypatch.delenv("NEBULA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ROUTERAI_API_KEY", raising=False)

    resp, runtime = resolve_ai_settings(repo, cred_store)

    assert resp.revision == 0
    assert resp.source == "environment"
    assert resp.can_update is True
    assert resp.update_blocker is None

    # Defaults to RouterAI
    assert resp.transcription.preset == ProviderPreset.ROUTERAI
    assert resp.text_analysis.preset == ProviderPreset.ROUTERAI
    assert runtime.language == "ru"
    assert runtime.stt_provider.id == "routerai"
    assert runtime.llm_provider.id == "routerai"
    assert runtime.llm_primary.upstream_model_id == "qwen/qwen3.7-flash"
    assert runtime.llm_primary.thinking_disable_payload == {"enable_thinking": False}


def test_resolver_environment_overrides_and_exact_model(repo, cred_store, monkeypatch):
    monkeypatch.setenv("NEBULA_LLM_PROVIDER", "routerai")
    monkeypatch.setenv("NEBULA_LLM_BASE_URL", "https://custom-router.internal/v1")
    monkeypatch.setenv("NEBULA_LLM_MODEL", "custom-vendor/Custom-Model-V1.5")
    monkeypatch.setenv("NEBULA_STT_ENDPOINT", "https://custom-stt.internal/v1/audio/transcriptions")
    monkeypatch.setenv("NEBULA_STT_MODEL", "whisper-custom-stt")
    monkeypatch.setenv("NEBULA_STT_LANGUAGE", "en")

    resp, runtime = resolve_ai_settings(repo, cred_store)

    assert resp.text_analysis.base_url == "https://custom-router.internal/v1"
    # Exact model ID and case preservation
    assert resp.text_analysis.primary_model.model_id == "custom-vendor/Custom-Model-V1.5"
    assert runtime.llm_primary.upstream_model_id == "custom-vendor/Custom-Model-V1.5"
    assert resp.transcription.endpoint_url == "https://custom-stt.internal/v1/audio/transcriptions"
    assert resp.transcription.model_id == "whisper-custom-stt"
    assert resp.transcription.language == "en"
    assert runtime.language == "en"


def test_resolver_reasoning_policy_mapping():
    router = get_preset_text_analysis_settings(ProviderPreset.ROUTERAI)
    # primary is qwen3.7-flash -> disable_enable_thinking
    assert router.primary_model.reasoning_policy == ReasoningPolicy.DISABLE_ENABLE_THINKING

    # fallback 2 is deepseek -> disable_reasoning_effort
    assert router.fallback_models[1].reasoning_policy == ReasoningPolicy.DISABLE_REASONING_EFFORT

    custom = get_preset_text_analysis_settings(ProviderPreset.CUSTOM)
    assert custom.primary_model.reasoning_policy == ReasoningPolicy.PROVIDER_DEFAULT


def test_resolver_from_database_row(repo, cred_store, monkeypatch):
    # Set env vars that should NOT leak into DB-managed config
    monkeypatch.setenv("NEBULA_LLM_MODEL", "ignored-env-model")
    monkeypatch.setenv("NEBULA_STT_LANGUAGE", "fr")

    # Save a configuration in DB
    stt_conf = TranscriptionSettings(
        preset=ProviderPreset.CUSTOM,
        provider_id="custom-stt",
        provider_name="My Custom STT",
        endpoint_url="https://stt.mycorp.internal/v1/transcribe",
        model_id="corp-whisper-v2",
        auth_mode=AuthMode.BEARER,
        language="de",
    )
    llm_conf = TextAnalysisSettings(
        preset=ProviderPreset.CUSTOM,
        provider_id="custom-llm",
        provider_name="My Custom LLM",
        base_url="https://llm.mycorp.internal/v1",
        auth_mode=AuthMode.BEARER,
        primary_model=AnalysisModelSettings(
            model_id="corp-llm-instruct",
            reasoning_policy=ReasoningPolicy.DISABLE_REASONING_EFFORT,
        ),
        fallback_models=[],
    )

    repo.update_ai_settings(
        expected_revision=0,
        transcription_config=stt_conf.model_dump(),
        text_analysis_config=llm_conf.model_dump(),
    )

    resp, runtime = resolve_ai_settings(repo, cred_store)

    assert resp.revision == 1
    assert resp.source == "database"
    assert resp.transcription.provider_id == "custom-stt"
    assert resp.transcription.language == "de"  # from DB, NOT "fr" from env
    assert resp.text_analysis.primary_model.model_id == "corp-llm-instruct"  # from DB, NOT "ignored-env-model"
    assert runtime.llm_primary.thinking_disable_payload == {"reasoning_effort": "none"}
    assert len(runtime.llm_fallbacks) == 0
