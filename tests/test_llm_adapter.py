import httpx
import pytest

from backend.adapters.llm import (
    LLMAuthenticationError,
    OpenAICompatibleAdapter,
    extract_clean_json_content,
)
from contracts.provider import (
    ModelProfile,
    ProviderProfile,
    StructuredOutputMode,
)


def test_extract_clean_json():
    raw_with_think = "<think>Internal reasoning trace</think>```json\n{\"score\": 4}\n```"
    cleaned = extract_clean_json_content(raw_with_think)
    assert cleaned == '{"score": 4}'

    raw_plain = "{\"score\": 5}"
    assert extract_clean_json_content(raw_plain) == '{"score": 5}'


def test_build_payload_modes():
    provider = ProviderProfile(id="p1", name="P1", base_url="http://localhost:8000/v1", api_key_env="DUMMY")

    # Mode 1: JSON_SCHEMA
    m_schema = ModelProfile(
        id="m1", provider_id="p1", upstream_model_id="local-model",
        structured_output_mode=StructuredOutputMode.JSON_SCHEMA,
    )
    adapter1 = OpenAICompatibleAdapter(provider, m_schema)
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    p1 = adapter1.build_payload([{"role": "user", "content": "Hi"}], json_schema=schema)
    assert p1["response_format"]["type"] == "json_schema"
    assert p1["response_format"]["json_schema"]["strict"] is True

    # Mode 2: JSON_OBJECT
    m_obj = ModelProfile(
        id="m2", provider_id="p1", upstream_model_id="local-model",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
    )
    adapter2 = OpenAICompatibleAdapter(provider, m_obj)
    p2 = adapter2.build_payload([{"role": "user", "content": "Hi"}], json_schema=schema)
    assert p2["response_format"]["type"] == "json_object"

    # Mode 3: PROMPT_INSTRUCTION
    m_prompt = ModelProfile(
        id="m3", provider_id="p1", upstream_model_id="local-model",
        structured_output_mode=StructuredOutputMode.PROMPT_INSTRUCTION,
    )
    adapter3 = OpenAICompatibleAdapter(provider, m_prompt)
    p3 = adapter3.build_payload([{"role": "user", "content": "Hi"}], json_schema=schema)
    assert "response_format" not in p3
    assert "STRICTLY with a valid JSON" in p3["messages"][0]["content"]


def test_build_payload_thinking_disable_payload():
    provider = ProviderProfile(id="p1", name="P1", base_url="http://localhost:8000/v1", api_key_env="DUMMY")
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    # Capability not declared -> the switch is never sent, so other providers are unaffected.
    plain = ModelProfile(
        id="m-plain", provider_id="p1", upstream_model_id="plain-model",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
    )
    p_plain = OpenAICompatibleAdapter(provider, plain).build_payload(
        [{"role": "user", "content": "Hi"}], json_schema=schema
    )
    assert "enable_thinking" not in p_plain
    assert "reasoning_effort" not in p_plain

    # Declared -> the fragment is merged verbatim without disturbing the request contract.
    qwen = ModelProfile(
        id="m-qwen", provider_id="p1", upstream_model_id="qwen-model",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        thinking_disable_payload={"enable_thinking": False},
    )
    p_qwen = OpenAICompatibleAdapter(provider, qwen).build_payload(
        [{"role": "user", "content": "Hi"}], json_schema=schema
    )
    assert p_qwen["enable_thinking"] is False
    assert p_qwen["model"] == "qwen-model"
    # JSON_OBJECT mode prepends a system schema instruction; the caller's turn must survive.
    assert p_qwen["messages"][-1] == {"role": "user", "content": "Hi"}
    assert p_qwen["response_format"]["type"] == "json_object"

    # DeepSeek-style fragment: a different key expresses the same capability.
    deepseek = ModelProfile(
        id="m-ds", provider_id="p1", upstream_model_id="ds-model",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        thinking_disable_payload={"reasoning_effort": "none"},
    )
    p_ds = OpenAICompatibleAdapter(provider, deepseek).build_payload(
        [{"role": "user", "content": "Hi"}], json_schema=schema
    )
    assert p_ds["reasoning_effort"] == "none"
    assert "enable_thinking" not in p_ds

    # A misconfigured fragment must never be able to hijack the request contract.
    hijack = ModelProfile(
        id="m-bad", provider_id="p1", upstream_model_id="real-model",
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        thinking_disable_payload={
            "model": "attacker-model",
            "messages": [],
            "response_format": {"type": "text"},
            "reasoning_effort": "none",
        },
    )
    p_bad = OpenAICompatibleAdapter(provider, hijack).build_payload(
        [{"role": "user", "content": "Hi"}], json_schema=schema
    )
    assert p_bad["model"] == "real-model"
    assert p_bad["messages"][-1] == {"role": "user", "content": "Hi"}
    assert p_bad["response_format"]["type"] == "json_object"
    # Non-reserved keys from the same fragment still apply.
    assert p_bad["reasoning_effort"] == "none"

@pytest.mark.asyncio
async def test_successful_request_with_mock_transport():
    mock_response = {
        "id": "chatcmpl-1",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "<think>Thinking</think>{\"score\": 5, \"verdict\": \"pass\"}",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_response)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    provider = ProviderProfile(id="p1", name="P1", base_url="http://localhost:8000/v1", api_key_env="DUMMY")
    model = ModelProfile(id="m1", provider_id="p1", upstream_model_id="test-model", structured_output_mode=StructuredOutputMode.JSON_OBJECT)
    adapter = OpenAICompatibleAdapter(provider, model, http_client=client)

    schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    result = await adapter.execute_request([{"role": "user", "content": "Rate"}], json_schema=schema)
    assert result["data"] == {"score": 5, "verdict": "pass"}
    assert result["usage"]["total_tokens"] == 25
    await client.aclose()


@pytest.mark.asyncio
async def test_auth_error_not_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized: Invalid API key")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    provider = ProviderProfile(id="p1", name="P1", base_url="http://localhost:8000/v1", api_key_env="DUMMY")
    model = ModelProfile(id="m1", provider_id="p1", upstream_model_id="test-model")
    adapter = OpenAICompatibleAdapter(provider, model, http_client=client)

    with pytest.raises(LLMAuthenticationError):
        await adapter.execute_request([{"role": "user", "content": "Hi"}], max_retries=3)

    await client.aclose()


@pytest.mark.asyncio
async def test_transient_503_retries_and_succeeds():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "{\"ok\": true}"}}]
        })

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    provider = ProviderProfile(id="p1", name="P1", base_url="http://localhost:8000/v1", api_key_env="DUMMY")
    model = ModelProfile(id="m1", provider_id="p1", upstream_model_id="test-model")
    adapter = OpenAICompatibleAdapter(provider, model, http_client=client)

    res = await adapter.execute_request(
        [{"role": "user", "content": "Hi"}],
        json_schema={"type": "object"},
        initial_backoff=0.01,
        max_retries=3,
    )
    assert res["data"] == {"ok": True}
    assert attempts == 2
    await client.aclose()
