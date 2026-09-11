from unittest.mock import patch

import pytest

from backend.core.summary_generator import ExecutiveSummaryGenerator
from backend.db.database import Database
from backend.db.repository import Repository, RepositoryConflictError
from backend.workers.pipeline import PipelineWorker


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_r4.db"
    db = Database(str(db_file))
    db.init_schema()
    return db


@pytest.fixture
def repo(test_db):
    return Repository(test_db)


@pytest.mark.asyncio
async def test_r4_prompt_filters_rejected_stale_and_foreign_revisions(repo):
    inv_id = "inv-r4-filter"
    repo.create_interview(
        interview_id=inv_id,
        title="Test Interview",
        candidate_name="Алексей Архитектор",
        role="Principal Engineer",
    )
    repo.set_active_rubric_revision(inv_id, "rub-rev-active")
    repo.set_active_transcript_revision(inv_id, "trans-rev-active")

    # 1. Proposal for q1 that is rejected
    repo.save_assessment_proposal(
        proposal_id="prop-rejected",
        interview_id=inv_id,
        question_id="q1",
        rubric_revision_id="rub-rev-active",
        transcript_revision_id="trans-rev-active",
        model_profile_id="test-model",
        scores=[{"criterion_id": "c1", "score": 5.0, "evidence": [{"exact_quote": "bad quote"}]}],
        is_rejected=True,
    )

    # 2. Proposal for q1 that is stale
    repo.save_assessment_proposal(
        proposal_id="prop-stale",
        interview_id=inv_id,
        question_id="q1",
        rubric_revision_id="rub-rev-active",
        transcript_revision_id="trans-rev-active",
        model_profile_id="test-model",
        scores=[{"criterion_id": "c1", "score": 4.0}],
        is_stale=True,
        stale_reason="Old transcript",
    )

    # 3. Proposal for q1 with foreign rubric revision
    repo.save_assessment_proposal(
        proposal_id="prop-foreign-rubric",
        interview_id=inv_id,
        question_id="q1",
        rubric_revision_id="rub-rev-old",
        transcript_revision_id="trans-rev-active",
        model_profile_id="test-model",
        scores=[{"criterion_id": "c1", "score": 3.0}],
    )

    # 4. Human assessment for q2 that is excluded
    repo.save_human_assessment(
        assessment_id="ha-q2",
        interview_id=inv_id,
        question_id="q2",
        rubric_revision_id="rub-rev-active",
        transcript_revision_id="trans-rev-active",
        scores=[],
        is_excluded=True,
        exclusion_reason="Кандидат попросил пропустить тему",
    )

    # 5. Valid confirmed human assessment for q3
    repo.save_human_assessment(
        assessment_id="ha-q3",
        interview_id=inv_id,
        question_id="q3",
        rubric_revision_id="rub-rev-active",
        transcript_revision_id="trans-rev-active",
        scores=[{"criterion_id": "c3", "score": 5.0, "evidence": [{"exact_quote": "я делал sharding"}]}],
        reviewer_notes="Отличный ответ",
    )

    worker = PipelineWorker(repository=repo)

    captured_decisions = []
    captured_limitations = []

    async def fake_generate_summary(*args, **kwargs):
        nonlocal captured_decisions, captured_limitations
        captured_decisions = kwargs.get("decisions", [])
        captured_limitations = kwargs.get("limitations", [])
        return {
            "key_strengths": [{"title": "Шардирование", "description": "Хорошо знает БД", "evidence_quote": "я делал sharding"}],
            "growth_areas": [],
            "hiring_recommendation": "STRONG_HIRE",
            "recommendation_rationale": "Отличный кандидат",
            "session_limitations": captured_limitations,
            "summary_markdown": "# Summary",
            "model_profile_id": "test-model",
        }

    # Add candidate transcript segment so quote validation passes
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=inv_id,
        track_id="trk-cand",
        start_time_ms=0,
        end_time_ms=2000,
        text="Да, я делал sharding базы данных.",
        speaker_role="candidate",
        revision_id="trans-rev-active",
    )

    with patch.object(ExecutiveSummaryGenerator, "generate_summary", side_effect=fake_generate_summary):
        await worker._handle_generate_summary(inv_id, payload={})

    # Assertions:
    # 1. Rejected, stale, foreign rubric proposals for q1 must NOT be in captured_decisions
    q_ids_in_decisions = [d["question_id"] for d in captured_decisions]
    assert "q1" not in q_ids_in_decisions

    # 2. Excluded q2 must NOT be in captured_decisions as an evaluated decision
    assert "q2" not in q_ids_in_decisions

    # 3. Only valid q3 must be in captured_decisions
    assert "q3" in q_ids_in_decisions

    # 4. Excluded q2 must be in limitations
    assert any("q2" in lim and "Кандидат попросил пропустить тему" in lim for lim in captured_limitations)


