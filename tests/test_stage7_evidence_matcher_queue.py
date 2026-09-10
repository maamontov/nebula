"""
Tests for Stage 7: Question matching, Evidence validation, and Atomic Job Lease Queue.
Тесты для Этапа 7: Привязка ответов QuestionMatcher, строгая валидация доказательств EvidenceValidator и атомарный лизинг очереди воркеров.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from unittest.mock import AsyncMock, MagicMock

from backend.api.app import app
from backend.core.evidence_validator import validate_proposal
from backend.core.matcher import QuestionMatcher
from backend.db.database import Database
from backend.db.repository import Repository, RepositoryConflictError
from backend.workers.pipeline import PipelineWorker
from contracts.audio import TrackType
from contracts.domain import (
    AssessmentProposal,
    CriterionScoreProposal,
    EvidenceRef,
    TranscriptRevision,
    TranscriptSegment,
)


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_stage7.db"
    db = Database(str(db_file))
    db.init_schema()
    return db


@pytest.fixture
def repo(test_db):
    return Repository(test_db)


@pytest.fixture
def client(test_db, monkeypatch):
    monkeypatch.setattr("backend.api.app.get_db", lambda: test_db)
    monkeypatch.setenv("NEBULA_DISABLE_AUTH", "1")
    return TestClient(app)


def _setup_interview_with_plan(repo: Repository, interview_id: str = "inv-stg7-1"):
    repo.create_interview(
        interview_id=interview_id,
        title="Stage 7 Python Backend Engineer",
        candidate_name="Алексей Смирнов",
        role="Senior Backend Developer",
    )
    repo.update_interview_status(interview_id, "ready")
    repo.update_interview_status(interview_id, "recording")

    plan_payload = {
        "questions": [
            {
                "id": "q1",
                "text": "Расскажите про GIL в Python и способы его обхода",
                "criteria": [
                    {
                        "id": "crit-gil",
                        "title": "Понимание Global Interpreter Lock и multiprocessing",
                        "weight": 1.0,
                    }
                ],
            },
            {
                "id": "q2",
                "text": "Как устроена изоляция транзакций в PostgreSQL?",
                "criteria": [
                    {
                        "id": "crit-pg",
                        "title": "Уровни изоляции транзакций и MVCC",
                        "weight": 1.0,
                    }
                ],
            },
        ]
    }
    repo.save_plan(
        plan_id=f"plan-{interview_id}",
        interview_id=interview_id,
        payload=plan_payload,
    )
    return plan_payload


def test_matcher_preserves_manual_associations(repo: Repository):
    interview_id = "inv-matcher-1"
    plan = _setup_interview_with_plan(repo, interview_id)
    questions = plan["questions"]

    # 1. Add interviewer question and candidate answer
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="interviewer",
        start_time_ms=0,
        end_time_ms=3000,
        text="Расскажите про GIL в Python?",
    )
    repo.add_transcript_segment(
        segment_id="seg-2",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=3500,
        end_time_ms=8000,
        text="GIL это глобальная блокировка интерпретатора в CPython, для параллелизма используют multiprocessing.",
    )
    repo.add_transcript_segment(
        segment_id="seg-3",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=8500,
        end_time_ms=12000,
        text="Также в PostgreSQL MVCC решает конфликты чтения и записи.",
    )

    matcher = QuestionMatcher()
    all_segs = repo.get_transcript_segments(interview_id)

    # Initial matching
    assocs_1 = matcher.associate_segments(questions, all_segs)
    for a in assocs_1:
        repo.save_association(
            assoc_id=f"a-{a.segment_id}",
            interview_id=interview_id,
            question_id=a.question_id,
            segment_id=a.segment_id,
            confidence=a.confidence,
            is_ambiguous=a.is_ambiguous,
            is_manually_adjusted=False,
            notes=a.notes,
        )

    # 2. Human manually reassigns seg-3 to q2
    repo.reassociate_segment(
        interview_id=interview_id,
        segment_id="seg-3",
        new_question_id="q2",
        notes="Ручная корректировка: ответ явно относится к PostgreSQL",
    )

    # Verify manual adjustment flag is persisted
    assocs_after_manual = repo.get_associations(interview_id)
    seg3_assoc = next(a for a in assocs_after_manual if a["segment_id"] == "seg-3")
    assert seg3_assoc["question_id"] == "q2"
    assert seg3_assoc["is_manually_adjusted"] == 1

    # 3. Add a new segment (seg-4) and run matcher with existing associations
    repo.add_transcript_segment(
        segment_id="seg-4",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=12500,
        end_time_ms=16000,
        text="Асинхронный код на asyncio работает в одном потоке и решает задачи ввода-вывода без блокировки GIL.",
    )
    all_segs_2 = repo.get_transcript_segments(interview_id)

    # Matcher with preserved manual associations
    assocs_2 = matcher.associate_segments(
        questions,
        all_segs_2,
        existing_associations=repo.get_associations(interview_id),
    )

    # Seg-3 MUST remain q2 (not overwritten by automatic matcher!)
    seg3_recomputed = next(a for a in assocs_2 if a.segment_id == "seg-3")
    assert seg3_recomputed.question_id == "q2"
    assert "ручная" in seg3_recomputed.notes.lower()

    # Seg-4 should be matched to q1 (GIL/asyncio)
    seg4_recomputed = next(a for a in assocs_2 if a.segment_id == "seg-4")
    assert seg4_recomputed.question_id == "q1"


def test_evidence_validator_rejects_hallucinated_quote():
    transcript = TranscriptRevision(
        revision_id="trans-rev-1",
        interview_id="inv-val-1",
        segments=[
            TranscriptSegment(
                id="seg-1",
                track_id=TrackType.CANDIDATE,
                start_time_ms=0,
                end_time_ms=5000,
                text="Мы использовали Redis для кэширования сессий пользователей.",
            )
        ],
    )

    # Hallucinated quote: "Мы переписали ядро на Rust" (never said by candidate)
    fake_proposal = AssessmentProposal(
        id="prop-fake-1",
        interview_id="inv-val-1",
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        model_profile_id="gpt-4o",
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-arch",
                score=5.0,
                explanation="Отличный опыт работы с языком Rust",
                evidence=[
                    EvidenceRef(
                        segment_id="seg-1",
                        exact_quote="Мы переписали ядро на Rust",
                    )
                ],
            )
        ],
    )

    res = validate_proposal(fake_proposal, transcript)
    assert res.is_valid is False
    assert len(res.errors) >= 1
    assert "not found verbatim" in res.errors[0].lower()


def test_evidence_validator_rejects_interviewer_track_quote():
    transcript = TranscriptRevision(
        revision_id="trans-rev-1",
        interview_id="inv-val-2",
        segments=[
            TranscriptSegment(
                id="seg-inv-1",
                track_id=TrackType.INTERVIEWER,
                start_time_ms=0,
                end_time_ms=3000,
                text="Каковы ваши сильные стороны в проектировании микросервисов?",
            ),
            TranscriptSegment(
                id="seg-cand-1",
                track_id=TrackType.CANDIDATE,
                start_time_ms=3500,
                end_time_ms=6000,
                text="Я проектировал брокеры сообщений на Kafka.",
            ),
        ],
    )

    # Model attributes interviewer question text as candidate evidence!
    invalid_track_proposal = AssessmentProposal(
        id="prop-track-1",
        interview_id="inv-val-2",
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        model_profile_id="gpt-4o",
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-1",
                score=4.0,
                explanation="Кандидат упомянул сильные стороны в микросервисах",
                evidence=[
                    EvidenceRef(
                        segment_id="seg-inv-1",
                        exact_quote="Каковы ваши сильные стороны в проектировании микросервисов",
                    )
                ],
            )
        ],
    )

    res = validate_proposal(invalid_track_proposal, transcript)
    assert res.is_valid is False
    assert any("interviewer track" in err.lower() or "candidate track" in err.lower() for err in res.errors)


def test_evidence_validator_rejects_out_of_range_score():
    transcript = TranscriptRevision(
        revision_id="trans-rev-1",
        interview_id="inv-val-3",
        segments=[
            TranscriptSegment(
                id="seg-1",
                track_id=TrackType.CANDIDATE,
                start_time_ms=0,
                end_time_ms=4000,
                text="Использовал индексы B-tree и Hash в PostgreSQL.",
            )
        ],
    )

    # Out-of-range score: 10.0 (standard rubric scale is 1.0 to 5.0)
    out_of_range_prop = AssessmentProposal(
        id="prop-range-1",
        interview_id="inv-val-3",
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        model_profile_id="gpt-4o",
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-db",
                score=10.0,
                explanation="Превосходный ответ выше максимума",
                evidence=[
                    EvidenceRef(
                        segment_id="seg-1",
                        exact_quote="Использовал индексы B-tree",
                    )
                ],
            )
        ],
    )

    res = validate_proposal(out_of_range_prop, transcript)
    assert res.is_valid is False
    assert any("range" in err.lower() for err in res.errors)


def test_rejected_proposal_cannot_be_approved(client: TestClient, repo: Repository):
    interview_id = "inv-reject-appr-1"
    _setup_interview_with_plan(repo, interview_id)

    # Save a proposal marked as rejected due to evidence validation errors
    proposal_id = "prop-rejected-1"
    repo.save_assessment_proposal(
        proposal_id=proposal_id,
        interview_id=interview_id,
        question_id="q1",
        model_profile_id="gpt-4o",
        scores=[{"criterion_id": "crit-gil", "score": 5.0, "explanation": "test"}],
        critical_errors=["Evidence quote 'Rust core' not found verbatim in transcript"],
        is_rejected=True,
        validation_errors=["Evidence quote 'Rust core' not found verbatim in transcript"],
    )

    # Attempting to approve rejected proposal must be blocked with 409 Conflict
    r = client.post(
        f"/api/v1/interviews/{interview_id}/assessments/{proposal_id}/approve",
        json={"question_id": "q1"},
    )
    assert r.status_code == 409
    assert "rejected" in r.json()["detail"].lower()


def test_atomic_job_lease_and_owner_lock(repo: Repository):
    interview_id = "inv-job-atomic-1"
    _setup_interview_with_plan(repo, interview_id)

    job_id = "job-lease-1"
    repo.enqueue_job(
        job_id=job_id,
        job_type="TRANSCRIBE_AUDIO",
        interview_id=interview_id,
        payload={"audio_hex": "0102"},
    )

    # Worker 1 claims job
    job1 = repo.claim_next_job(lock_duration_sec=30)
    assert job1 is not None
    assert job1["id"] == job_id
    owner_token_1 = job1.get("locked_by")
    assert owner_token_1 is not None

    # No other worker can claim it while active
    job2 = repo.claim_next_job(lock_duration_sec=30)
    assert job2 is None

    # Worker with WRONG token cannot complete the job
    with pytest.raises(RepositoryConflictError):
        repo.complete_job(job_id=job_id, owner_token="wrong-token-abc")

    # Worker 1 can renew lease
    renewed = repo.renew_job_lease(job_id=job_id, owner_token=owner_token_1, extension_sec=45)
    assert renewed is True

    # Worker 1 with valid token completes job successfully
    repo.complete_job(job_id=job_id, owner_token=owner_token_1)

    # Re-completing already completed job raises conflict
    with pytest.raises(RepositoryConflictError):
        repo.complete_job(job_id=job_id, owner_token=owner_token_1)


def test_incremental_associations_endpoint(client: TestClient, repo: Repository):
    interview_id = "inv-assoc-inc-1"
    _setup_interview_with_plan(repo, interview_id)

    # Ingest segment 1
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=3000,
        text="GIL это Global Interpreter Lock в CPython",
    )

    r1 = client.get(f"/api/v1/interviews/{interview_id}/associations")
    assert r1.status_code == 200
    assocs1 = r1.json()["associations"]
    assert len(assocs1) == 1
    assert assocs1[0]["segment_id"] == "seg-1"
    assert assocs1[0]["question_id"] == "q1"

    # Ingest segment 2 (about PostgreSQL)
    repo.add_transcript_segment(
        segment_id="seg-2",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=4000,
        end_time_ms=7000,
        text="Изоляция транзакций в PostgreSQL реализуется через MVCC snapshotting",
    )

    # Calling GET associations should incrementally match seg-2 without overwriting seg-1
    r2 = client.get(f"/api/v1/interviews/{interview_id}/associations")
    assert r2.status_code == 200
    assocs2 = r2.json()["associations"]
    assert len(assocs2) == 2
    seg1_a = next(a for a in assocs2 if a["segment_id"] == "seg-1")
    seg2_a = next(a for a in assocs2 if a["segment_id"] == "seg-2")
    assert seg1_a["question_id"] == "q1"
    assert seg2_a["question_id"] == "q2"


@pytest.mark.asyncio
async def test_pipeline_worker_evidence_validation_rejects_hallucination(repo: Repository):
    from unittest.mock import AsyncMock, MagicMock
    from backend.workers.pipeline import PipelineWorker

    interview_id = "inv-worker-ev-1"
    _setup_interview_with_plan(repo, interview_id)

    # Real segment in candidate speech
    repo.add_transcript_segment(
        segment_id="seg-real-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5000,
        text="В CPython GIL блокирует параллельное выполнение байткода.",
    )
    repo.save_association(
        assoc_id="a-1",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-real-1",
        confidence=0.9,
        is_ambiguous=False,
    )

    # LLM returns a hallucinated quote: "Я переписал рантайм на Go"
    mock_llm = MagicMock()
    mock_llm.execute_request = AsyncMock(
        return_value=(
            {
                "scores": [
                    {
                        "criterion_id": "crit-gil",
                        "score": 5.0,
                        "explanation": "Отличный ответ",
                        "evidence": [
                            {"segment_id": "seg-real-1", "exact_quote": "Я переписал рантайм на Go"}
                        ],
                    }
                ],
                "critical_errors": [],
            },
            "test-gemini-3.8-flash",
        )
    )

    worker = PipelineWorker(repo, stt_adapter=MagicMock(), llm_adapter=mock_llm)

    # Enqueue EVALUATE_QUESTION with only question_id (worker retrieves candidate segments from DB)
    repo.enqueue_job(
        job_id="job-ev-hallucinated",
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={"question_id": "q1"},
    )

    processed = await worker.process_one_job()
    assert processed is True

    # Check saved proposal
    props = repo.get_assessment_proposals(interview_id)
    assert len(props) == 1
    p = props[0]
    assert p["is_rejected"] == 1
    assert len(p["validation_errors"]) >= 1
    assert "not found verbatim" in p["validation_errors"][0].lower()


@pytest.mark.asyncio
async def test_pipeline_worker_no_cross_question_leak_and_unanswered(repo: Repository):
    interview_id = "inv-leak-test"
    _setup_interview_with_plan(repo, interview_id)

    # Add candidate speech only for q1 (GIL)
    repo.add_transcript_segment(
        segment_id="seg-gil-cand",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=5000,
        end_time_ms=10000,
        text="Мы используем multiprocessing для обхода GIL.",
        is_final=True,
    )
    repo.save_association(
        assoc_id="assoc-gil-cand",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-gil-cand",
        confidence=0.95,
        is_ambiguous=False,
    )

    mock_llm = MagicMock()
    mock_llm.execute_request = AsyncMock()
    worker = PipelineWorker(repo, stt_adapter=MagicMock(), llm_adapter=mock_llm)

    # Now evaluate question q2 (PostgreSQL) which has NO candidate speech!
    repo.enqueue_job(
        job_id="job-eval-q2",
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={"question_id": "q2"},
    )

    processed = await worker.process_one_job()
    assert processed is True
    # Verify LLM was NOT called because there was no candidate speech for q2
    mock_llm.execute_request.assert_not_called()

    # Verify an explicit unanswered proposal was saved
    props = repo.get_assessment_proposals(interview_id)
    assert len(props) == 1
    p = props[0]
    assert p["question_id"] == "q2"
    assert p["is_rejected"] == 0
    scores = p["scores"]
    assert len(scores) == 1
    assert scores[0]["score"] is None
    assert "отсутствует" in scores[0]["explanation"]


@pytest.mark.asyncio
async def test_pipeline_worker_stale_revision_detected(repo: Repository):
    interview_id = "inv-stale-test"
    _setup_interview_with_plan(repo, interview_id)

    repo.add_transcript_segment(
        segment_id="seg-pg-cand",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=5000,
        end_time_ms=10000,
        text="В Postgres по умолчанию уровень Read Committed.",
        is_final=True,
    )
    repo.save_association(
        assoc_id="assoc-pg-cand",
        interview_id=interview_id,
        question_id="q2",
        segment_id="seg-pg-cand",
        confidence=0.95,
        is_ambiguous=False,
    )

    mock_llm = MagicMock()
    mock_llm.execute_request = AsyncMock(
        return_value=(
            {
                "scores": [
                    {
                        "criterion_id": "crit-pg",
                        "score": 5.0,
                        "explanation": "Правильный ответ по Read Committed.",
                        "evidence": [
                            {"segment_id": "seg-pg-cand", "exact_quote": "В Postgres по умолчанию уровень Read Committed."}
                        ],
                    }
                ],
                "critical_errors": [],
            },
            "test-model",
        )
    )
    worker = PipelineWorker(repo, stt_adapter=MagicMock(), llm_adapter=mock_llm)

    # Enqueue with older transcript revision trans-rev-old
    repo.enqueue_job(
        job_id="job-eval-stale",
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={"question_id": "q2", "transcript_revision_id": "trans-rev-old"},
    )

    # Active transcript revision in interview is trans-rev-1 (or not matching trans-rev-old)
    processed = await worker.process_one_job()
    assert processed is True

    props = repo.get_assessment_proposals(interview_id)
    assert len(props) == 1
    p = props[0]
    assert p["is_stale"] == 1
    assert "Revision" in p["stale_reason"]
