import httpx
import pytest

from backend.adapters.stt import (
    OpenAICompatibleSTTAdapter,
    STTAuthenticationError,
)
from contracts.provider import STTProfile, STTProtocol


@pytest.fixture
def stt_profile() -> STTProfile:
    return STTProfile(
        id="test-whisper",
        name="Test Whisper",
        endpoint_url="https://plusvibeapi.ru/v1/audio/transcriptions",
        protocol=STTProtocol.BATCH,
        model_id="whisper-large-v3-turbo",
        timeout_seconds=30.0,
    )


@pytest.mark.asyncio
async def test_successful_stt_transcription(stt_profile):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v1/audio/transcriptions" in str(request.url)
        return httpx.Response(200, json={"text": "Привет, мир. Тестируем распознавание речи."})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenAICompatibleSTTAdapter(stt_profile, api_key="test-key", http_client=client)

    fake_wav = b"RIFF....WAVEfmt ...."
    res = await adapter.transcribe_audio(fake_wav, language="ru")

    assert res.text == "Привет, мир. Тестируем распознавание речи."
    assert res.model_id == "whisper-large-v3-turbo"
    assert res.latency_seconds >= 0.0
    await client.aclose()


@pytest.mark.asyncio
async def test_stt_auth_error_not_retried(stt_profile):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenAICompatibleSTTAdapter(stt_profile, api_key="test-key", http_client=client)

    fake_wav = b"RIFF...."
    with pytest.raises(STTAuthenticationError):
        await adapter.transcribe_audio(fake_wav, max_retries=2)

    await client.aclose()


@pytest.mark.asyncio
async def test_stt_server_error_retries_and_succeeds(stt_profile):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, json={"text": "Успешная попытка после сбоя"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    adapter = OpenAICompatibleSTTAdapter(stt_profile, api_key="test-key", http_client=client)

    fake_wav = b"RIFF...."
    res = await adapter.transcribe_audio(fake_wav, max_retries=3, initial_backoff=0.01)

    assert attempts == 2
    assert res.text == "Успешная попытка после сбоя"
    await client.aclose()