@pytest.mark.asyncio
async def test_r4_edit_during_generation_marks_proposal_stale(repo):
    inv_id = "inv-r4-stale-gen"
    repo.create_interview(
        interview_id=inv_id,
        title="Test Interview",
        candidate_name="Борис",
        role="Backend Lead",
    )
    repo.set_active_rubric_revision(inv_id, "rub-rev-1")
    repo.set_active_transcript_revision(inv_id, "trans-rev-1")

    repo.save_human_assessment(
        assessment_id="ha-q1",
        interview_id=inv_id,
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        scores=[{"criterion_id": "c1", "score": 3.0}],
    )

    worker = PipelineWorker(repository=repo)

    async def fake_generate_with_concurrent_edit(*args, **kwargs):
        # Simulate human reviewer editing an assessment while AI was generating
        repo.save_human_assessment(
            assessment_id="ha-q1",
            interview_id=inv_id,
            question_id="q1",
            rubric_revision_id="rub-rev-1",
            transcript_revision_id="trans-rev-1",
            scores=[{"criterion_id": "c1", "score": 5.0}],
            reviewer_notes="Обновил оценку до 5",
        )
        return {
            "key_strengths": [],
            "growth_areas": [],
            "hiring_recommendation": "HIRE",
            "recommendation_rationale": "OK",
            "session_limitations": [],
            "summary_markdown": "# Summary",
            "model_profile_id": "test-model",
        }

    with patch.object(ExecutiveSummaryGenerator, "generate_summary", side_effect=fake_generate_with_concurrent_edit):
        await worker._handle_generate_summary(inv_id, payload={})

    summary_prop = repo.get_summary_proposal(inv_id)
    assert summary_prop is not None
    assert summary_prop["is_stale"] == 1
    assert "Оценки были изменены" in (summary_prop.get("stale_reason") or "")


@pytest.mark.asyncio
async def test_r4_confirm_summary_stale_or_wrong_revision_raises_409(repo):
    inv_id = "inv-r4-occ"
    repo.create_interview(
        interview_id=inv_id,
        title="Test Interview",
        candidate_name="Владимир",
        role="Staff Engineer",
    )
    repo.set_active_rubric_revision(inv_id, "rub-rev-1")
    repo.set_active_transcript_revision(inv_id, "trans-rev-1")

    # Save a summary proposal for an older revision
    repo.save_summary_proposal(
        proposal_id="prop-stale-sum",
        interview_id=inv_id,
        model_profile_id="test-model",
        summary_data={"summary_markdown": "Test"},
        transcript_revision_id="trans-rev-old",
        is_stale=True,
        stale_reason="Transcript changed",
    )

    # Attempting to confirm a summary proposal based on old revision must fail with RepositoryConflictError
    with pytest.raises(RepositoryConflictError, match="Revision conflict"):
        repo.confirm_summary(
            interview_id=inv_id,
            reviewer_id="rev-1",
            confirmed_markdown="Confirmed text",
            confirmed_recommendation="HIRE",
            summary_id="prop-stale-sum",
        )

    # Save a fresh proposal
    repo.save_summary_proposal(
        proposal_id="prop-fresh-sum",
        interview_id=inv_id,
        model_profile_id="test-model",
        summary_data={"summary_markdown": "Test Fresh"},
        transcript_revision_id="trans-rev-1",
        is_stale=False,
    )

    # If expected_transcript_revision doesn't match active transcript revision
    with pytest.raises(RepositoryConflictError, match="Revision conflict"):
        repo.confirm_summary(
            interview_id=inv_id,
            reviewer_id="rev-1",
            confirmed_markdown="Confirmed text",
            confirmed_recommendation="HIRE",
            summary_id="prop-fresh-sum",
            expected_transcript_revision="trans-rev-old",
        )

    # Confirming with matching active revision succeeds
    repo.confirm_summary(
        interview_id=inv_id,
        reviewer_id="rev-1",
        confirmed_markdown="Confirmed text",
        confirmed_recommendation="HIRE",
        summary_id="prop-fresh-sum",
        expected_transcript_revision="trans-rev-1",
    )

    prop = repo.get_summary_proposal(inv_id)
    assert prop["is_confirmed"] == 1
    assert prop["confirmed_markdown"] == "Confirmed text"


