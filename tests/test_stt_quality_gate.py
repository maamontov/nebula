"""
ASR output quality gate tests.
Тесты гейта качества распознавания речи.

Регрессия: на коротких/неразборчивых фрагментах ASR отдаёт правдоподобный, но посторонний
текст (наблюдались другие языки и зацикленное повторение одного слова). Такой сегмент мог
попасть в стенограмму как реплика кандидата и стать evidence оценки.
"""
import hashlib

import pytest

from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import (
    PipelineWorker,
    is_quality_gate_enabled,
    is_script_mismatch,
    transcript_skip_reason,
)

CAPTURE_EPOCH = 1


# ============================================================================
# 1. Пустой и служебный текст
# ============================================================================
@pytest.mark.parametrize("text", ["", "   ", ".", "...", ",", "!", "?", "—", "-"])
def test_empty_and_punctuation_only_are_skipped(text):
    assert transcript_skip_reason(text, "ru", 1000) == "empty" if not text.strip() else True
    assert transcript_skip_reason(text, "ru", 1000) is not None


@pytest.mark.parametrize(
    "text",
    ["продолжение следует", "Спасибо за просмотр!", "до скорых встреч", "Редактор субтитров"],
)
def test_boilerplate_hallucinations_are_skipped(text):
    assert transcript_skip_reason(text, "ru", 4000) == "boilerplate_hallucination"


def test_boilerplate_is_skipped_even_with_gate_disabled(monkeypatch):
    """Служебные фразы не являются настройкой: они отсекаются всегда."""
    monkeypatch.setenv("NEBULA_STT_QUALITY_GATE", "0")
    assert transcript_skip_reason("продолжение следует", "ru", 4000) == "boilerplate_hallucination"


# ============================================================================
# 2. Несоответствие письменности
# ============================================================================
def test_cjk_output_is_skipped_at_any_duration():
    """CJK-текст не может быть ответом на поддерживаемых языках интервью."""
    assert transcript_skip_reason("多啊多啊多啊多啊！", "ru", 1300) == "script_mismatch"
    assert transcript_skip_reason("多啊多啊多啊多啊！", "ru", 20000) == "script_mismatch"
    assert transcript_skip_reason("多啊多啊多啊多啊！", "en", 20000) == "script_mismatch"


def test_wrong_script_on_short_turn_is_skipped():
    """Короткий фрагмент без единого символа ожидаемой письменности — галлюцинация."""
    assert is_script_mismatch("No vraťme důvod.", "ru", 700) is True
    assert transcript_skip_reason("No vraťme důvod.", "ru", 700) == "script_mismatch"


def test_wrong_script_on_long_turn_is_kept():
    """
    Длинный ответ на другом языке может быть настоящим ответом кандидата,
    поэтому на длинных репликах правило не применяется.
    """
    text = "No, I would use PostgreSQL MVCC snapshot isolation for that particular case."
    assert is_script_mismatch(text, "ru", 6000) is False
    assert transcript_skip_reason(text, "ru", 6000) is None


def test_mixed_script_with_technical_terms_is_kept():
    """Русский текст с латинскими техническими терминами сохраняется."""
    for text in (
        "Мы использовали PostgreSQL для шардинга.",
        "MVCC в PostgreSQL решает конфликты чтения и записи.",
    ):
        assert transcript_skip_reason(text, "ru", 2000) is None


def test_real_short_russian_answers_are_kept():
    """Легитимные короткие ответы не должны отбрасываться."""
    for text in ("Десять.", "Ну не больше десяти точно.", "Ни хера нету.", "читал."):
        assert transcript_skip_reason(text, "ru", 800) is None


def test_unknown_language_is_not_script_checked():
    """Для языков без известной письменности проверка не применяется."""
    assert is_script_mismatch("No vraťme důvod.", "de", 700) is False


# ============================================================================
# 3. Зацикленное повторение
# ============================================================================
def test_real_repeated_speech_is_kept():
    """
    Регрессия по реальному замеру: интервьюер считал вслух при проверке микрофона,
    и оба STT-профиля вернули одинаковый текст. Это настоящая речь, её нельзя терять.
    Правило "зацикленное повторение" удалено именно из-за этого ложного срабатывания.
    """
    for text in (
        "Раз, раз, раз, раз.",
        "Раз, раз, раз, два, два, два, три, три, три.",
        "Да, да, да, да.",
    ):
        assert transcript_skip_reason(text, "ru", 1360) is None


