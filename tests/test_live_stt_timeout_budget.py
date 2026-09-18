"""
Live STT timeout budget tests.
Тесты бюджета таймаутов живой транскрибации.

Регрессия: дедлайн задания TRANSCRIBE_TURN был вдвое меньше клиентского таймаута STT, поэтому
внешняя отмена срабатывала раньше таймаута httpx. Отмена приходит как CancelledError, который
адаптер не обрабатывает, поэтому его собственные ретраи не выполнялись, а полоса live-STT
занималась на три подряд 30-секундных ожидания.
"""
import hashlib

import httpx
import pytest

from backend.adapters import stt as stt_module
from backend.adapters.stt import STTTransientError
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import (
    DEFAULT_JOB_DEADLINES,
    STT_TURN_ATTEMPT_TIMEOUT_SEC,
    STT_TURN_MAX_RETRIES,
    PipelineWorker,
    assert_stt_turn_budget,
)
from contracts.provider import STTProfile, STTProtocol

CAPTURE_EPOCH = 1

# The adapter class carries a provider-agnostic prefix; resolve it by suffix so the test
# does not depend on the exact spelling.
ADAPTER_CLS = next(
    obj
    for name, obj in vars(stt_module).items()
    if name.endswith("CompatibleSTTAdapter") and isinstance(obj, type)
)


def _profile(timeout_seconds: float = 60.0) -> STTProfile:
    return STTProfile(
        id="test-asr",
        name="Test ASR",
        endpoint_url="https://example.invalid/v1/audio/transcriptions",
        protocol=STTProtocol.BATCH,
        model_id="test-asr-model",
        timeout_seconds=timeout_seconds,
    )


# ============================================================================
# 1. Инвариант бюджета: дедлайн задания больше одной попытки STT
# ============================================================================
def test_turn_deadline_exceeds_per_attempt_stt_timeout():
    """
    Внешний дедлайн обязан быть больше таймаута одной попытки, иначе отмена pre-empt'ит
    обработку таймаута и ретраи внутри адаптера не выполняются.
    """
    deadline = DEFAULT_JOB_DEADLINES["TRANSCRIBE_TURN"]
    assert deadline is not None
    assert deadline > STT_TURN_ATTEMPT_TIMEOUT_SEC, (
        "дедлайн TRANSCRIBE_TURN должен быть больше таймаута одной попытки STT"
    )
    # Конфигурация, которая привела к инциденту, должна отвергаться проверкой.
    assert_stt_turn_budget(deadline, STT_TURN_ATTEMPT_TIMEOUT_SEC)


def test_historical_broken_budget_is_rejected():
    """
    Регрессия инцидента: дедлайн 30s при клиентском таймауте 60s. Внешняя отмена срабатывала
    раньше таймаута httpx, адаптер видел CancelledError, и его ретраи не выполнялись.
    """
    old_deadline = 30.0
    old_effective_attempt_timeout = 60.0  # profile.timeout_seconds / shared client timeout

    with pytest.raises(ValueError):
        assert_stt_turn_budget(old_deadline, old_effective_attempt_timeout)


def test_budget_check_skips_unbounded_jobs():
    """У заданий без дедлайна (BATCH_RETRANSCRIBE) проверка не применяется."""
    assert_stt_turn_budget(None, STT_TURN_ATTEMPT_TIMEOUT_SEC)


# ============================================================================
# 2. Адаптер применяет per-attempt таймаут и обрабатывает свой таймаут сам
# ============================================================================
@pytest.mark.asyncio
async def test_per_attempt_timeout_override_reaches_the_request():
    """Явный timeout_seconds применяется к HTTP-запросу вместо таймаута профиля."""
    observed: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"text": "ок"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ADAPTER_CLS(_profile(timeout_seconds=60.0), api_key="test-key", http_client=client)

    await adapter.transcribe_audio(b"RIFF....", timeout_seconds=STT_TURN_ATTEMPT_TIMEOUT_SEC)
    assert observed == [STT_TURN_ATTEMPT_TIMEOUT_SEC]

    # Без override используется таймаут профиля
    await adapter.transcribe_audio(b"RIFF....")
    assert observed[-1] == 60.0

    await client.aclose()


