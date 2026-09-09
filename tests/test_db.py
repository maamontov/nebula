import pytest
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.domain import InterviewStatus


@pytest.fixture
def repo(tmp_path):
    db_file = tmp_path / "test.db"
    db = Database(str(db_file))
    db.init_schema()
    return Repository(db)


def test_interview_lifecycle(repo):
    # 1. Create
    inv = repo.create_interview(
        interview_id="inv-101",
        title="Senior Backend Go",
        candidate_name="Алексей Смирнов",
        role="Backend Tech Lead",
        status=InterviewStatus.DRAFT,
    )
    assert inv["id"] == "inv-101"
    assert inv["status"] == "draft"
    assert inv["candidate_name"] == "Алексей Смирнов"

    # 2. Update to RECORDING with consent
    repo.update_interview_status(
        interview_id="inv-101",
        new_status=InterviewStatus.RECORDING,
        consent_confirmed_at="2026-09-10T00:00:00Z",
        consent_version="1.0-ru",
    )
    updated = repo.get_interview("inv-101")
    assert updated["status"] == "recording"
    assert updated["consent_confirmed_at"] == "2026-09-10T00:00:00Z"

    # 3. List
    all_invs = repo.list_interviews()
    assert len(all_invs) == 1
    assert all_invs[0]["id"] == "inv-101"


def test_interview_plan_versioning(repo):
    repo.create_interview("inv-201", "QA Lead", "Мария", "QA")

    repo.save_plan("plan-1", "inv-201", {"questions": [{"id": "q1", "text": "Test plan"}]}, version=1)
    plan_v1 = repo.get_latest_plan("inv-201")
    assert plan_v1["version"] == 1
    assert plan_v1["payload"]["questions"][0]["id"] == "q1"

    # Update to version 2
    repo.save_plan("plan-2", "inv-201", {"questions": [{"id": "q1"}, {"id": "q2"}]}, version=2)
    plan_v2 = repo.get_latest_plan("inv-201")
    assert plan_v2["version"] == 2
    assert len(plan_v2["payload"]["questions"]) == 2


def test_transcript_segments_ordering(repo):
    repo.create_interview("inv-301", "Frontend", "Иван", "Frontend")

    repo.add_transcript_segment("seg-2", "inv-301", "candidate", 2000, 4000, "Вторая реплика")
    repo.add_transcript_segment("seg-1", "inv-301", "interviewer", 0, 1800, "Первая реплика")

    segments = repo.get_transcript_segments("inv-301")
    assert len(segments) == 2
    assert segments[0]["id"] == "seg-1"
    assert segments[1]["id"] == "seg-2"


def test_assessment_proposals_and_review(repo):
    repo.create_interview("inv-401", "DevOps", "Олег", "DevOps")

    repo.save_assessment_proposal(
        proposal_id="prop-1",
        interview_id="inv-401",
        question_id="q-k8s",
        model_profile_id="gemini-3.8-flash",
        scores=[{"criterion_id": "c1", "score": 4.0, "explanation": "Хорошо", "evidence": []}],
        critical_errors=[],
    )

    props = repo.get_assessment_proposals("inv-401")
    assert len(props) == 1
    assert props[0]["is_approved"] == 0
    assert props[0]["scores"][0]["score"] == 4.0

    # Human review & approval
    repo.approve_assessment(
        proposal_id="prop-1",
        reviewed_scores=[{"criterion_id": "c1", "score": 5.0, "explanation": "Отлично после уточнения", "evidence": []}],
        reviewer_notes="Кандидат детально раскрыл тему при уточнении",
    )

    approved_props = repo.get_assessment_proposals("inv-401")
    assert approved_props[0]["is_approved"] == 1
    assert approved_props[0]["reviewed_scores"][0]["score"] == 5.0
    assert approved_props[0]["reviewer_notes"] == "Кандидат детально раскрыл тему при уточнении"


def test_durable_jobs_lifecycle(repo):
    repo.create_interview("inv-501", "Data Eng", "Дмитрий", "Data")

    repo.enqueue_job("job-1", "TRANSCRIBE_AUDIO", "inv-501", {"track": "candidate"}, max_attempts=2)

    # 1. Claim
    claimed = repo.claim_next_job(lock_duration_sec=30)
    assert claimed is not None
    assert claimed["id"] == "job-1"
    assert claimed["attempts"] == 1

    # While locked, no more eligible jobs
    assert repo.claim_next_job() is None

    # 2. Transient fail -> re-enqueued because attempts < max_attempts
    repo.fail_job("job-1", "Network timeout")
    retried = repo.claim_next_job(lock_duration_sec=30)
    assert retried is not None
    assert retried["id"] == "job-1"
    assert retried["attempts"] == 2

    # 3. Fail again -> reaches max_attempts=2 -> FAILED status
    repo.fail_job("job-1", "Final error")
    assert repo.claim_next_job() is None  # No jobs left
