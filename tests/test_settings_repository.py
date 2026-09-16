import pytest

from backend.db.database import Database
from backend.db.migrations import run_migrations
from backend.db.repository import Repository, RepositoryConflictError
from contracts.domain import InterviewStatus


@pytest.fixture
def repo(tmp_path):
    db_file = tmp_path / "test.db"
    db = Database(str(db_file))
    db.init_schema()
    return Repository(db)


def test_schema_and_migrations_idempotency(tmp_path):
    db_file = tmp_path / "migration_test.db"
    db = Database(str(db_file))
    db.init_schema()

    # Re-running migrations should be idempotent and succeed
    v = run_migrations(db)
    assert v >= 13
    assert db.verify_integrity()


def test_ai_settings_initial_state_none(repo):
    settings = repo.get_ai_settings()
    assert settings is None
    assert repo.get_ai_settings_update_blocker() is None


def test_ai_settings_first_write_creates_revision_1(repo):
    trans_conf = {"provider_id": "routerai", "model_id": "gpt-transcribe"}
    text_conf = {"provider_id": "routerai", "model_id": "qwen2.5-72b"}

    result = repo.update_ai_settings(
        expected_revision=0,
        transcription_config=trans_conf,
        text_analysis_config=text_conf,
    )
    assert result["revision"] == 1
    assert result["transcription_config"] == trans_conf
    assert result["text_analysis_config"] == text_conf

    saved = repo.get_ai_settings()
    assert saved is not None
    assert saved["revision"] == 1
    assert saved["transcription_config"] == trans_conf


def test_ai_settings_subsequent_write_increments_revision(repo):
    trans_conf = {"provider_id": "routerai", "model_id": "m1"}
    text_conf = {"provider_id": "routerai", "model_id": "m2"}

    repo.update_ai_settings(0, trans_conf, text_conf)

    trans_conf_2 = {"provider_id": "custom", "model_id": "whisper"}
    result_2 = repo.update_ai_settings(1, trans_conf_2, text_conf)
    assert result_2["revision"] == 2

    saved = repo.get_ai_settings()
    assert saved["revision"] == 2
    assert saved["transcription_config"]["provider_id"] == "custom"


def test_ai_settings_occ_conflict_raises_error(repo):
    trans_conf = {"provider_id": "routerai", "model_id": "m1"}
    text_conf = {"provider_id": "routerai", "model_id": "m2"}

    repo.update_ai_settings(0, trans_conf, text_conf)

    # Wrong expected revision (e.g. 0 when revision is 1)
    with pytest.raises(RepositoryConflictError, match="expected revision 0, but current revision is 1"):
        repo.update_ai_settings(0, trans_conf, text_conf)

    # Wrong expected revision in the future
    with pytest.raises(RepositoryConflictError, match="expected revision 5, but current revision is 1"):
        repo.update_ai_settings(5, trans_conf, text_conf)


def test_ai_settings_blocked_by_recording_or_paused_interview(repo):
    repo.create_interview("inv-rec", "Go Dev", "Alice", "Developer", status=InterviewStatus.DRAFT)

    trans_conf = {"provider_id": "routerai", "model_id": "m1"}
    text_conf = {"provider_id": "routerai", "model_id": "m2"}

    # In DRAFT status, update is allowed
    assert repo.get_ai_settings_update_blocker() is None
    repo.update_ai_settings(0, trans_conf, text_conf)

    # Change to RECORDING
    repo.update_interview_status("inv-rec", InterviewStatus.RECORDING)
    blocker = repo.get_ai_settings_update_blocker()
    assert blocker is not None
    assert "recording" in blocker.lower()

    with pytest.raises(RepositoryConflictError, match="recording"):
        repo.update_ai_settings(1, trans_conf, text_conf)

    # Change to PAUSED
    repo.update_interview_status("inv-rec", InterviewStatus.PAUSED)
    blocker_paused = repo.get_ai_settings_update_blocker()
    assert blocker_paused is not None
    assert "paused" in blocker_paused.lower()

    with pytest.raises(RepositoryConflictError, match="paused"):
        repo.update_ai_settings(1, trans_conf, text_conf)

    # Change to REVIEW -> now allowed
    repo.update_interview_status("inv-rec", InterviewStatus.REVIEW)
    assert repo.get_ai_settings_update_blocker() is None
    res = repo.update_ai_settings(1, trans_conf, text_conf)
    assert res["revision"] == 2


def test_ai_settings_blocked_by_pending_or_processing_job(repo):
    repo.create_interview("inv-job", "Go Dev", "Bob", "Developer", status=InterviewStatus.REVIEW)

    job_id = "job-blocker-1"
    repo.enqueue_job(
        job_id=job_id,
        job_type="EVALUATE_QUESTION",
        interview_id="inv-job",
        payload={"question_id": "q1"},
    )
    # Enqueued job is PENDING
    blocker = repo.get_ai_settings_update_blocker()
    assert blocker is not None
    assert "PENDING" in blocker

    trans_conf = {"provider_id": "routerai", "model_id": "m1"}
    text_conf = {"provider_id": "routerai", "model_id": "m2"}

    with pytest.raises(RepositoryConflictError, match="PENDING"):
        repo.update_ai_settings(0, trans_conf, text_conf)

    # Claim job -> PROCESSING
    claimed = repo.claim_next_job(lock_duration_sec=30)
    assert claimed is not None
    assert claimed["id"] == job_id

    conn = repo.db.get_connection()
    try:
        j_row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert j_row["status"] == "PROCESSING"
    finally:
        conn.close()

    blocker_proc = repo.get_ai_settings_update_blocker()
    assert blocker_proc is not None
    assert "PROCESSING" in blocker_proc

    with pytest.raises(RepositoryConflictError, match="PROCESSING"):
        repo.update_ai_settings(0, trans_conf, text_conf)

    # Complete job -> COMPLETED
    repo.complete_job(job_id=job_id, owner_token=claimed["locked_by"])
    assert repo.get_ai_settings_update_blocker() is None

    # Now update succeeds
    res = repo.update_ai_settings(0, trans_conf, text_conf)
    assert res["revision"] == 1
