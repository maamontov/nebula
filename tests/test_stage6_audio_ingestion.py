import hashlib
import io
import struct
import wave
import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository, get_trusted_spool_dir
from backend.core.state_machine import transition_status, can_accept_new_chunks
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.domain import InterviewStatus
from contracts.audio import AudioChunkMetadata, AudioFormat, TrackManifest, TrackType


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "stage6_test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)
    spool_dir = tmp_path / "server_spool"
    spool_dir.mkdir(parents=True, exist_ok=True)

    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_trusted_spool_dir] = lambda: spool_dir

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()



def _create_recording_interview(client: TestClient, interview_id: str) -> None:
    # 1. Create interview (status: DRAFT)
    r = client.post(
        "/api/v1/interviews",
        json={
            "id": interview_id,
            "title": "Backend Audio Ingestion Test",
            "candidate_name": "Alice Candidate",
            "role": "Audio Engineer",
        },
    )
    assert r.status_code == 200

    # 2. Transition DRAFT -> READY with consent
    r = client.post(
        f"/api/v1/interviews/{interview_id}/status",
        json={
            "target_status": "ready",
            "consent_confirmed_at": "2026-09-10T15:00:00Z",
            "consent_version": "v1.0",
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ready"

    # 3. Transition READY -> RECORDING
    r = client.post(
        f"/api/v1/interviews/{interview_id}/status",
        json={"target_status": "recording"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "recording"




def test_chunk_ingestion_valid_and_idempotent(client: TestClient):
    interview_id = "inv-audio-1"
    _create_recording_interview(client, interview_id)

    # 16,000 samples of 16-bit mono PCM = 32,000 bytes
    pcm_payload = b"\x00\x01" * 16000
    sha256_hash = hashlib.sha256(pcm_payload).hexdigest()

    metadata = {
        "interview_id": interview_id,
        "track_id": "candidate",
        "capture_epoch": 0,
        "sequence": 0,
        "start_time_ms": 0,
        "end_time_ms": 1000,
        "sample_rate": 16000,
        "channels": 1,
        "sample_count": 16000,
        "format": "pcm_s16le",
        "checksum_sha256": sha256_hash,
        "size_bytes": len(pcm_payload),
    }

    # First ingestion -> 200 OK
    r1 = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={"metadata": metadata, "payload_hex": pcm_payload.hex()},
    )
    assert r1.status_code == 200
    res1 = r1.json()
    assert res1["status"] == "persisted"
    assert res1["sequence"] == 0

    # Idempotent re-send with same bytes and hash -> 200 OK
    r2 = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={"metadata": metadata, "payload_hex": pcm_payload.hex()},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "idempotent_duplicate"


def test_chunk_ingestion_hash_mismatch_rejected(client: TestClient):
    interview_id = "inv-audio-2"
    _create_recording_interview(client, interview_id)

    pcm_payload = b"\xAA\xBB" * 500
    tampered_hash = hashlib.sha256(b"tampered_bytes").hexdigest()

    metadata = {
        "interview_id": interview_id,
        "track_id": "candidate",
        "capture_epoch": 0,
        "sequence": 0,
        "start_time_ms": 0,
        "end_time_ms": 500,
        "sample_rate": 16000,
        "channels": 1,
        "sample_count": 500,
        "format": "pcm_s16le",
        "checksum_sha256": tampered_hash,
        "size_bytes": len(pcm_payload),
    }

    r = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={"metadata": metadata, "payload_hex": pcm_payload.hex()},
    )
    assert r.status_code == 422
    assert "checksum mismatch" in r.json()["detail"].lower()


def test_chunk_ingestion_conflict_different_bytes(client: TestClient):
    interview_id = "inv-audio-3"
    _create_recording_interview(client, interview_id)

    payload_a = b"\x11" * 100
    hash_a = hashlib.sha256(payload_a).hexdigest()

    metadata_a = {
        "interview_id": interview_id,
        "track_id": "interviewer",
        "capture_epoch": 0,
        "sequence": 0,
        "start_time_ms": 0,
        "end_time_ms": 100,
        "sample_rate": 16000,
        "channels": 1,
        "sample_count": 50,
        "format": "pcm_s16le",
        "checksum_sha256": hash_a,
        "size_bytes": len(payload_a),
    }

    r1 = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={"metadata": metadata_a, "payload_hex": payload_a.hex()},
    )
    assert r1.status_code == 200

    # Send DIFFERENT bytes for the same sequence key -> 409 Conflict
    payload_b = b"\x22" * 100
    hash_b = hashlib.sha256(payload_b).hexdigest()
    metadata_b = dict(metadata_a)
    metadata_b["checksum_sha256"] = hash_b

    r2 = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={"metadata": metadata_b, "payload_hex": payload_b.hex()},
    )
    assert r2.status_code == 409
    assert "conflict" in r2.json()["detail"].lower()


