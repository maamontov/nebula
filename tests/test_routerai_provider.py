import json
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.adapters.llm import OpenAICompatibleAdapter
from backend.adapters.resilient_llm import ResilientLLMAdapter
from backend.adapters.stt import OpenAICompatibleSTTAdapter
from backend.core.profiles import (
    get_default_llm_model,
    get_default_provider,
    get_default_stt_profile,
    get_default_task_routes,
    get_routerai_deepseek_model,
    get_routerai_gpt_transcribe_stt,
    get_routerai_provider,
    get_routerai_qwen_asr_stt,
    get_routerai_qwen_flash_model,
    get_routerai_qwen_plus_model,
)
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker
from contracts.provider import StructuredOutputMode


def test_routerai_provider_profile():
    provider = get_routerai_provider()
    assert provider.id == "routerai"
    assert provider.base_url == "https://routerai.ru/api/v1"
    assert provider.api_key_env == "ROUTERAI_API_KEY"
    assert "/api/v1/chat/completions" in provider.allowed_endpoints
    assert "/api/v1/audio/transcriptions" in provider.allowed_endpoints


def test_routerai_model_profiles():
    flash = get_routerai_qwen_flash_model()
    assert flash.id == "routerai-qwen3.7-flash"
    assert flash.provider_id == "routerai"
    assert flash.upstream_model_id == "qwen/qwen3.7-flash"
    assert flash.context_window_tokens == 1000000
    assert flash.supports_reasoning is True
    assert flash.structured_output_mode == StructuredOutputMode.JSON_OBJECT

    plus = get_routerai_qwen_plus_model()
    assert plus.id == "routerai-qwen3.7-plus"
    assert plus.upstream_model_id == "qwen/qwen3.7-plus"

    deepseek = get_routerai_deepseek_model()
    assert deepseek.id == "routerai-deepseek-v4"
    assert deepseek.upstream_model_id == "deepseek/deepseek-v4-flash-0731"


def test_routerai_stt_profile():
    stt = get_routerai_qwen_asr_stt()
    assert stt.id == "routerai-qwen3-asr-1.7b"
    assert stt.endpoint_url == "https://routerai.ru/api/v1/audio/transcriptions"
    assert stt.model_id == "qwen/qwen3-asr-1.7b"
    assert "ru" in stt.supported_languages


def test_routerai_gpt_transcribe_stt_profile():
    stt = get_routerai_gpt_transcribe_stt()
    assert stt.id == "routerai-gpt-transcribe"
    assert stt.endpoint_url == "https://routerai.ru/api/v1/audio/transcriptions"
    assert stt.model_id == "openai/gpt-transcribe"
    assert "ru" in stt.supported_languages


def test_gpt_transcribe_is_the_default_stt_profile():
    """
    Дефолтный STT-профиль - GPT Transcribe: он точнее распознаёт техническую русскую речь,
    а искажённые термины попадают в evidence оценки.
    """
    with patch.dict(os.environ, {"NEBULA_LLM_PROVIDER": "routerai"}, clear=False):
        os.environ.pop("NEBULA_STT_MODEL", None)
        stt = get_default_stt_profile()
        assert stt.id == "routerai-gpt-transcribe"
        assert stt.model_id == "openai/gpt-transcribe"


@pytest.mark.parametrize(
    "model_name, expected_profile_id, expected_upstream",
    [
        ("openai/gpt-transcribe", "routerai-gpt-transcribe", "openai/gpt-transcribe"),
        ("qwen/qwen3-asr-1.7b", "routerai-qwen3-asr-1.7b", "qwen/qwen3-asr-1.7b"),
        ("qwen/qwen3-asr-0.6b", "routerai-qwen3-asr-1.7b", "qwen/qwen3-asr-0.6b"),
        ("microsoft/mai-transcribe-2", "routerai-qwen3-asr-1.7b", "microsoft/mai-transcribe-2"),
    ],
)
def test_stt_model_env_selects_profile_and_preserves_upstream_id(
    model_name, expected_profile_id, expected_upstream
):
    """
    NEBULA_STT_MODEL выбирает профиль, но точный upstream model_id всегда передаётся как есть
    и не подменяется выдуманным названием.
    """
    with patch.dict(os.environ, {"NEBULA_LLM_PROVIDER": "routerai", "NEBULA_STT_MODEL": model_name}):
        stt = get_default_stt_profile()
        assert stt.id == expected_profile_id
        assert stt.model_id == expected_upstream


