from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.adapters.stt import STTTranscriptionResult
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker
from contracts.domain import InterviewStatus


@pytest.fixture
def repo(tmp_path):
    db_file = tmp_path / "worker_test.db"
    db = Database(str(db_file))
    db.init_schema()
    return Repository(db)


@pytest.mark.asyncio
async def test_worker_transcribe_audio_job(repo):
    repo.create_interview("inv-w1", "Mobile Lead", "Екатерина", "iOS", status=InterviewStatus.RECORDING)

    # Mock STT adapter
    mock_stt = MagicMock()
    mock_stt.transcribe_audio = AsyncMock(
        return_value=STTTranscriptionResult(
            text="Паттерн MVVM хорошо разделяет UI и бизнес-логику.",
            model_id="whisper-large-v3-turbo",
            latency_seconds=1.2,
            raw_response={},
        )
    )

    worker = PipelineWorker(repo, stt_adapter=mock_stt, llm_adapter=MagicMock())

    # Enqueue a transcription job (audio_hex of dummy bytes)
    dummy_audio = b"\x00\x01\x02\x03".hex()
    repo.enqueue_job(
        job_id="job-transcribe-1",
        job_type="TRANSCRIBE_AUDIO",
        interview_id="inv-w1",
        payload={
            "audio_hex": dummy_audio,
            "track_id": "candidate",
            "start_ms": 1000,
            "end_ms": 3000,
            "segment_id": "seg-mvvm-1",
        },
    )

    processed = await worker.process_one_job()
    assert processed is True

    # Verify transcript segment saved
    segments = repo.get_transcript_segments("inv-w1")
    assert len(segments) == 1
    assert segments[0]["id"] == "seg-mvvm-1"
    assert segments[0]["text"] == "Паттерн MVVM хорошо разделяет UI и бизнес-логику."

    # Verify audit event
    audits = repo.get_audit_events("inv-w1")
    assert any(a["event_type"] == "JOB_COMPLETED_TRANSCRIBE_AUDIO" for a in audits)


@pytest.mark.asyncio
async def test_worker_evaluate_question_job(repo):
    repo.create_interview("inv-w2", "Backend Go", "Сергей", "Go", status=InterviewStatus.RECORDING)

    mock_llm = MagicMock()
    mock_llm.model.upstream_model_id = "google/gemini-3.8-flash"
    mock_llm.execute_request = AsyncMock(
        return_value={
            "data": {
                "scores": [
                    {
                        "criterion_id": "crit-concurrency",
                        "score": 5.0,
                        "explanation": "Точное понимание sync.Pool и sync.Mutex.",
                        "evidence": [{"segment_id": "seg-go-1", "exact_quote": "sync.Pool снижает нагрузку на GC."}],
                    }
                ],
                "critical_errors": [],
            },
            "usage": {"total_tokens": 120},
        }
    )

    repo.add_transcript_segment(
        segment_id="seg-go-1",
        interview_id="inv-w2",
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=10000,
        text="sync.Pool снижает нагрузку на GC.",
        speaker_role="candidate",
        revision_id="trans-rev-1",
    )
    repo.save_association(
        assoc_id="assoc-go-1",
        interview_id="inv-w2",
        question_id="q-sync",
        segment_id="seg-go-1",
        revision_id="trans-rev-1",
    )

    worker = PipelineWorker(repo, stt_adapter=MagicMock(), llm_adapter=mock_llm)

    repo.enqueue_job(
        job_id="job-eval-1",
        job_type="EVALUATE_QUESTION",
        interview_id="inv-w2",
        payload={
            "question_id": "q-sync",
            "rubric_description": "Go concurrency & memory primitives",
        },
    )


    processed = await worker.process_one_job()
    assert processed is True

    props = repo.get_assessment_proposals("inv-w2")
    assert len(props) == 1
    assert props[0]["question_id"] == "q-sync"
    assert props[0]["scores"][0]["score"] == 5.0
    assert props[0]["scores"][0]["evidence"][0]["exact_quote"] == "sync.Pool снижает нагрузку на GC."


@pytest.mark.asyncio
async def test_worker_job_failure_and_retry(repo):
    repo.create_interview("inv-w3", "Security", "Анна", "Sec", status=InterviewStatus.RECORDING)

    mock_stt = MagicMock()
    mock_stt.transcribe_audio = AsyncMock(side_effect=RuntimeError("Connection refused by upstream"))

    worker = PipelineWorker(repo, stt_adapter=mock_stt, llm_adapter=MagicMock())

    repo.enqueue_job(
        job_id="job-fail-1",
        job_type="TRANSCRIBE_AUDIO",
        interview_id="inv-w3",
        payload={"audio_hex": b"1234".hex()},
        max_attempts=1,
    )

    processed = await worker.process_one_job()
    assert processed is False

    # Since max_attempts=1, job must now be FAILED
    with repo.db.transaction() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = 'job-fail-1'").fetchone()
        assert job["status"] == "FAILED"
        assert "Connection refused" in job["error_message"]
