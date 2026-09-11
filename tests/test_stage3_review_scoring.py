"""
Regression and validation suite for Stage 3: Unified Human Review & Scoring Pipeline.
Covers requirements R2, R3, R7 from docs/remediation-plan.md:
1. Deterministic scoring engine: 3 on scale 1-5 equals exactly 50.0.
2. Protection against NaN, Infinity, unknown criteria, and duplicate criteria.
3. Empty interview results in final_score_100 = None and coverage = 0.0.
4. Revision-aware review endpoint: stale revision returns 409 Conflict.
5. Human review creates an immutable HumanQuestionAssessment and does not mutate the AI proposal.
6. Reading interview (GET) and exporting report uses calculate_interview_score.
"""
import pytest
from fastapi.testclient import TestClient

from backend.api.app import app
from backend.core.scoring import ScoringError, calculate_interview_score
from contracts.domain import (
    HumanCriterionScore,
    HumanQuestionAssessment,
    PlannedQuestion,
    RubricCriterion,
    RubricRevision,
)


@pytest.fixture
def sample_rubric() -> RubricRevision:
    q1 = PlannedQuestion(
        id="q-algo",
        title="Algorithms",
        prompt="Explain QuickSort",
        weight=1.0,
        criteria=[
            RubricCriterion(id="crit-complexity", title="Complexity", min_score=1.0, max_score=5.0, weight=1.0),
            RubricCriterion(id="crit-edge-cases", title="Edge Cases", min_score=1.0, max_score=5.0, weight=1.0),
        ],
    )
    q2 = PlannedQuestion(
        id="q-sys",
        title="System Design",
        prompt="Design a distributed cache",
        weight=2.0,
        criteria=[
            RubricCriterion(id="crit-arch", title="Architecture", min_score=0.0, max_score=10.0, weight=1.0),
        ],
    )
    return RubricRevision(
        revision_id="rub-rev-1",
        interview_id="int-test-scoring",
        questions=[q1, q2],
    )


# --- 1. Scoring Engine Unit Tests ---

def test_scale_1_to_5_midpoint_is_exactly_50():
    """Requirement: 3 on scale 1-5 must equal 50.0 in all representations."""
    crit = RubricCriterion(id="c1", title="Criterion", min_score=1.0, max_score=5.0, weight=1.0)
    rubric = RubricRevision(
        revision_id="rub-1",
        interview_id="int-1",
        questions=[PlannedQuestion(id="q1", title="Q1", prompt="Prompt", weight=1.0, criteria=[crit])],
    )
    assessments = [
        HumanQuestionAssessment(
            question_id="q1",
            criteria_scores=[HumanCriterionScore(criterion_id="c1", score=3.0)],
        )
    ]
    res = calculate_interview_score(rubric, assessments)
    assert res.final_score_100 == 50.0
    assert res.coverage_percentage == 100.0


def test_empty_interview_returns_none_and_zero_coverage(sample_rubric):
    """Requirement: empty interview has score null and 0% coverage."""
    res = calculate_interview_score(sample_rubric, [])
    assert res.final_score_100 is None
    assert res.coverage_percentage == 0.0


def test_missing_question_does_not_become_zero(sample_rubric):
    """Missing question reduces coverage, does not pull down score to zero as a failure."""
    # Only q1 is assessed with max scores (5/5 = 100%)
    assessments = [
        HumanQuestionAssessment(
            question_id="q-algo",
            criteria_scores=[
                HumanCriterionScore(criterion_id="crit-complexity", score=5.0),
                HumanCriterionScore(criterion_id="crit-edge-cases", score=5.0),
            ],
        )
    ]
    res = calculate_interview_score(sample_rubric, assessments)
    # q1 has weight 1.0, q2 has weight 2.0. Total planned weight = 3.0.
    # Evaluated weight = 1.0. Coverage = 1/3 = 33.333%
    assert res.final_score_100 == 100.0
    assert pytest.approx(res.coverage_percentage, rel=1e-3) == 33.333