def test_whisper_model_name_wins_over_provider_default():
    """Явное имя whisper переключает на профиль PlusVibe независимо от провайдера LLM."""
    with patch.dict(
        os.environ,
        {"NEBULA_LLM_PROVIDER": "routerai", "NEBULA_STT_MODEL": "whisper-large-v3-turbo"},
    ):
        stt = get_default_stt_profile()
        assert stt.id == "plusvibe-whisper-turbo"


def test_default_profile_resolvers_routerai():
    with patch.dict(os.environ, {"NEBULA_LLM_PROVIDER": "routerai", "NEBULA_LLM_MODEL": "qwen/qwen3.7-flash", "NEBULA_STT_MODEL": "qwen/qwen3-asr-1.7b"}):
        prov = get_default_provider()
        assert prov.id == "routerai"
        assert prov.base_url == "https://routerai.ru/api/v1"

        llm = get_default_llm_model()
        assert llm.id == "routerai-qwen3.7-flash"
        assert llm.upstream_model_id == "qwen/qwen3.7-flash"

        stt = get_default_stt_profile()
        assert stt.id == "routerai-qwen3-asr-1.7b"
        assert stt.model_id == "qwen/qwen3-asr-1.7b"

        routes = get_default_task_routes()
        assert all(r.model_profile_id == "routerai-qwen3.7-flash" for r in routes)


def test_default_profile_resolvers_fallback_plusvibe():
    with patch.dict(os.environ, {"NEBULA_LLM_PROVIDER": "plusvibe", "NEBULA_LLM_MODEL": "google/gemini-3.8-flash", "NEBULA_STT_MODEL": "whisper-large-v3-turbo"}):
        prov = get_default_provider()
        assert prov.id == "plusvibe"

        llm = get_default_llm_model()
        assert llm.id == "plusvibe-gemini-3.8-flash"

        stt = get_default_stt_profile()
        assert stt.id == "plusvibe-whisper-turbo"


def test_llm_adapter_build_url_routerai():
    provider = get_routerai_provider()
    model = get_routerai_qwen_flash_model()
    adapter = OpenAICompatibleAdapter(provider, model)

    url = adapter._build_url("/chat/completions")
    assert url == "https://routerai.ru/api/v1/chat/completions"

    payload = adapter.build_payload([{"role": "user", "content": "test"}])
    assert payload["model"] == "qwen/qwen3.7-flash"


@pytest.mark.asyncio
async def test_llm_adapter_reads_routerai_key():
    provider = get_routerai_provider()
    model = get_routerai_qwen_flash_model()

    with patch.dict(os.environ, {"ROUTERAI_API_KEY": "test-router-key-123"}):
        adapter = OpenAICompatibleAdapter(provider, model)
        assert adapter._get_api_key() == "test-router-key-123"


def test_routerai_profiles_disable_reasoning():
    """Capability verified 2026-09-15 against RouterAI (see evals/provider_probe.py)."""
    flash = get_routerai_qwen_flash_model()
    assert flash.thinking_disable_payload == {"enable_thinking": False}

    plus = get_routerai_qwen_plus_model()
    assert plus.thinking_disable_payload == {"enable_thinking": False}
    # Corrected metadata: this model does emit reasoning tokens by default (2619 measured).
    assert plus.supports_reasoning is True

    # DeepSeek ignores enable_thinking and needs the effort switch instead.
    deepseek = get_routerai_deepseek_model()
    assert deepseek.thinking_disable_payload == {"reasoning_effort": "none"}