@pytest.mark.asyncio
async def test_r4_new_generation_does_not_overwrite_confirmed_summary(repo):
    inv_id = "inv-r4-preserve-confirmed"
    repo.create_interview(
        interview_id=inv_id,
        title="Test Interview",
        candidate_name="Дмитрий",
        role="Staff Engineer",
    )
    repo.set_active_transcript_revision(inv_id, "trans-rev-1")

    # 1. Initial proposal confirmed by human
    repo.save_summary_proposal(
        proposal_id="prop-sum-1",
        interview_id=inv_id,
        model_profile_id="test-model",
        summary_data={"summary_markdown": "AI v1"},
        transcript_revision_id="trans-rev-1",
    )
    repo.confirm_summary(
        interview_id=inv_id,
        reviewer_id="rev-alice",
        confirmed_markdown="Human Confirmed Text v1",
        confirmed_recommendation="STRONG_HIRE",
        summary_id="prop-sum-1",
    )

    # 2. Worker generates a new proposal later (e.g. re-trigger)
    repo.save_summary_proposal(
        proposal_id="prop-sum-2",
        interview_id=inv_id,
        model_profile_id="test-model",
        summary_data={"summary_markdown": "AI v2 newly generated"},
        transcript_revision_id="trans-rev-1",
    )

    # Querying summary proposal should still preserve confirmed human text
    prop = repo.get_summary_proposal(inv_id)
    assert prop is not None
    assert prop["confirmed_markdown"] == "Human Confirmed Text v1"
    assert prop["confirmed_recommendation"] == "STRONG_HIRE"


@pytest.mark.asyncio
async def test_r4_evidence_quotes_validation(repo):
    inv_id = "inv-r4-quotes"
    repo.create_interview(
        interview_id=inv_id,
        title="Test Interview",
        candidate_name="Евгений",
        role="Senior SRE",
    )
    repo.set_active_transcript_revision(inv_id, "trans-rev-1")
    repo.set_active_rubric_revision(inv_id, "rub-rev-1")

    # Add real candidate segment
    repo.add_transcript_segment(
        segment_id="seg-real",
        interview_id=inv_id,
        track_id="trk-c",
        start_time_ms=0,
        end_time_ms=5000,
        text="Мы используем Kubernetes и Prometheus для мониторинга.",
        speaker_role="candidate",
        revision_id="trans-rev-1",
    )

    repo.save_human_assessment(
        assessment_id="ha-q1",
        interview_id=inv_id,
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        scores=[{"criterion_id": "c1", "score": 5.0}],
    )

    worker = PipelineWorker(repository=repo)

    async def fake_hallucinating_generator(*args, **kwargs):
        return {
            "key_strengths": [
                {
                    "title": "Мониторинг",
                    "description": "Знает K8s",
                    "evidence_quote": "Мы используем Kubernetes и Prometheus для мониторинга.",  # REAL
                },
                {
                    "title": "Базы данных",
                    "description": "Якобы знает Oracle",
                    "evidence_quote": "Я 10 лет администрировал Oracle Exadata.",  # HALLUCINATED
                },
            ],
            "growth_areas": [],
            "hiring_recommendation": "HIRE",
            "recommendation_rationale": "Хороший опыт",
            "session_limitations": [],
            "summary_markdown": "# Summary",
            "model_profile_id": "test-model",
        }

    with patch.object(ExecutiveSummaryGenerator, "generate_summary", side_effect=fake_hallucinating_generator):
        await worker._handle_generate_summary(inv_id, payload={})

    prop = repo.get_summary_proposal(inv_id)
    assert prop is not None
    strengths = prop["summary_data"]["key_strengths"]
    # Real quote preserved
    assert strengths[0].get("evidence_quote") == "Мы используем Kubernetes и Prometheus для мониторинга."
    # Hallucinated quote must be stripped or None
    assert strengths[1].get("evidence_quote") is None


@pytest.mark.asyncio
async def test_r4_confirm_summary_after_retranscription_resolves_stale(repo):
    inv_id = "inv-r4-stale-resolve"
    repo.create_interview(
        interview_id=inv_id,
        title="Test Interview",
        candidate_name="Сергей",
        role="Backend Architect",
    )
    repo.set_active_transcript_revision(inv_id, "trans-rev-1")
    repo.set_active_rubric_revision(inv_id, "rub-rev-1")

    # AI proposal generated on trans-rev-1
    repo.save_summary_proposal(
        proposal_id="prop-initial-ai",
        interview_id=inv_id,
        model_profile_id="gemini-3.8-flash",
        summary_data={
            "summary_markdown": "AI initial draft",
            "hiring_recommendation": "HIRE",
        },
        transcript_revision_id="trans-rev-1",
        is_stale=False,
    )

    # Transcript gets updated to trans-rev-2
    repo.set_active_transcript_revision(inv_id, "trans-rev-2")

    # Human expert confirms summary on the active trans-rev-2
    repo.confirm_summary(
        interview_id=inv_id,
        reviewer_id="lead-interviewer",
        confirmed_markdown="Human verified summary on rev 2",
        confirmed_recommendation="STRONG_HIRE",
        expected_transcript_revision="trans-rev-2",
    )

    prop = repo.get_summary_proposal(inv_id)
    assert prop["is_confirmed"] == 1
    assert prop["is_stale"] == 0
    assert prop["transcript_revision_id"] == "trans-rev-2"
    assert prop["confirmed_markdown"] == "Human verified summary on rev 2"
    assert prop["confirmed_recommendation"] == "STRONG_HIRE"
    assert prop["confirmed_by"] == "lead-interviewer"