def test_pcm_to_wav_packaging_and_header_validity():
    from backend.core.audio_utils import pcm_s16le_to_wav_bytes

    pcm_samples = struct.pack("<4h", 0, 1000, -1000, 32767)
    sample_rate = 16000
    channels = 1

    wav_bytes = pcm_s16le_to_wav_bytes(pcm_samples, sample_rate, channels)

    # Validate using standard Python wave module
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2  # 16 bits
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 4
        read_samples = wf.readframes(4)
        assert read_samples == pcm_samples


def test_pause_and_resume_lifecycle(client: TestClient):
    interview_id = "inv-pause-1"
    _create_recording_interview(client, interview_id)

    # 1. Pause
    r_pause = client.post(f"/api/v1/interviews/{interview_id}/pause")
    assert r_pause.status_code == 200
    assert r_pause.json()["status"] == "paused"

    # Ingestion during PAUSED is allowed for remaining inflight chunks
    payload = b"\x00" * 100
    h = hashlib.sha256(payload).hexdigest()
    r_chunk = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={
            "metadata": {
                "interview_id": interview_id,
                "track_id": "candidate",
                "capture_epoch": 0,
                "sequence": 0,
                "start_time_ms": 0,
                "end_time_ms": 50,
                "sample_rate": 16000,
                "channels": 1,
                "sample_count": 25,
                "format": "pcm_s16le",
                "checksum_sha256": h,
                "size_bytes": 100,
            },
            "payload_hex": payload.hex(),
        },
    )
    assert r_chunk.status_code == 200

    # 2. Resume
    r_resume = client.post(f"/api/v1/interviews/{interview_id}/resume")
    assert r_resume.status_code == 200
    assert r_resume.json()["status"] == "recording"


def test_stop_capture_transitions_to_processing_and_seals(client: TestClient):
    interview_id = "inv-stop-1"
    _create_recording_interview(client, interview_id)

    manifest_inv = {
        "interview_id": interview_id,
        "track_id": "interviewer",
        "capture_epoch": 0,
        "total_chunks": 1,
        "total_duration_ms": 1000,
        "is_sealed": True,
        "gaps": [],
    }
    manifest_cand = {
        "interview_id": interview_id,
        "track_id": "candidate",
        "capture_epoch": 0,
        "total_chunks": 1,
        "total_duration_ms": 1000,
        "is_sealed": True,
        "gaps": [],
    }

    r_stop = client.post(
        f"/api/v1/interviews/{interview_id}/stop",
        json={
            "manifests": [manifest_inv, manifest_cand],
        },
    )
    assert r_stop.status_code == 200
    assert r_stop.json()["status"] == "processing"

    # Direct finalization from PROCESSING is blocked
    r_final = client.post(
        f"/api/v1/interviews/{interview_id}/report/finalize",
        json={
            "summary_markdown": "Test Summary",
            "hiring_recommendation": "Strong Hire",
            "confirmed_by": "Senior Reviewer",
        },
    )
    assert r_final.status_code in (400, 409)
    assert "processing" in r_final.json()["detail"].lower()


def test_interview_jobs_status_reporting(client: TestClient):
    interview_id = "inv-jobs-status-1"
    _create_recording_interview(client, interview_id)

    # Initial state: no jobs -> idle
    r = client.get(f"/api/v1/interviews/{interview_id}/jobs/status")
    assert r.status_code == 200
    data = r.json()
    assert data["interview_id"] == interview_id
    assert data["pending_or_processing"] == 0
    assert data["is_pipeline_idle"] is True

    # Ingest a chunk -> TRANSCRIBE_AUDIO job is automatically enqueued
    pcm_payload = b"\x01\x02" * 16000
    sha256_hash = hashlib.sha256(pcm_payload).hexdigest()
    metadata = {
        "interview_id": interview_id,
        "track_id": "candidate",
        "capture_epoch": 0,
        "sequence": 0,
        "start_time_ms": 0,
        "end_time_ms": 1000,
        "sample_rate": 16000,
        "channels": 1,
        "sample_count": 16000,
        "format": "pcm_s16le",
        "checksum_sha256": sha256_hash,
        "size_bytes": len(pcm_payload),
    }
    client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={"metadata": metadata, "payload_hex": pcm_payload.hex()},
    )

    # Jobs status should reflect pending job
    r2 = client.get(f"/api/v1/interviews/{interview_id}/jobs/status")
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["pending_or_processing"] == 1
    assert data2["is_pipeline_idle"] is False
    assert data2["counts"].get("PENDING") == 1