def test_reject_nan_and_inf_in_scores(sample_rubric):
    """Requirement: reject NaN/Infinity in scores."""
    nan_assessments = [
        HumanQuestionAssessment(
            question_id="q-algo",
            criteria_scores=[HumanCriterionScore(criterion_id="crit-complexity", score=float("nan"))],
        )
    ]
    with pytest.raises(ScoringError, match="(?i)(nan|invalid|finite)"):
        calculate_interview_score(sample_rubric, nan_assessments)

    inf_assessments = [
        HumanQuestionAssessment(
            question_id="q-algo",
            criteria_scores=[HumanCriterionScore(criterion_id="crit-complexity", score=float("inf"))],
        )
    ]
    with pytest.raises(ScoringError, match="(?i)(inf|outside|invalid|finite)"):
        calculate_interview_score(sample_rubric, inf_assessments)


def test_reject_unknown_criterion_id(sample_rubric):
    """Requirement: check engine on unknown criterion IDs."""
    bad_assessments = [
        HumanQuestionAssessment(
            question_id="q-algo",
            criteria_scores=[
                HumanCriterionScore(criterion_id="non-existent-criterion", score=3.0),
            ],
        )
    ]
    with pytest.raises(ScoringError, match="(?i)(unknown criterion|not found)"):
        calculate_interview_score(sample_rubric, bad_assessments)


def test_reject_duplicate_criterion_id(sample_rubric):
    """Requirement: check engine on duplicate criterion IDs in the same question assessment."""
    bad_assessments = [
        HumanQuestionAssessment(
            question_id="q-algo",
            criteria_scores=[
                HumanCriterionScore(criterion_id="crit-complexity", score=3.0),
                HumanCriterionScore(criterion_id="crit-complexity", score=4.0),
            ],
        )
    ]
    with pytest.raises(ScoringError, match="(?i)(duplicate criterion)"):
        calculate_interview_score(sample_rubric, bad_assessments)


# --- 2. API Endpoint Tests for Review, Revision Verification and State Isolation ---

from backend.api.app import get_repository
from backend.db.database import Database
from backend.db.repository import Repository


@pytest.fixture
def test_env(tmp_path):
    db_file = tmp_path / "stage3_test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as client:
        yield {"client": client, "repo": repo, "db": db}
    app.dependency_overrides.clear()


def test_stale_review_returns_409_conflict(test_env):
    """Requirement: review submission with stale expected transcript revision returns 409 Conflict."""
    client = test_env["client"]
    int_id = "test-stale-rev-1"
    client.post(
        "/api/v1/interviews",
        json={
            "id": int_id,
            "title": "Stale Review Check",
            "candidate_name": "Test User",
            "role": "Backend",
            "plan": {
                "role": "Backend",
                "questions": [
                    {
                        "id": "q1",
                        "title": "Concurrency",
                        "prompt": "Explain locks",
                        "criteria": [{"id": "c1", "title": "Accuracy", "min_score": 1.0, "max_score": 5.0, "weight": 1.0}],
                    }
                ],
            },
        },
    )

    # Submitting with an obsolete expected revision must yield 409
    res = client.post(
        f"/api/v1/interviews/{int_id}/assessments/q1/review",
        json={
            "expected_transcript_revision": "obsolete-trans-rev-0",
            "scores": [{"criterion_id": "c1", "score": 4.0}],
            "reviewer_notes": "Looks good",
        },
    )
    assert res.status_code == 409
    assert "conflict" in res.json()["detail"].lower()


