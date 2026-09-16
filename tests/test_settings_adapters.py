import httpx
import pytest

from backend.adapters.llm import (
    LLMAdapterError,
    LLMAuthenticationError,
    OpenAICompatibleAdapter,
)
from backend.adapters.resilient_llm import ResilientLLMAdapter
from backend.adapters.stt import OpenAICompatibleSTTAdapter, STTAuthenticationError
from contracts.provider import ModelProfile, ProviderProfile, STTProfile, STTProtocol
from contracts.settings import AuthMode


@pytest.mark.asyncio
async def test_auth_mode_none_does_not_send_authorization_header():
    sent_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_headers
        sent_headers = dict(request.headers)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "{\"ok\": true}"}}]})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    provider = ProviderProfile(id="local", name="Local", base_url="http://127.0.0.1:8000/v1", api_key_env="NO_KEY")
    model = ModelProfile(id="m1", provider_id="local", upstream_model_id="local-model")
    adapter = OpenAICompatibleAdapter(
        provider,
        model,
        api_key=None,
        auth_mode=AuthMode.NONE,
        http_client=client,
    )

    await adapter.execute_request([{"role": "user", "content": "hello"}])
    assert "authorization" not in sent_headers
    await client.aclose()


@pytest.mark.asyncio
async def test_bearer_without_key_fails_before_http_call():
    provider = ProviderProfile(id="p1", name="P1", base_url="http://127.0.0.1:8000/v1", api_key_env="NOT_SET")
    model = ModelProfile(id="m1", provider_id="p1", upstream_model_id="m1")

    # Without external client and with empty env key, drops before HTTP call
    adapter = OpenAICompatibleAdapter(provider, model, api_key=None, auth_mode=AuthMode.BEARER)
    with pytest.raises(LLMAuthenticationError, match="requires Bearer authentication"):
        await adapter.execute_request([{"role": "user", "content": "hi"}])

    stt_prof = STTProfile(
        id="stt1",
        name="STT1",
        endpoint_url="http://127.0.0.1:8000/v1/audio/transcriptions",
        protocol=STTProtocol.BATCH,
        model_id="whisper",
    )
    stt_adapter = OpenAICompatibleSTTAdapter(stt_prof, api_key=None, auth_mode=AuthMode.BEARER)
    with pytest.raises(STTAuthenticationError, match="requires Bearer authentication"):
        await stt_adapter.transcribe_audio(b"fake-audio")


@pytest.mark.asyncio
async def test_resilient_llm_zero_fallbacks_only_primary():
    provider = ProviderProfile(id="p1", name="P1", base_url="http://127.0.0.1:8000/v1", api_key_env="K")
    m_primary = ModelProfile(id="m-prim", provider_id="p1", upstream_model_id="prim-model")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Error")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    adapter = ResilientLLMAdapter(
        provider=provider,
        primary_model=m_primary,
        fallback_models=[],  # explicitly 0 fallbacks
        api_key="valid-key",
        http_client=client,
    )

    with pytest.raises(LLMAdapterError):
        await adapter.execute_request([{"role": "user", "content": "hi"}])

    assert adapter.last_fallback_event is None
    await client.aclose()


@pytest.mark.asyncio
async def test_resilient_llm_auth_error_does_not_trigger_fallback():
    provider = ProviderProfile(id="p1", name="P1", base_url="http://127.0.0.1:8000/v1", api_key_env="K")
    m_primary = ModelProfile(id="m-prim", provider_id="p1", upstream_model_id="prim-model")
    m_fb = ModelProfile(id="m-fb", provider_id="p1", upstream_model_id="fb-model")

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="Unauthorized")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    adapter = ResilientLLMAdapter(
        provider=provider,
        primary_model=m_primary,
        fallback_models=[m_fb],
        api_key="bad-key",
        http_client=client,
    )

    with pytest.raises(LLMAuthenticationError):
        await adapter.execute_request([{"role": "user", "content": "hi"}])

    # Shared auth failure on same provider fails fast without trying fallback
    assert calls == 1
    assert adapter.last_fallback_event is None
    await client.aclose()


@pytest.mark.asyncio
async def test_resilient_llm_fallback_uses_own_capabilities():
    provider = ProviderProfile(id="p1", name="P1", base_url="http://127.0.0.1:8000/v1", api_key_env="K")
    m_primary = ModelProfile(
        id="m-prim",
        provider_id="p1",
        upstream_model_id="prim-model",
        thinking_disable_payload={"enable_thinking": False},
    )
    m_fb = ModelProfile(
        id="m-fb",
        provider_id="p1",
        upstream_model_id="fb-model",
        thinking_disable_payload={"reasoning_effort": "none"},
    )

    sent_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        payload = json.loads(request.content)
        sent_payloads.append(payload)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "{\"result\": \"from_fb\"}"}}]
        })

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    adapter = ResilientLLMAdapter(
        provider=provider,
        primary_model=m_primary,
        fallback_models=[m_fb],
        force_primary_fail=True,
        api_key="valid-key",
        http_client=client,
    )

    res, actual_model = await adapter.execute_request([{"role": "user", "content": "hi"}], json_schema={"type": "object"})

    assert actual_model == "fb-model"
    assert res["data"] == {"result": "from_fb"}
    assert len(sent_payloads) == 1

    # Fallback model payload used its OWN thinking_disable_payload
    assert sent_payloads[0]["model"] == "fb-model"
    assert sent_payloads[0].get("reasoning_effort") == "none"
    assert "enable_thinking" not in sent_payloads[0]

    await client.aclose()
