import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.domain import InterviewStatus


@pytest.fixture
def test_setup(tmp_path):
    db_path = tmp_path / "stage5.db"
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)

    app.dependency_overrides[get_repository] = lambda: repo
    client = TestClient(app)

    interview_id = "inv-stage5-1"
    plan = {
        "title": "Stage 5 Interview",
        "questions": [
            {
                "id": "q1",
                "title": "Event Loop",
                "prompt": "Расскажите про Event Loop в Python",
                "criteria": [
                    {
                        "id": "c1",
                        "title": "Event Loop",
                        "description": "Понимание async/await и селекторов",
                        "min_score": 1,
                        "max_score": 5,
                        "weight": 1.0,
                    }
                ],
            },
            {
                "id": "q2",
                "title": "Databases",
                "prompt": "Расскажите про ACID",
                "criteria": [
                    {
                        "id": "c2",
                        "title": "ACID",
                        "description": "Транзакции и изоляция",
                        "min_score": 1,
                        "max_score": 5,
                        "weight": 1.0,
                    }
                ],
            },
        ],
    }
    repo.create_interview(
        interview_id=interview_id,
        title="Software Engineer",
        candidate_name="Алексей",
        role="Backend",
        status=InterviewStatus.RECORDING,
    )
    repo.save_plan(f"plan-{interview_id}", interview_id, plan, version=1)

    yield repo, client, interview_id

    app.dependency_overrides.clear()


def test_evaluate_question_server_dedup_same_context(test_setup):
    """Verifies that duplicate enqueue of EVALUATE_QUESTION for the same context returns existing job without duplicating."""
    repo, client, interview_id = test_setup

    # 1. First enqueue
    payload1 = {
        "id": "job-eval-1",
        "type": "EVALUATE_QUESTION",
        "payload": {
            "question_id": "q1",
            "rubric_description": "Event Loop",
        },
    }
    res1 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=payload1)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "enqueued"
    assert data1["job_id"] == "job-eval-1"
    assert data1.get("existing") is False

    # 2. Duplicate enqueue with different client job id
    payload2 = {
        "id": "job-eval-2",
        "type": "EVALUATE_QUESTION",
        "payload": {
            "question_id": "q1",
            "rubric_description": "Event Loop",
        },
    }
    res2 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=payload2)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "already_queued"
    assert data2["job_id"] == "job-eval-1"
    assert data2.get("existing") is True

    # Check repository has only 1 job
    st = repo.get_interview_jobs_status(interview_id)
    assert st["counts"].get("PENDING") == 1
    assert len(st.get("active_jobs", [])) == 1


def test_new_candidate_speech_triggers_new_job(test_setup):
    """Verifies that new candidate speech updates candidate_fingerprint and permits a new job."""
    repo, client, interview_id = test_setup

    # Initial speech from candidate
    repo.add_transcript_segment(
        segment_id="seg-cand-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5000,
        text="Event Loop использует селекторы ОС для неблокирующего ввода-вывода.",
        is_final=True,
        speaker_role="candidate",
        revision_id="trans-rev-1",
    )
    repo.save_association(
        assoc_id="assoc-1",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-cand-1",
        confidence=1.0,
        is_ambiguous=False,
        is_manually_adjusted=False,
        revision_id="trans-rev-1",
    )

    # First enqueue with candidate speech
    p1 = {
        "id": "job-q1-v1",
        "type": "EVALUATE_QUESTION",
        "payload": {"question_id": "q1"},
    }
    res1 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=p1)
    assert res1.status_code == 200
    assert res1.json()["job_id"] == "job-q1-v1"
    assert res1.json()["status"] == "enqueued"

    # Candidate speaks more on the question
    repo.add_transcript_segment(
        segment_id="seg-cand-2",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=5100,
        end_time_ms=9000,
        text="Также он управляет очередью задач и микрозадач в asyncio.",
        is_final=True,
        speaker_role="candidate",
        revision_id="trans-rev-1",
    )
    repo.save_association(
        assoc_id="assoc-2",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-cand-2",
        confidence=1.0,
        is_ambiguous=False,
        is_manually_adjusted=False,
        revision_id="trans-rev-1",
    )

    # Second enqueue should NOT be deduplicated because fingerprint changed
    p2 = {
        "id": "job-q1-v2",
        "type": "EVALUATE_QUESTION",
        "payload": {"question_id": "q1"},
    }
    res2 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=p2)
    assert res2.status_code == 200
    assert res2.json()["job_id"] == "job-q1-v2"
    assert res2.json()["status"] == "enqueued"
    assert res2.json().get("existing") is False

    # Check repository has 2 pending jobs
    st = repo.get_interview_jobs_status(interview_id)
    assert st["counts"].get("PENDING") == 2


