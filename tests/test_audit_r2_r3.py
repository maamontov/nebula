"""
Tests for R2 and R3: Transcript revision integrity, role assignment, evidence scope.
Тесты для R2 и R3: Целостность ревизий стенограммы, роли спикеров и область evidence.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker
from contracts.domain import (
    InterviewStatus,
)


@pytest.fixture
def r2_r3_env(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    repo = Repository(db)

    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as client:
        yield {
            "client": client,
            "repo": repo,
            "tmp_path": tmp_path,
        }
    app.dependency_overrides.clear()


def test_r2_role_change_creates_revision_and_invalidates_assessments(r2_r3_env):
    """
    R2: Changing speaker_role must create a new transcript revision,
    preserve previous revision segments, and mark dependent proposals/assessments as stale.
    """
    client = r2_r3_env["client"]
    repo = r2_r3_env["repo"]

    interview_id = "inv-r2-test"
    repo.create_interview(
        interview_id=interview_id,
        title="R2 Test",
        candidate_name="Bob",
        role="Backend",
        capture_mode="dual_source",
    )
    repo.update_interview_status(interview_id, InterviewStatus.REVIEW)

    # 1. Add transcript segment in trans-rev-1
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5000,
        text="Я использовал PostgreSQL для шардирования.",
        speaker_role="candidate",
        revision_id="trans-rev-1",
    )

    # 2. Associate with q1
    repo.save_association(
        assoc_id="assoc-1",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-1",
        revision_id="trans-rev-1",
    )

    # 3. Save AI proposal and Human assessment for q1 on trans-rev-1
    repo.save_assessment_proposal(
        proposal_id="prop-1",
        interview_id=interview_id,
        question_id="q1",
        model_profile_id="gpt-4o",
        scores=[{"criterion_id": "c1", "score": 4.0, "evidence": [{"segment_id": "seg-1", "exact_quote": "Я использовал PostgreSQL"}]}],
        transcript_revision_id="trans-rev-1",
    )
    repo.save_human_assessment(
        assessment_id=f"ha-{interview_id}-q1",
        interview_id=interview_id,
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        scores=[{"criterion_id": "c1", "score": 4.0}],
    )

    # 4. Change role of seg-1: candidate -> interviewer
    res = client.post(
        f"/api/v1/interviews/{interview_id}/segments/seg-1/speaker-role",
        json={"speaker_role": "interviewer", "expected_revision_id": "trans-rev-1"},
    )
    assert res.status_code == 200

    inv = repo.get_interview(interview_id)
    new_rev = inv["active_transcript_revision_id"]
    assert new_rev != "trans-rev-1", "A new revision must be created upon role change"

    # Verify old revision is preserved
    old_segs = repo.get_transcript_segments(interview_id, revision_id="trans-rev-1")
    assert len(old_segs) == 1
    assert old_segs[0]["speaker_role"] == "candidate", "Old revision segment must remain untouched"

    # Verify new revision has updated role
    new_segs = repo.get_transcript_segments(interview_id, revision_id=new_rev)
    assert len(new_segs) == 1
    assert new_segs[0]["speaker_role"] == "interviewer"

    # Verify dependent proposals and human assessments are marked stale!
    props = repo.get_assessment_proposals(interview_id)
    assert len(props) == 1
    assert props[0]["is_stale"] == 1, "Proposal must be marked stale after evidence segment role changed"

    has = repo.get_human_assessments(interview_id)
    assert len(has) == 1
    assert has[0]["is_stale"] == 1, "Human assessment must be marked stale after evidence segment role changed"


def test_r2_split_creates_revision_and_invalidates_assessments(r2_r3_env):
    """
    R2: Splitting a segment must create a new revision and invalidate dependent assessments.
    """
    client = r2_r3_env["client"]
    repo = r2_r3_env["repo"]

    interview_id = "inv-r2-split"
    repo.create_interview(
        interview_id=interview_id,
        title="R2 Split",
        candidate_name="Bob",
        role="Backend",
        capture_mode="single_source",
    )
    repo.update_interview_status(interview_id, InterviewStatus.REVIEW)

    repo.add_transcript_segment(
        segment_id="seg-mixed",
        interview_id=interview_id,
        track_id="shared",
        start_time_ms=0,
        end_time_ms=10000,
        text="Вопрос интервьюера. Ответ кандидата.",
        speaker_role="unknown",
        revision_id="trans-rev-1",
    )
    repo.save_association(
        assoc_id="assoc-mixed",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-mixed",
        revision_id="trans-rev-1",
    )
    repo.save_human_assessment(
        assessment_id=f"ha-{interview_id}-q1",
        interview_id=interview_id,
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        scores=[{"criterion_id": "c1", "score": 3.0}],
    )

    res = client.post(
        f"/api/v1/interviews/{interview_id}/segments/seg-mixed/split",
        json={
            "split_time_ms": 5000,
            "text_part1": "Вопрос интервьюера.",
            "text_part2": "Ответ кандидата.",
            "role_part1": "interviewer",
            "role_part2": "candidate",
            "expected_revision_id": "trans-rev-1",
        },
    )
    assert res.status_code == 200

    inv = repo.get_interview(interview_id)
    new_rev = inv["active_transcript_revision_id"]
    assert new_rev != "trans-rev-1"

    # Old revision has 1 segment
    assert len(repo.get_transcript_segments(interview_id, revision_id="trans-rev-1")) == 1
    # New revision has 2 segments
    assert len(repo.get_transcript_segments(interview_id, revision_id=new_rev)) == 2

    # Assessment marked stale
    has = repo.get_human_assessments(interview_id)
    assert has[0]["is_stale"] == 1


def test_r2_occ_conflict_on_stale_expected_revision(r2_r3_env):
    """
    R2: Requests with outdated expected_revision_id must receive 409 conflict.
    """
    client = r2_r3_env["client"]
    repo = r2_r3_env["repo"]

    interview_id = "inv-r2-occ"
    repo.create_interview(
        interview_id=interview_id,
        title="R2 OCC",
        candidate_name="Bob",
        role="Backend",
    )
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5000,
        text="Test",
        speaker_role="candidate",
        revision_id="trans-rev-1",
    )

    # Role change with wrong expected_revision_id
    res = client.post(
        f"/api/v1/interviews/{interview_id}/segments/seg-1/speaker-role",
        json={"speaker_role": "interviewer", "expected_revision_id": "trans-rev-999"},
    )
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_r3_fabricated_candidate_text_rejected(r2_r3_env):
    """
    R3: Worker must NOT accept fabricated candidate_text if there are no segments in DB.
    """
    repo = r2_r3_env["repo"]
    interview_id = "inv-r3-fab"
    repo.create_interview(
        interview_id=interview_id,
        title="R3 Fab",
        candidate_name="Bob",
        role="Backend",
        status=InterviewStatus.RECORDING,
    )

    mock_llm = MagicMock()
    mock_llm.model.upstream_model_id = "mock-gpt"
    mock_llm.execute_request = AsyncMock(
        return_value={
            "data": {
                "scores": [{"criterion_id": "c1", "score": 5.0, "explanation": "ok", "evidence": [{"segment_id": "fabricated", "exact_quote": "invented speech"}]}],
                "critical_errors": [],
            },
            "usage": {"total_tokens": 50},
        }
    )
    worker = PipelineWorker(repo, stt_adapter=MagicMock(), llm_adapter=mock_llm)

    # Enqueue job with fabricated candidate_text and non-existent segment_id
    repo.enqueue_job(
        job_id="job-fab-1",
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={
            "question_id": "q1",
            "candidate_text": "invented speech",
            "segment_id": "fabricated",
        },
    )

    processed = await worker.process_one_job()
    assert processed is True

    props = repo.get_assessment_proposals(interview_id)
    assert len(props) == 1
    # Either proposal has score=None (unanswered) or is_rejected=True
    is_unanswered = props[0]["scores"][0].get("score") is None
    is_rejected = bool(props[0].get("is_rejected"))
    assert is_unanswered or is_rejected, "Fabricated evidence without DB segments must be rejected or marked unanswered!"


@pytest.mark.asyncio
async def test_r3_cross_question_evidence_rejected(r2_r3_env):
    """
    R3: If stub LLM returns a quote from a segment associated with q2 while evaluating q1,
    the proposal must be rejected (is_rejected=True) by the evidence validator.
    """
    repo = r2_r3_env["repo"]
    interview_id = "inv-r3-cross"
    repo.create_interview(
        interview_id=interview_id,
        title="R3 Cross",
        candidate_name="Bob",
        role="Backend",
        status=InterviewStatus.RECORDING,
    )
    plan_payload = {
        "questions": [
            {"id": "q1", "title": "Database indexing", "criteria": [{"id": "c1", "title": "B-Tree knowledge", "min_score": 1, "max_score": 5, "weight": 1}]},
            {"id": "q2", "title": "Concurrency in Go", "criteria": [{"id": "c2", "title": "Goroutines & channels", "min_score": 1, "max_score": 5, "weight": 1}]},
        ]
    }
    repo.save_plan("plan-1", interview_id, plan_payload, 1)

    # Segment 1 belongs to q1
    repo.add_transcript_segment(
        segment_id="seg-q1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5000,
        text="B-Tree индексы ускоряют поиск по диапазону.",
        speaker_role="candidate",
        revision_id="trans-rev-1",
    )
    repo.save_association("assoc-q1", interview_id, "q1", "seg-q1", revision_id="trans-rev-1")

    # Segment 2 belongs to q2
    repo.add_transcript_segment(
        segment_id="seg-q2",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=6000,
        end_time_ms=10000,
        text="Горутины мультиплексируются через рантайм Go.",
        speaker_role="candidate",
        revision_id="trans-rev-1",
    )
    repo.save_association("assoc-q2", interview_id, "q2", "seg-q2", revision_id="trans-rev-1")

    # Mock LLM returns quote from seg-q2 (belonging to q2) when evaluating q1!
    mock_llm = MagicMock()
    mock_llm.model.upstream_model_id = "mock-gpt"
    mock_llm.execute_request = AsyncMock(
        return_value={
            "data": {
                "scores": [
                    {
                        "criterion_id": "c1",
                        "score": 5.0,
                        "explanation": "Great answer",
                        "evidence": [{"segment_id": "seg-q2", "exact_quote": "Горутины мультиплексируются через рантайм Go."}],
                    }
                ],
                "critical_errors": [],
            },
            "usage": {"total_tokens": 100},
        }
    )
    worker = PipelineWorker(repo, stt_adapter=MagicMock(), llm_adapter=mock_llm)

    repo.enqueue_job(
        job_id="job-eval-q1",
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={"question_id": "q1", "rubric_revision_id": "rub-rev-1", "transcript_revision_id": "trans-rev-1"},
    )

    processed = await worker.process_one_job()
    assert processed is True

    props = repo.get_assessment_proposals(interview_id)
    assert len(props) == 1
    assert props[0]["question_id"] == "q1"
    # Evidence validation MUST reject this proposal because seg-q2 does not belong to q1's allowed segments!
    assert props[0]["is_rejected"] is True, "Cross-question evidence must cause proposal to be rejected!"
    assert len(props[0]["validation_errors"]) > 0