def test_short_ascii_technical_answers_are_kept():
    """
    Короткий ответ латиницей - законный ответ кандидата на русском интервью
    ("PostgreSQL.", "MVCC.", "Yes."). Он не должен отбрасываться как чужая письменность.
    """
    for text in ("PostgreSQL.", "MVCC.", "Yes.", "OK"):
        assert is_script_mismatch(text, "ru", 900) is False
        assert transcript_skip_reason(text, "ru", 900) is None


# ============================================================================
# 4. Отключаемость эвристик
# ============================================================================
def test_heuristic_gate_can_be_disabled(monkeypatch):
    """
    Эвристики можно отключить переменной окружения, если оператор готов принимать
    все фрагменты: отключение не влияет на отсечение служебных фраз.
    """
    monkeypatch.setenv("NEBULA_STT_QUALITY_GATE", "0")
    assert is_quality_gate_enabled() is False
    assert transcript_skip_reason("No vraťme důvod.", "ru", 700) is None
    # Служебные фразы всё равно отсекаются
    assert transcript_skip_reason("продолжение следует", "ru", 1360) == "boilerplate_hallucination"


def test_heuristic_gate_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("NEBULA_STT_QUALITY_GATE", raising=False)
    assert is_quality_gate_enabled() is True


# ============================================================================
# 5. Сквозной путь: мусор не сохраняется, причина видна в результате задания
# ============================================================================
class _FixedSTTAdapter:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe_audio(self, audio_data, **kwargs):
        from backend.adapters.stt import STTTranscriptionResult

        return STTTranscriptionResult(
            text=self.text,
            model_id="stub-model",
            latency_seconds=0.01,
            raw_response={"text": self.text},
        )


def _seed(repo: Repository, interview_id: str, tmp_path, chunk_count: int = 2) -> None:
    repo.create_interview(
        interview_id=interview_id,
        title="Quality gate",
        candidate_name="Test Cand",
        role="Engineer",
        capture_mode="single_source",
    )
    repo.update_interview_status(interview_id, "ready")
    repo.update_interview_status(interview_id, "recording")

    spool_dir = tmp_path / "spool"
    for seq in range(chunk_count):
        raw_bytes = bytes([(seq * 11 + i) % 256 for i in range(32000)])
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


def _turn_payload(segment_id: str) -> dict:
    return {
        "track_id": "shared",
        "capture_epoch": CAPTURE_EPOCH,
        "first_sequence": 0,
        "first_sample_offset": 0,
        "last_sequence": 1,
        "last_sample_offset": 16000,
        "start_ms": 0,
        "end_ms": 1000,
        "sample_rate": 16000,
        "channels": 1,
        "language": "ru",
        "segment_id": segment_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hallucinated_text",
    ["多啊多啊多啊多啊！", "No vraťme důvod."],
)
async def test_turn_with_hallucinated_text_is_not_saved(tmp_path, hallucinated_text):
    """Распознанный мусор не становится сегментом стенограммы, но причина фиксируется."""
    db = Database(str(tmp_path / "quality_gate.db"))
    db.init_schema()
    repo = Repository(db)

    interview_id = "inv-quality-gate-1"
    _seed(repo, interview_id, tmp_path)

    worker = PipelineWorker(
        repository=repo,
        stt_adapter=_FixedSTTAdapter(hallucinated_text),
        llm_adapter=object(),
    )

    result = await worker._handle_transcribe_turn(interview_id, _turn_payload("seg-quality-1"))

    assert result is not None
    assert result["is_skipped"] is True
    assert result["skip_reason"] == "script_mismatch"
    assert repo.get_transcript_segments(interview_id) == []


@pytest.mark.asyncio
async def test_turn_with_real_answer_is_saved(tmp_path):
    """Настоящий ответ кандидата сохраняется как сегмент стенограммы."""
    db = Database(str(tmp_path / "quality_gate_ok.db"))
    db.init_schema()
    repo = Repository(db)

    interview_id = "inv-quality-gate-2"
    _seed(repo, interview_id, tmp_path)

    worker = PipelineWorker(
        repository=repo,
        stt_adapter=_FixedSTTAdapter("Мы использовали PostgreSQL для шардинга."),
        llm_adapter=object(),
    )

    result = await worker._handle_transcribe_turn(interview_id, _turn_payload("seg-quality-2"))

    assert result is not None
    assert result.get("is_skipped") is not True
    segments = repo.get_transcript_segments(interview_id)
    assert [s["id"] for s in segments] == ["seg-quality-2"]
    assert segments[0]["speaker_role"] == "unknown"
