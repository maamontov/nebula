import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.app import app
from contracts.domain import (
    HumanCriterionScore,
    HumanQuestionAssessment,
    PlannedQuestion,
    RubricCriterion,
    RubricRevision,
)


@pytest.mark.asyncio
async def test_healthz():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "version": "0.1.0"}


@pytest.mark.asyncio
async def test_scoring_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        rubric = RubricRevision(
            revision_id="r1",
            interview_id="i1",
            questions=[
                PlannedQuestion(
                    id="q1",
                    title="Q1",
                    prompt="P1",
                    criteria=[RubricCriterion(id="c1", title="C1", min_score=1, max_score=5, weight=1)],
                )
            ],
        )
        assessments = [
            HumanQuestionAssessment(
                question_id="q1",
                criteria_scores=[HumanCriterionScore(criterion_id="c1", score=5.0)],
            )
        ]
        payload = {
            "rubric": rubric.model_dump(mode="json"),
            "assessments": [a.model_dump(mode="json") for a in assessments],
        }
        resp = await client.post("/api/v1/scoring/calculate", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["final_score_100"] == 100.0
        assert data["coverage_percentage"] == 100.0


@pytest.mark.asyncio
async def test_lifecycle_transition_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Legal transition DRAFT -> READY
        resp = await client.post(
            "/api/v1/lifecycle/transition",
            json={"current_status": "draft", "target_status": "ready"},
        )
        assert resp.status_code == 200
        assert resp.json()["current_status"] == "ready"

        # Illegal transition DRAFT -> FINALIZED
        resp2 = await client.post(
            "/api/v1/lifecycle/transition",
            json={"current_status": "draft", "target_status": "finalized"},
        )
        assert resp2.status_code == 400
