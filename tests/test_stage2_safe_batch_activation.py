import hashlib
import math
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.adapters.stt import STTTranscriptionResult
from backend.api.app import app
from backend.core.audio_utils import pcm_s16le_to_wav_bytes
from backend.db.database import Database
from backend.db.repository import Repository, RepositoryConflictError
from backend.workers.pipeline import PipelineWorker


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "stage2.db"))
    database.init_schema()
    return database


@pytest.fixture
def repo(db):
    return Repository(db)


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr("backend.api.app.get_db", lambda: db)
    monkeypatch.setenv("NEBULA_DISABLE_AUTH", "1")
    return TestClient(app)


def _create_audio_chunk(db, tmp_path, interview_id):
    chunk_file = tmp_path / f"chunk_{interview_id}.wav"
    sample_count = 16000 * 2
    fake_pcm = b"".join(
        struct.pack("<h", int(15000 * math.sin(2 * math.pi * 440 * i / 16000)))
        for i in range(sample_count)
    )
    chunk_file.write_bytes(pcm_s16le_to_wav_bytes(fake_pcm, 16000, 1))
    raw_file_bytes = chunk_file.read_bytes()
    file_sha256 = hashlib.sha256(raw_file_bytes).hexdigest()

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO audio_chunks (
                interview_id, track_id, capture_epoch, sequence,
                start_time_ms, end_time_ms, sample_rate, channels,
                sample_count, format, checksum_sha256, size_bytes,
                file_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interview_id, "shared", 1, 0,
                0, 2000, 16000, 1,
                sample_count, "pcm_s16le", file_sha256, len(raw_file_bytes),
                str(chunk_file), "2026-09-12T12:00:00Z",
            ),
        )


@pytest.mark.asyncio
async def test_batch_retranscribe_fails_cleanly_on_concurrent_manual_edit(tmp_path, db, repo):
    interview_id = "inv-r1-worker"
    repo.create_interview(interview_id, "Title", "Cand", "Role")
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="shared",
        start_time_ms=0,
        end_time_ms=5000,
        text="Hello world",
        speaker_role="unknown",
        revision_id="trans-rev-1",
    )
    _create_audio_chunk(db, tmp_path, interview_id)

    stt_mock = MagicMock()

    async def fake_transcribe(*args, **kwargs):
        repo.update_segment_speaker_role(interview_id, "seg-1", "interviewer")
        return STTTranscriptionResult(text="Hello world transcribed", model_id="test-stt", latency_seconds=0.1, raw_response={})

    stt_mock.transcribe_audio = AsyncMock(side_effect=fake_transcribe)
    worker = PipelineWorker(repository=repo, stt_adapter=stt_mock)

    job_id = "job-batch-test-1"
    repo.enqueue_job(
        job_id=job_id,
        job_type="BATCH_RETRANSCRIBE",
        interview_id=interview_id,
        payload={
            "old_revision_id": "trans-rev-1",
            "new_revision_id": "trans-rev-3",
        },
        max_attempts=1,
    )

    # Process job via worker loop
    processed = await worker.process_one_job()
    assert processed is False

    # Check job status is FAILED
    with db.transaction() as conn:
        job = conn.execute("SELECT status, error_message FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["status"] == "FAILED"
        assert "Transcript revision conflict" in job["error_message"]

        # Conflict audit event recorded
        audit = conn.execute(
            "SELECT event_type FROM audit_events WHERE interview_id = ? AND event_type = 'BATCH_RETRANSCRIPTION_CONFLICT'",
            (interview_id,),
        ).fetchone()
        assert audit is not None

    # Check active revision retains human edit
    inv = repo.get_interview(interview_id)
    active_rev = inv["active_transcript_revision_id"]
    active_segs = repo.get_transcript_segments(interview_id, revision_id=active_rev)
    seg = next(s for s in active_segs if s["id"] == "seg-1")
    assert seg["speaker_role"] == "interviewer"


def test_duplicate_batch_retranscribe_job_rejected(repo):
    interview_id = "inv-dup-test"
    repo.create_interview(interview_id, "Title", "Cand", "Role")

    repo.enqueue_job(
        job_id="job-batch-dup-1",
        job_type="BATCH_RETRANSCRIBE",
        interview_id=interview_id,
        payload={"old_revision_id": "trans-rev-1", "new_revision_id": "trans-rev-2"},
    )

    with pytest.raises(RepositoryConflictError) as exc_info:
        repo.enqueue_job(
            job_id="job-batch-dup-2",
            job_type="BATCH_RETRANSCRIBE",
            interview_id=interview_id,
            payload={"old_revision_id": "trans-rev-1", "new_revision_id": "trans-rev-2"},
        )
    assert "already pending/processing" in str(exc_info.value)


def test_batch_retranscribe_endpoint_rejects_stale_old_revision(client, repo):
    interview_id = "inv-endpoint-stale"
    repo.create_interview(interview_id, "Title", "Cand", "Role")
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=2000,
        text="text",
        revision_id="trans-rev-1",
    )
    # Manual edit moves active revision to trans-rev-2
    repo.update_segment_speaker_role(interview_id, "seg-1", "interviewer")
    assert repo.get_interview(interview_id)["active_transcript_revision_id"] == "trans-rev-2"

    # Client tries to request batch with stale old_revision_id trans-rev-1
    res = client.post(
        f"/api/v1/interviews/{interview_id}/batch-retranscribe",
        json={"old_revision_id": "trans-rev-1", "new_revision_id": "trans-rev-3"},
    )
    assert res.status_code == 409
    assert "Active transcript revision is 'trans-rev-2'" in res.json()["detail"]


@pytest.mark.asyncio
async def test_batch_retranscribe_retry_after_publish_is_noop(repo):
    interview_id = "inv-retry-noop"
    repo.create_interview(interview_id, "Title", "Cand", "Role")
    repo.create_transcript_revision(
        revision_id="trans-rev-2",
        interview_id=interview_id,
        revision_number=2,
        is_batch_final=True,
    )
    repo.add_transcript_segment(
        segment_id="seg-new-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=2000,
        text="New batch text",
        revision_id="trans-rev-2",
    )
    # Set trans-rev-2 already active (simulating previous successful publication)
    repo.set_active_transcript_revision(interview_id, "trans-rev-2")

    stt_mock = MagicMock()
    worker = PipelineWorker(repository=repo, stt_adapter=stt_mock)

    payload = {
        "old_revision_id": "trans-rev-1",
        "new_revision_id": "trans-rev-2",
        "segments": [{"id": "seg-unused", "text": "should not be called", "start_time_ms": 0, "end_time_ms": 100}],
    }
    # Direct call must return immediately without changing revisions or re-processing
    await worker._handle_batch_retranscribe(interview_id, payload)

    inv = repo.get_interview(interview_id)
    assert inv["active_transcript_revision_id"] == "trans-rev-2"
    # No extra revisions created
    revs = repo.get_transcript_revisions(interview_id)
    assert {r["id"] for r in revs} == {"trans-rev-1", "trans-rev-2"}

