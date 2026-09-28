"""
The assessment prompt asks the model for critical errors as objects, while the proposal contract
stores strings. An answer with a critical error must still produce a proposal instead of failing
the EVALUATE_QUESTION job on every attempt.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker, normalize_critical_errors
from contracts.domain import InterviewStatus


def test_normalize_critical_errors_shapes():
    assert normalize_critical_errors([
        {"severity": "CRITICAL", "title": "Путает изменяемость", "description": "Считает кортеж изменяемым", "evidence": []},
        {"title": "Только заголовок"},
        "Уже строка",
        {"severity": "MAJOR"},
        42,
    ]) == [
        "CRITICAL: Путает изменяемость — Считает кортеж изменяемым",
        "Только заголовок",
        "Уже строка",
    ]
    assert normalize_critical_errors(None) == []
    assert normalize_critical_errors("oops") == []


@pytest.mark.asyncio
async def test_evaluation_with_object_critical_errors_completes(tmp_path):
    db = Database(str(tmp_path / "crit.db"))
    db.init_schema()
    repo = Repository(db)
    interview_id = "inv-crit"
    repo.create_interview(
        interview_id=interview_id, title="t", candidate_name="c", role="r", status=InterviewStatus.RECORDING
    )
    repo.save_plan("plan-1", interview_id, {
        "questions": [{"id": "q1", "title": "Изменяемость", "criteria": [
            {"id": "c1", "title": "Модель данных", "min_score": 1, "max_score": 5, "weight": 1}
        ]}]
    }, 1)
    quote = "Кортеж можно менять, это изменяемый тип."
    repo.add_transcript_segment(
        segment_id="seg-1", interview_id=interview_id, track_id="candidate", start_time_ms=0, end_time_ms=4000,
        text=quote, speaker_role="candidate", revision_id="trans-rev-1",
    )
    repo.save_association("assoc-1", interview_id, "q1", "seg-1", revision_id="trans-rev-1")

    llm = MagicMock()
    llm.model.upstream_model_id = "mock-model"
    llm.execute_request = AsyncMock(return_value={
        "data": {
            "scores": [{"criterion_id": "c1", "score": 2.0, "explanation": "Ошибка в базовом понятии",
                        "evidence": [{"segment_id": "seg-1", "exact_quote": quote}]}],
            "critical_errors": [{
                "severity": "CRITICAL", "title": "Неверная изменяемость", "description": "Кортеж назван изменяемым",
                "evidence": [{"segment_id": "seg-1", "exact_quote": quote}],
            }],
        },
        "usage": {"total_tokens": 10},
    })
    worker = PipelineWorker(repo, stt_adapter=MagicMock(), llm_adapter=llm)
    repo.enqueue_job(
        job_id="job-eval-crit", job_type="EVALUATE_QUESTION", interview_id=interview_id,
        payload={"question_id": "q1", "rubric_revision_id": "rub-rev-1", "transcript_revision_id": "trans-rev-1"},
    )

    assert await worker.process_one_job() is True

    job = repo.get_interview_jobs_status(interview_id)
    assert job["counts"].get("COMPLETED") == 1, job
    props = repo.get_assessment_proposals(interview_id)
    assert len(props) == 1
    assert props[0]["critical_errors"] == ["CRITICAL: Неверная изменяемость — Кортеж назван изменяемым"]