def test_unverified_profiles_never_send_thinking_switch():
    """Providers we have not measured must not receive an unverified parameter."""
    from backend.core.profiles import (
        get_plusvibe_deepseek_model,
        get_plusvibe_gemini_model,
        get_plusvibe_qwen_model,
    )

    for profile in (
        get_plusvibe_gemini_model(),
        get_plusvibe_qwen_model(),
        get_plusvibe_deepseek_model(),
    ):
        assert profile.thinking_disable_payload is None, profile.id

@pytest.mark.asyncio
async def test_resilient_chain_sends_thinking_disable_payload():
    """The assessment path must carry the switch end-to-end, not just in profile metadata."""
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "{\"scores\": [], \"critical_errors\": []}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.dict(os.environ, {"NEBULA_LLM_PROVIDER": "routerai", "ROUTERAI_API_KEY": "dummy-key"}):
        resilient = ResilientLLMAdapter(http_client=client)
        await resilient.execute_request(
            messages=[{"role": "user", "content": "Hi"}],
            json_schema={"type": "object", "properties": {}},
            schema_name="assessment_schema",
        )
    await client.aclose()

    assert len(captured) == 1
    assert captured[0]["model"] == "qwen/qwen3.7-flash"
    assert captured[0]["enable_thinking"] is False
    assert captured[0]["response_format"] == {"type": "json_object"}

@pytest.mark.asyncio
async def test_stt_adapter_routerai_transcription():
    stt_prof = get_routerai_qwen_asr_stt()

    fake_resp = httpx.Response(
        200,
        json={"text": "Привет, мир!"},
        request=httpx.Request("POST", "https://routerai.ru/api/v1/audio/transcriptions"),
    )
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=fake_resp)

    with patch.dict(os.environ, {"ROUTERAI_API_KEY": "test-key-stt"}):
        adapter = OpenAICompatibleSTTAdapter(stt_prof, http_client=mock_client)
        assert adapter._get_api_key() == "test-key-stt"

        res = await adapter.transcribe_audio(b"dummy-audio-bytes", filename="audio.wav", language="ru")
        assert res.text == "Привет, мир!"
        assert res.model_id == "qwen/qwen3-asr-1.7b"

        # Verify post call arguments
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://routerai.ru/api/v1/audio/transcriptions"
        assert call_args[1]["data"]["model"] == "qwen/qwen3-asr-1.7b"
        assert call_args[1]["data"]["language"] == "ru"
        assert call_args[1]["headers"]["Authorization"] == "Bearer test-key-stt"


def test_resilient_llm_adapter_routerai_defaults():
    with patch.dict(os.environ, {"NEBULA_LLM_PROVIDER": "routerai"}):
        resilient = ResilientLLMAdapter()
        assert resilient.provider.id == "routerai"
        assert resilient.primary_model.upstream_model_id == "qwen/qwen3.7-flash"
        assert resilient.fallback_model_1.upstream_model_id == "qwen/qwen3.7-plus"
        assert resilient.fallback_model_2.upstream_model_id == "deepseek/deepseek-v4-flash-0731"


def test_pipeline_worker_initialization_with_routerai(tmp_path):
    db_file = tmp_path / "worker_routerai.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)

    with patch.dict(os.environ, {
        "NEBULA_LLM_PROVIDER": "routerai",
        "NEBULA_LLM_MODEL": "qwen/qwen3.7-flash",
        "NEBULA_STT_MODEL": "qwen/qwen3-asr-1.7b",
        "ROUTERAI_API_KEY": "dummy-key",
    }):
        worker = PipelineWorker(repository=repo)
        assert worker.provider.id == "routerai"
        assert worker.stt_adapter.profile.model_id == "qwen/qwen3-asr-1.7b"
        assert worker.stt_adapter.profile.endpoint_url == "https://routerai.ru/api/v1/audio/transcriptions"
        assert worker.followup_llm_adapter.model.upstream_model_id == "qwen/qwen3.7-flash"

        # Both LLM paths the worker actually uses must send the verified switch, otherwise the
        # capability would exist only as unused profile metadata.
        probe_messages = [{"role": "user", "content": "ping"}]
        assert worker.llm_adapter.primary_adapter.build_payload(probe_messages)["enable_thinking"] is False
        assert worker.followup_llm_adapter.build_payload(probe_messages)["enable_thinking"] is False