@pytest.mark.asyncio
async def test_adapter_timeout_fails_fast_with_single_attempt():
    """
    С max_retries=1 таймаут апстрима завершает попытку сразу, а не занимает полосу
    повторными ожиданиями: именно так полоса live-STT перестаёт блокироваться.
    """
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("upstream did not answer", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ADAPTER_CLS(_profile(timeout_seconds=60.0), api_key="test-key", http_client=client)

    with pytest.raises(STTTransientError):
        await adapter.transcribe_audio(
            b"RIFF....",
            max_retries=STT_TURN_MAX_RETRIES,
            timeout_seconds=STT_TURN_ATTEMPT_TIMEOUT_SEC,
        )

    assert attempts == 1, "с max_retries=1 адаптер обязан сделать ровно одну попытку"
    await client.aclose()


@pytest.mark.asyncio
async def test_adapter_retries_its_own_timeout_within_budget():
    """
    Пока бюджет попыток позволяет, таймаут адаптера приводит к повторной попытке.
    Ранее этот путь не выполнялся вообще: внешняя отмена приходила как CancelledError,
    которую адаптер не обрабатывает.
    """
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("upstream did not answer", request=request)
        return httpx.Response(200, json={"text": "со второй попытки"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ADAPTER_CLS(_profile(timeout_seconds=60.0), api_key="test-key", http_client=client)

    res = await adapter.transcribe_audio(
        b"RIFF....",
        max_retries=2,
        initial_backoff=0.01,
        timeout_seconds=STT_TURN_ATTEMPT_TIMEOUT_SEC,
    )

    assert res.text == "со второй попытки"
    assert attempts == 2
    await client.aclose()


# ============================================================================
# 3. Воркер передаёт согласованный бюджет в адаптер
# ============================================================================
class _RecordingSTTAdapter:
    """Заглушка STT: запоминает параметры вызова и отдаёт фиксированный текст."""

    def __init__(self, text: str = "Распознанный ответ кандидата") -> None:
        self.calls: list[dict] = []
        self.text = text

    async def transcribe_audio(self, audio_data, **kwargs):
        from backend.adapters.stt import STTTranscriptionResult

        self.calls.append(kwargs)
        return STTTranscriptionResult(
            text=self.text,
            model_id="stub-model",
            latency_seconds=0.01,
            raw_response={"text": self.text},
        )


def _seed_interview_with_chunks(repo: Repository, interview_id: str, tmp_path, chunk_count: int = 2) -> None:
    repo.create_interview(
        interview_id=interview_id,
        title="Live STT budget",
        candidate_name="Test Cand",
        role="Engineer",
        capture_mode="single_source",
    )
    repo.update_interview_status(interview_id, "ready")
    repo.update_interview_status(interview_id, "recording")

    spool_dir = tmp_path / "spool"
    for seq in range(chunk_count):
        raw_bytes = bytes([(seq * 7 + i) % 256 for i in range(32000)])
        repo.save_audio_chunk(
            interview_id=interview_id,
            track_id="shared",
            capture_epoch=CAPTURE_EPOCH,
            sequence=seq,
            start_time_ms=seq * 1000,
            end_time_ms=(seq + 1) * 1000,
            sample_rate=16000,
            channels=1,
            sample_count=16000,
            format_str="pcm_s16le",
            checksum_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            payload_bytes=raw_bytes,
            spool_base_dir=spool_dir,
        )


@pytest.mark.asyncio
async def test_transcribe_turn_passes_live_stt_budget(tmp_path):
    """
    _handle_transcribe_turn обязан передавать per-attempt таймаут и бюджет ретраев,
    иначе адаптер снова начнёт ждать клиентский таймаут 60s под дедлайном 25s.
    """
    db = Database(str(tmp_path / "live_stt.db"))
    db.init_schema()
    repo = Repository(db)

    interview_id = "inv-live-budget-1"
    _seed_interview_with_chunks(repo, interview_id, tmp_path)

    stub = _RecordingSTTAdapter()
    worker = PipelineWorker(repository=repo, stt_adapter=stub, llm_adapter=object())

    await worker._handle_transcribe_turn(
        interview_id,
        {
            "track_id": "shared",
            "capture_epoch": CAPTURE_EPOCH,
            "first_sequence": 0,
            "first_sample_offset": 0,
            "last_sequence": 1,
            "last_sample_offset": 16000,
            "start_ms": 0,
            "end_ms": 2000,
            "sample_rate": 16000,
            "channels": 1,
            "language": "ru",
            "segment_id": "seg-live-budget-1",
        },
    )

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["timeout_seconds"] == STT_TURN_ATTEMPT_TIMEOUT_SEC
    assert call["max_retries"] == STT_TURN_MAX_RETRIES

    # Бюджет попытки укладывается в дедлайн задания с запасом на обработку
    assert call["timeout_seconds"] < DEFAULT_JOB_DEADLINES["TRANSCRIBE_TURN"]

    segments = repo.get_transcript_segments(interview_id)
    assert any(s["id"] == "seg-live-budget-1" for s in segments)
