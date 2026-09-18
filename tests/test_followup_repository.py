
import pytest

from backend.db.database import Database
from backend.db.repository import Repository, RepositoryConflictError
from contracts.domain import (
    InterviewStatus,
)


@pytest.fixture
def repo(tmp_path):
    db_file = tmp_path / "followup_repo_test.db"
    db = Database(str(db_file))
    db.init_schema()
    return Repository(db)


def _setup_interview(repo: Repository, interview_id: str = "inv-test-1") -> str:
    repo.create_interview(
        interview_id=interview_id,
        title="Python Interview",
        candidate_name="Алексей",
        role="Backend Engineer",
        status=InterviewStatus.RECORDING,
    )
    plan = {
        "id": f"plan-{interview_id}",
        "title": "Backend Plan",
        "role": "Backend Engineer",
        "candidate_name": "Алексей",
        "questions": [
            {
                "id": "q1",
                "title": "GIL & Concurrency",
                "prompt": "Tell me about GIL",
                "criteria": [{"id": "c1", "title": "GIL", "description": "GIL details", "min_score": 1, "max_score": 5, "weight": 1.0}],
            }
        ],
    }
    repo.save_plan(f"plan-{interview_id}", interview_id, plan, version=1)
    return interview_id


def test_create_followup_request_and_deduplication(repo: Repository):
    interview_id = _setup_interview(repo)

    req1, is_new1, wait1, cd1 = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="auto",
        candidate_fingerprint="fp-111",
        context_hash="hash-abc-123",
        context_json='{"test": 1}',
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
    )

    assert is_new1 is True
    assert req1 is not None
    assert req1["job_id"] is not None
    assert req1["outcome"] is None

    # Second call with the EXACT SAME context_hash must be deduplicated
    req2, is_new2, wait2, cd2 = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="auto",
        candidate_fingerprint="fp-111",
        context_hash="hash-abc-123",
        context_json='{"test": 1}',
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
    )

    assert is_new2 is False
    assert req2["id"] == req1["id"]


def test_followup_job_claiming_and_lease(repo: Repository):
    interview_id = _setup_interview(repo)

    req, is_new, _, _ = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        candidate_fingerprint="fp-claim",
        context_hash="hash-claim-1",
        context_json='{"test": 1}',
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
    )
    assert is_new is True

    # Main worker consumer excludes GENERATE_FOLLOWUPS
    main_claimed = repo.claim_next_job(lock_duration_sec=30, exclude_types=["GENERATE_FOLLOWUPS"])
    assert main_claimed is None

    # Dedicated followup worker claims GENERATE_FOLLOWUPS
    followup_claimed = repo.claim_next_job(lock_duration_sec=30, include_types=["GENERATE_FOLLOWUPS"])
    assert followup_claimed is not None
    assert followup_claimed["id"] == req["job_id"]
    assert followup_claimed["type"] == "GENERATE_FOLLOWUPS"


def test_save_suggestions_and_occ_patch(repo: Repository):
    interview_id = _setup_interview(repo)

    req, is_new, _, _ = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        candidate_fingerprint="fp-sug",
        context_hash="hash-sug-1",
        context_json='{"test": 1}',
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
    )

    job = repo.claim_next_job(lock_duration_sec=30, include_types=["GENERATE_FOLLOWUPS"])
    assert job is not None

    sug1 = {
        "kind": "clarify",
        "question_text": "Уточните, как вы обходили GIL?",
        "purpose": "Проверка понимания multiprocessing.",
        "criterion_ids": ["c1"],
        "source_refs": [{"segment_id": "seg-1", "exact_quote": "обходил GIL"}],
    }
    sug2 = {
        "kind": "deepen",
        "question_text": "Какие накладные расходы у IPC в вашем решении?",
        "purpose": "Проверка компромиссов IPC.",
        "criterion_ids": ["c1"],
        "source_refs": [{"segment_id": "seg-2", "exact_quote": "использовал IPC"}],
    }

    repo.save_followup_suggestions(
        job_id=job["id"],
        request_id=req["id"],
        owner_token=job["locked_by"],
        suggestions=[sug1, sug2],
        outcome="success",
    )

    # Verify state response
    state = repo.get_followup_state(interview_id, "q1", "probe")
    assert len(state["suggestions"]) == 2
    sug1_id = state["suggestions"][0]["id"]

    # OCC Patch: First update with expected_decision_version=1 succeeds
    patched = repo.update_suggestion_decision(
        interview_id=interview_id,
        suggestion_id=sug1_id,
        status="asked",
        asked_text="Как конкретно вы обходили блокировку GIL?",
        expected_decision_version=1,
    )
    assert patched is not None
    assert patched["status"] == "asked"
    assert patched["decision_version"] == 2
    assert patched["asked_text"] == "Как конкретно вы обходили блокировку GIL?"

    # OCC Patch: Conflicting update with stale version=1 must raise RepositoryConflictError
    with pytest.raises(RepositoryConflictError):
        repo.update_suggestion_decision(
            interview_id=interview_id,
            suggestion_id=sug1_id,
            status="dismissed",
            asked_text=None,
            expected_decision_version=1,
        )

    # History of asked followups
    asked_history = repo.get_asked_followup_history(interview_id)
    assert len(asked_history) == 1
    assert asked_history[0]["id"] == sug1_id