def test_review_creates_human_assessment_without_mutating_ai_proposal(test_env):
    """Requirement: human confirmation creates HumanQuestionAssessment; AI proposal is NOT mutated."""
    client = test_env["client"]
    repo = test_env["repo"]
    int_id = "test-ai-proposal-preservation"
    client.post(
        "/api/v1/interviews",
        json={
            "id": int_id,
            "title": "AI Preservation Check",
            "candidate_name": "Test User",
            "role": "Backend",
            "plan": {
                "role": "Backend",
                "questions": [
                    {
                        "id": "q1",
                        "title": "Concurrency",
                        "prompt": "Explain locks",
                        "criteria": [{"id": "c1", "title": "Accuracy", "min_score": 1.0, "max_score": 5.0, "weight": 1.0}],
                    }
                ],
            },
        },
    )

    # 1. Mock AI proposal
    repo.save_assessment_proposal(
        proposal_id="prop-ai-1",
        interview_id=int_id,
        question_id="q1",
        model_profile_id="google/gemini-3.8-flash",
        scores=[{"criterion_id": "c1", "score": 2.0, "explanation": "Weak explanation", "evidence": []}],
        critical_errors=[],
    )

    # 2. Human reviewer submits score 4.0
    res = client.post(
        f"/api/v1/interviews/{int_id}/assessments/q1/review",
        json={
            "expected_transcript_revision": "trans-rev-1",
            "scores": [{"criterion_id": "c1", "score": 4.0, "explanation": "Actually candidate answered well"}],
            "reviewer_notes": "Candidate clarified in detail",
            "reviewer_id": "human-interviewer-1",
            "is_manually_adjusted": True,
        },
    )
    assert res.status_code == 200

    # 3. Verify GET /interview:
    # - AI proposal remains untouched (score 2.0, model google/gemini-3.8-flash, is_approved False)
    # - Human assessments list contains the human decision (score 4.0)
    # - Server calculates final score based on human assessment: 4 on scale 1-5 is 75.0%
    get_res = client.get(f"/api/v1/interviews/{int_id}")
    assert get_res.status_code == 200
    data = get_res.json()

    proposals = data["assessment_proposals"]
    assert len(proposals) == 1
    assert proposals[0]["scores"][0]["score"] == 2.0
    assert bool(proposals[0]["is_approved"]) is False

    human_assessments = data.get("human_assessments", [])
    assert len(human_assessments) == 1
    assert human_assessments[0]["question_id"] == "q1"
    assert human_assessments[0]["scores"][0]["score"] == 4.0

    scoring = data.get("scoring")
    assert scoring is not None
    # 4 on 1-5 scale: (4 - 1)/(5 - 1) * 100 = 75.0%
    assert scoring["final_score_100"] == 75.0
    assert scoring["coverage_percentage"] == 100.0


def test_export_endpoint_uses_deterministic_scoring(test_env):
    """Requirement: /export uses calculate_interview_score instead of naive total/5 average."""
    client = test_env["client"]
    int_id = "test-export-scoring-match"
    client.post(
        "/api/v1/interviews",
        json={
            "id": int_id,
            "title": "Export Scoring Check",
            "candidate_name": "Test User",
            "role": "Backend",
            "plan": {
                "role": "Backend",
                "questions": [
                    {
                        "id": "q1",
                        "title": "Concurrency",
                        "prompt": "Explain locks",
                        "criteria": [{"id": "c1", "title": "Accuracy", "min_score": 1.0, "max_score": 5.0, "weight": 1.0}],
                    }
                ],
            },
        },
    )

    # Score 3 on scale 1-5: Naive old formula gave 3/5 * 100 = 60.0.
    # Deterministic engine gives (3-1)/(5-1) * 100 = 50.0.
    client.post(
        f"/api/v1/interviews/{int_id}/assessments/q1/review",
        json={
            "expected_transcript_revision": "trans-rev-1",
            "scores": [{"criterion_id": "c1", "score": 3.0}],
            "reviewer_notes": "Average response",
        },
    )

    res = client.get(f"/api/v1/interviews/{int_id}/export")
    assert res.status_code == 200
    export_data = res.json()
    assert export_data["final_score_100"] == 50.0