def test_interviewer_and_unknown_speech_do_not_alter_candidate_fingerprint(test_setup):
    """Verifies interviewer speech and unverified shared track speech do not alter candidate fingerprint."""
    repo, client, interview_id = test_setup

    repo.add_transcript_segment(
        segment_id="seg-cand-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5000,
        text="Я использую asyncio.gather.",
        is_final=True,
        speaker_role="candidate",
        revision_id="trans-rev-1",
    )
    repo.save_association(
        assoc_id="assoc-1",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-cand-1",
        confidence=1.0,
        is_ambiguous=False,
        is_manually_adjusted=False,
        revision_id="trans-rev-1",
    )

    p1 = {
        "id": "job-q1-orig",
        "type": "EVALUATE_QUESTION",
        "payload": {"question_id": "q1"},
    }
    res1 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=p1)
    assert res1.json()["status"] == "enqueued"

    # Now interviewer speaks
    repo.add_transcript_segment(
        segment_id="seg-int-1",
        interview_id=interview_id,
        track_id="interviewer",
        start_time_ms=5500,
        end_time_ms=8000,
        text="Отлично, а что насчет обработки исключений?",
        is_final=True,
        speaker_role="interviewer",
        revision_id="trans-rev-1",
    )
    repo.save_association(
        assoc_id="assoc-int-1",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-int-1",
        confidence=1.0,
        is_ambiguous=False,
        is_manually_adjusted=False,
        revision_id="trans-rev-1",
    )

    # Also a shared track speech with unknown role
    repo.add_transcript_segment(
        segment_id="seg-shared-1",
        interview_id=interview_id,
        track_id="shared",
        start_time_ms=8100,
        end_time_ms=9000,
        text="[неразборчивый шум]",
        is_final=True,
        speaker_role="unknown",
        revision_id="trans-rev-1",
    )
    repo.save_association(
        assoc_id="assoc-shared-1",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-shared-1",
        confidence=0.8,
        is_ambiguous=True,
        is_manually_adjusted=False,
        revision_id="trans-rev-1",
    )

    # Enqueue again: neither interviewer nor unknown should alter candidate fingerprint!
    p2 = {
        "id": "job-q1-dup",
        "type": "EVALUATE_QUESTION",
        "payload": {"question_id": "q1"},
    }
    res2 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=p2)
    assert res2.status_code == 200
    assert res2.json()["status"] == "already_queued"
    assert res2.json()["job_id"] == "job-q1-orig"


def test_failed_job_allows_retry(test_setup):
    """Verifies that when a job transitions to FAILED, subsequent enqueue request succeeds (permitting retry)."""
    repo, client, interview_id = test_setup

    p1 = {
        "id": "job-failed-1",
        "type": "EVALUATE_QUESTION",
        "payload": {"question_id": "q1"},
    }
    res1 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=p1)
    assert res1.json()["status"] == "enqueued"

    # Lease and fail the job
    leased = repo.claim_next_job(include_types=["EVALUATE_QUESTION"])
    assert leased is not None
    repo.fail_job(leased["id"], "Provider timeout 504", owner_token=leased["locked_by"], is_terminal=True)

    # Now client requests retry
    p2 = {
        "id": "job-retry-1",
        "type": "EVALUATE_QUESTION",
        "payload": {"question_id": "q1"},
    }
    res2 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=p2)
    assert res2.status_code == 200
    assert res2.json()["status"] == "enqueued"
    assert res2.json()["job_id"] == "job-retry-1"
    assert res2.json().get("existing") is False


def test_jobs_status_api_filter_and_telemetry(test_setup):
    """Verifies /api/v1/interviews/{id}/jobs/status endpoint with job_ids filter and telemetry fields."""
    repo, client, interview_id = test_setup

    # Enqueue q1 and q2
    res_q1 = client.post(
        f"/api/v1/interviews/{interview_id}/jobs/enqueue",
        json={"id": "job-track-q1", "type": "EVALUATE_QUESTION", "payload": {"question_id": "q1"}},
    )
    assert res_q1.status_code == 200
    res_q2 = client.post(
        f"/api/v1/interviews/{interview_id}/jobs/enqueue",
        json={"id": "job-track-q2", "type": "EVALUATE_QUESTION", "payload": {"question_id": "q2"}},
    )
    assert res_q2.status_code == 200

    # Query status filtering only by job-track-q1
    resp = client.get(
        f"/api/v1/interviews/{interview_id}/jobs/status",
        params={"job_ids": ["job-track-q1"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["interview_id"] == interview_id
    assert data["pending_or_processing"] == 2
    active_jobs = data["active_jobs"]
    assert len(active_jobs) == 1
    assert active_jobs[0]["id"] == "job-track-q1"
    assert active_jobs[0]["question_id"] == "q1"
    assert active_jobs[0]["status"] == "PENDING"
    assert "queue_wait_sec" in active_jobs[0]


def test_revision_change_allows_new_job(test_setup):
    """Verifies that changing transcript or rubric revision permits a new evaluation job."""
    repo, client, interview_id = test_setup

    p1 = {
        "id": "job-rev-1",
        "type": "EVALUATE_QUESTION",
        "payload": {
            "question_id": "q1",
            "transcript_revision_id": "trans-rev-1",
            "rubric_revision_id": "rub-rev-1",
        },
    }
    res1 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=p1)
    assert res1.status_code == 200
    assert res1.json()["status"] == "enqueued"

    # Same context with new transcript revision
    p2 = {
        "id": "job-rev-2",
        "type": "EVALUATE_QUESTION",
        "payload": {
            "question_id": "q1",
            "transcript_revision_id": "trans-rev-2",
            "rubric_revision_id": "rub-rev-1",
        },
    }
    res2 = client.post(f"/api/v1/interviews/{interview_id}/jobs/enqueue", json=p2)
    assert res2.status_code == 200
    assert res2.json()["status"] == "enqueued"
    assert res2.json()["job_id"] == "job-rev-2"
    assert res2.json().get("existing") is False