def test_retry_followup_request(repo: Repository):
    interview_id = _setup_interview(repo)

    req, _, _, _ = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        candidate_fingerprint="fp-retry",
        context_hash="hash-retry-1",
        context_json='{"test": 1}',
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
    )

    # Simulate failure
    repo.fail_job(req["job_id"], "LLM timeout", is_terminal=True)
    with repo.db.transaction() as conn:
        conn.execute("UPDATE followup_requests SET outcome = 'error' WHERE id = ?", (req["id"],))

    # Retry
    retried_req = repo.retry_followup_request(interview_id, req["id"])
    assert retried_req is not None
    assert retried_req["outcome"] is None
    assert retried_req["job_id"] is not None


def test_finalization_includes_followup_questions_in_snapshot(repo: Repository):
    interview_id = _setup_interview(repo)

    req, _, _, _ = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        candidate_fingerprint="fp-fin",
        context_hash="hash-fin-1",
        context_json='{"test": 1}',
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
    )
    job = repo.claim_next_job(lock_duration_sec=30, include_types=["GENERATE_FOLLOWUPS"])
    sug = {
        "kind": "guide",
        "question_text": "Подумайте про пул процессов.",
        "purpose": "Наводка.",
        "criterion_ids": ["c1"],
        "source_refs": [],
    }
    repo.save_followup_suggestions(
        job_id=job["id"],
        request_id=req["id"],
        owner_token=job["locked_by"],
        suggestions=[sug],
        outcome="success",
    )

    state = repo.get_followup_state(interview_id, "q1", "probe")
    sug_id = state["suggestions"][0]["id"]
    repo.update_suggestion_decision(
        interview_id=interview_id,
        suggestion_id=sug_id,
        status="asked",
        asked_text="Подумайте про пул процессов.",
        expected_decision_version=1,
    )

    # Set status to review
    repo.update_interview_status(interview_id, InterviewStatus.REVIEW)

    # Save human assessment for question q1
    repo.save_human_assessment(
        assessment_id=f"ha-{interview_id}-q1",
        interview_id=interview_id,
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        scores=[{"criterion_id": "c1", "score": 4.0}],
        reviewer_id="lead-interviewer",
    )

    # Finalize interview
    res = repo.finalize_interview(
        interview_id=interview_id,
        summary_markdown="# Отчет",
        hiring_recommendation="HIRE",
        confirmed_by="lead-interviewer",
    )
    assert res["status"] == "finalized"
    snapshot = res["snapshot"]
    assert "followup_questions" in snapshot
    assert len(snapshot["followup_questions"]) == 1
    assert snapshot["followup_questions"][0]["id"] == sug_id
    assert snapshot["followup_questions"][0]["kind"] == "guide"


def test_cascade_delete_followup_data(repo: Repository):
    interview_id = _setup_interview(repo)

    req, _, _, _ = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        candidate_fingerprint="fp-del",
        context_hash="hash-del-1",
        context_json='{"test": 1}',
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
    )
    job = repo.claim_next_job(lock_duration_sec=30, include_types=["GENERATE_FOLLOWUPS"])
    sug = {
        "kind": "clarify",
        "question_text": "Тестовый вопрос",
        "purpose": "Тест",
        "criterion_ids": ["c1"],
        "source_refs": [],
    }
    repo.save_followup_suggestions(
        job_id=job["id"],
        request_id=req["id"],
        owner_token=job["locked_by"],
        suggestions=[sug],
        outcome="success",
    )

    # Delete interview
    repo.delete_interview(interview_id)

    with repo.db.transaction() as conn:
        req_count = conn.execute("SELECT COUNT(*) FROM followup_requests WHERE interview_id = ?", (interview_id,)).fetchone()[0]
        sug_count = conn.execute("SELECT COUNT(*) FROM followup_suggestions WHERE interview_id = ?", (interview_id,)).fetchone()[0]
        assert req_count == 0
        assert sug_count == 0
