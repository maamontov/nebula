"""
A failed transcription (e.g. missing STT API key) must not trap the interview in PROCESSING:
the reason is reported, and the interviewer can explicitly open review with an incomplete
transcript. Missing audio is never bypassed.
"""
import json

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository, get_trusted_spool_dir
from backend.db.database import Database
from backend.db.repository import Repository

INV = "inv-stt-fail"
STT_ERROR = "Missing STT API key: provider 'RouterAI' requires Bearer authentication."


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "incomplete.db"))
    db.init_schema()
    repo = Repository(db)
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_trusted_spool_dir] = lambda: spool_dir
    with TestClient(app) as client:
        yield client, repo, db, spool_dir
    app.dependency_overrides.clear()


def _stopped_interview(client, db, spool_dir, *, ingest_chunk: bool, stt_status: str):
    assert client.post(
        "/api/v1/interviews",
        json={"id": INV, "title": "t", "candidate_name": "c", "role": "r", "capture_mode": "single_source"},
    ).status_code == 200
    assert client.post(f"/api/v1/interviews/{INV}/status", json={"target_status": "ready"}).status_code == 200
    assert client.post(
        f"/api/v1/interviews/{INV}/status",
        json={"target_status": "recording", "consent_confirmed_at": "2026-09-28T12:00:00Z"},
    ).status_code == 200

    shared_dir = spool_dir / INV / "shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / "manifest.json").write_text(json.dumps({
        "interview_id": INV, "track_id": "shared", "capture_epoch": 0,
        "total_chunks": 1, "total_duration_ms": 2000, "is_sealed": True,
    }))
    assert client.post(f"/api/v1/interviews/{INV}/stop", json={"manifests": []}).status_code == 200

    with db.transaction() as conn:
        if ingest_chunk:
            conn.execute(
                """
                INSERT INTO audio_chunks (
                    interview_id, track_id, capture_epoch, sequence, start_time_ms, end_time_ms,
                    sample_rate, channels, sample_count, format, checksum_sha256, size_bytes, file_path, created_at
                ) VALUES (?, 'shared', 0, 0, 0, 2000, 16000, 1, 32000, 'pcm_s16le', ?, 64000, '/tmp/x.pcm', '2026-09-28T12:00:00Z')
                """,
                (INV, "a" * 64),
            )
            conn.execute(
                "INSERT INTO jobs (id, interview_id, type, status, payload_json, created_at, updated_at) "
                "VALUES ('stt-audio-1', ?, 'TRANSCRIBE_AUDIO', 'COMPLETED', '{}', '2026-09-28T12:00:00Z', '2026-09-28T12:00:00Z')",
                (INV,),
            )
        conn.execute(
            "INSERT INTO jobs (id, interview_id, type, status, payload_json, error_message, created_at, updated_at) "
            "VALUES ('stt-turn-1', ?, 'TRANSCRIBE_TURN', ?, '{}', ?, '2026-09-28T12:00:01Z', '2026-09-28T12:00:01Z')",
            (INV, stt_status, STT_ERROR if stt_status == "FAILED" else None),
        )


def test_readiness_reports_stt_failure_reason(env):
    client, _repo, db, spool_dir = env
    _stopped_interview(client, db, spool_dir, ingest_chunk=True, stt_status="FAILED")

    report = client.get(f"/api/v1/interviews/{INV}/readiness").json()
    assert report["is_ready"] is False
    assert report["state"] == "STT_FAILED"
    assert report["failed_error"] == STT_ERROR


def test_review_with_incomplete_transcript_requires_explicit_consent(env):
    client, repo, db, spool_dir = env
    _stopped_interview(client, db, spool_dir, ingest_chunk=True, stt_status="FAILED")

    blocked = client.post(f"/api/v1/interviews/{INV}/status", json={"current_status": "processing", "target_status": "review"})
    assert blocked.status_code == 409

    opened = client.post(
        f"/api/v1/interviews/{INV}/status",
        json={"current_status": "processing", "target_status": "review", "allow_incomplete_transcript": True},
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["status"] == "review"
    events = [e["event_type"] for e in repo.get_audit_events(INV)]
    assert "REVIEW_OPENED_WITH_INCOMPLETE_TRANSCRIPT" in events


def test_incomplete_review_allowed_while_stt_still_running(env):
    client, _repo, db, spool_dir = env
    _stopped_interview(client, db, spool_dir, ingest_chunk=True, stt_status="PENDING")
    res = client.post(
        f"/api/v1/interviews/{INV}/status",
        json={"target_status": "review", "allow_incomplete_transcript": True},
    )
    assert res.status_code == 200, res.text


def test_missing_audio_is_never_bypassed(env):
    client, _repo, db, spool_dir = env
    _stopped_interview(client, db, spool_dir, ingest_chunk=False, stt_status="FAILED")
    res = client.post(
        f"/api/v1/interviews/{INV}/status",
        json={"target_status": "review", "allow_incomplete_transcript": True},
    )
    assert res.status_code == 409
    assert "AWAITING_CHUNKS" in res.json()["detail"]


def test_live_state_reports_transcription_issue(env):
    client, _repo, db, spool_dir = env
    _stopped_interview(client, db, spool_dir, ingest_chunk=True, stt_status="FAILED")
    issue = client.get(f"/api/v1/interviews/{INV}/live-state").json()["transcription_issue"]
    assert issue == {"failed_jobs": 1, "error": STT_ERROR}


def test_failed_transcription_can_be_requeued(env):
    client, repo, db, spool_dir = env
    _stopped_interview(client, db, spool_dir, ingest_chunk=True, stt_status="FAILED")

    res = client.post(f"/api/v1/interviews/{INV}/transcription/retry-failed")
    assert res.status_code == 200
    assert res.json() == {"requeued_jobs": 1}

    report = client.get(f"/api/v1/interviews/{INV}/readiness").json()
    assert report["state"] == "STT_IN_PROGRESS"
    assert client.post(f"/api/v1/interviews/{INV}/transcription/retry-failed").json() == {"requeued_jobs": 0}
