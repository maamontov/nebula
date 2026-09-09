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
