"""
Repository and Schema tests for Turn Assembler and Migration 009.
Тесты репозитория и схемы для сборщика реплик и миграции 009.
"""
import hashlib
import json

import pytest

from backend.core.turn_assembler import AssembledTurn
from backend.db.database import Database
from backend.db.migrations import get_current_migration_version, run_migrations
from backend.db.repository import Repository


def _setup_interview(repo: Repository, interview_id: str = "inv-turn-repo"):
    return repo.create_interview(
        interview_id=interview_id,
        title="Test Turn Assembly",
        candidate_name="Alexey",
        role="Senior Python Engineer",
    )


def test_migration_009_applied_and_idempotent(tmp_path):
    db_path = str(tmp_path / "test_mig_009.db")
    db = Database(db_path)
    # Initialize schema first, which runs schema.sql and then run_migrations
    db.init_schema()

    conn = db.get_connection()
    try:
        assert get_current_migration_version(conn) >= 9
        # Check transcript_assembly_state table exists
        cols = [
            r["name"]
            for r in conn.execute("PRAGMA table_info(transcript_assembly_state)").fetchall()
        ]
        assert "interview_id" in cols
        assert "track_id" in cols
        assert "capture_epoch" in cols
        assert "next_sequence" in cols
        assert "next_sample_offset" in cols
    finally:
        conn.close()

    # Re-run for idempotency
    ver2 = run_migrations(db)
    assert ver2 >= 9


def test_turn_assembly_cursor_and_commit(tmp_path):
    db_path = str(tmp_path / "test_cursor.db")
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)

    interview_id = "inv-cursor-1"
    _setup_interview(repo, interview_id)

    # Initial cursor is (0, 0)
    seq, off = repo.get_turn_assembly_cursor(interview_id, "shared", 1)
    assert seq == 0
    assert off == 0

    # Commit a turn
    turn1 = AssembledTurn(
        track_id="shared",
        capture_epoch=1,
        first_sequence=0,
        first_sample_offset=1600,
        last_sequence=2,
        last_sample_offset=8000,
        start_ms=100,
        end_ms=2500,
    )
    job_ids = repo.commit_turn_assembly_results(
        interview_id=interview_id,
        track_id="shared",
        capture_epoch=1,
        next_sequence=2,
        next_sample_offset=8000,
        turns=[turn1],
    )
    assert len(job_ids) == 1
    assert "stt-turn-" in job_ids[0]

    # Verify updated cursor
    seq2, off2 = repo.get_turn_assembly_cursor(interview_id, "shared", 1)
    assert seq2 == 2
    assert off2 == 8000

    # Verify job in DB
    with repo.db.transaction() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE interview_id = ?", (interview_id,)).fetchall()
        turn_jobs = [dict(r) for r in rows if r["type"] == "TRANSCRIBE_TURN"]

    assert len(turn_jobs) == 1
    p = json.loads(turn_jobs[0]["payload_json"])
    assert p["track_id"] == "shared"
    assert p["first_sequence"] == 0
    assert p["last_sequence"] == 2

    # Idempotent re-commit does not duplicate jobs
    job_ids_repeat = repo.commit_turn_assembly_results(
        interview_id=interview_id,
        track_id="shared",
        capture_epoch=1,
        next_sequence=2,
        next_sample_offset=8000,
        turns=[turn1],
    )
    assert len(job_ids_repeat) == 0


def test_get_turn_audio_pcm_reconstruction(tmp_path):
    db_path = str(tmp_path / "test_pcm_recon.db")
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)

    interview_id = "inv-pcm-recon"
    _setup_interview(repo, interview_id)

    # Create 3 chunks of 1000ms each (16000 samples = 32000 bytes each)
    spool_dir = tmp_path / "spool"
    chunk_bytes_list = []
    for seq in range(3):
        raw_bytes = bytes([(seq * 10 + i) % 256 for i in range(32000)])
        chunk_bytes_list.append(raw_bytes)
        sha = hashlib.sha256(raw_bytes).hexdigest()
        repo.save_audio_chunk(
            interview_id=interview_id,
            track_id="shared",
            capture_epoch=1,
            sequence=seq,
            start_time_ms=seq * 1000,
            end_time_ms=(seq + 1) * 1000,
            sample_rate=16000,
            channels=1,
            sample_count=16000,
            format_str="pcm_s16le",
            checksum_sha256=sha,
            payload_bytes=raw_bytes,
            spool_base_dir=spool_dir,
        )

    # Reconstruct turn spanning from seq 0 (sample 1000) to seq 2 (sample 4000)
    # In chunk 0: samples 1000..16000 (bytes 2000..32000)
    # In chunk 1: samples 0..16000 (bytes 0..32000)
    # In chunk 2: samples 0..4000 (bytes 0..8000)
    expected_bytes = (
        chunk_bytes_list[0][2000:32000]
        + chunk_bytes_list[1]
        + chunk_bytes_list[2][0:8000]
    )

    reconstructed = repo.get_turn_audio_pcm(
        interview_id=interview_id,
        track_id="shared",
        capture_epoch=1,
        first_sequence=0,
        first_sample_offset=1000,
        last_sequence=2,
        last_sample_offset=4000,
    )
    assert reconstructed == expected_bytes
    assert len(reconstructed) == (15000 + 16000 + 4000) * 2


def test_get_turn_audio_pcm_missing_sequence_raises(tmp_path):
    db_path = str(tmp_path / "test_pcm_missing.db")
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)

    interview_id = "inv-pcm-missing"
    _setup_interview(repo, interview_id)
    spool_dir = tmp_path / "spool"

    # Save seq 0 and seq 2 (seq 1 is missing)
    for seq in [0, 2]:
        raw = b"\x00" * 32000
        sha = hashlib.sha256(raw).hexdigest()
        repo.save_audio_chunk(
            interview_id=interview_id,
            track_id="shared",
            capture_epoch=1,
            sequence=seq,
            start_time_ms=seq * 1000,
            end_time_ms=(seq + 1) * 1000,
            sample_rate=16000,
            channels=1,
            sample_count=16000,
            format_str="pcm_s16le",
            checksum_sha256=sha,
            payload_bytes=raw,
            spool_base_dir=spool_dir,
        )

    with pytest.raises(ValueError, match="Missing audio chunk sequences"):
        repo.get_turn_audio_pcm(
            interview_id=interview_id,
            track_id="shared",
            capture_epoch=1,
            first_sequence=0,
            first_sample_offset=0,
            last_sequence=2,
            last_sample_offset=16000,
        )
