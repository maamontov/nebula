"""
Tests for Stage 8: Batch Retranscription, Revision Consistency, and Optimistic Concurrency.
Тесты для Этапа 8: Финальная пакетная STT, согласованность ревизий стенограмм и оптимистичные блокировки.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app
from backend.core.evidence_validator import validate_proposal
from backend.db.database import Database
from backend.db.repository import Repository, RepositoryConflictError
from backend.workers.pipeline import PipelineWorker
from contracts.audio import TrackType
from contracts.domain import (
    AssessmentProposal,
    CriterionScoreProposal,
    EvidenceRef,
    InterviewStatus,
    TranscriptRevision,
    TranscriptSegment,
)


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_stage8.db"
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


def _setup_interview_stage8(repo: Repository, interview_id: str = "inv-stg8-1"):
    repo.create_interview(
        interview_id=interview_id,
        title="Stage 8 Senior Architect",
        candidate_name="Иван Кузнецов",
        role="Distributed Systems Architect",
    )
    repo.update_interview_status(interview_id, "ready")
    repo.update_interview_status(interview_id, "recording")

    plan_payload = {
        "questions": [
            {
                "id": "q1",
                "text": "Как обеспечить отказоустойчивость Redis в распределённой системе?",
                "criteria": [
                    {
                        "id": "crit-redis",
                        "title": "Redis Sentinel, Cluster и репликация",
                        "weight": 1.0,
                        "min_score": 1.0,
                        "max_score": 5.0,
                    }
                ],
            }
        ]
    }
    repo.save_plan(
        plan_id=f"plan-{interview_id}",
        interview_id=interview_id,
        payload=plan_payload,
    )
    return plan_payload


def test_two_versions_of_segment_stored_and_addressable(client: TestClient, repo: Repository):
    """
    Acceptance Criteria 1: Две версии одного logical segment доступны и адресуются независимо.
    """
    interview_id = "inv-rev-two-versions"
    _setup_interview_stage8(repo, interview_id)

    # Revision 1 (Live recording STT)
    repo.create_transcript_revision(
        revision_id="trans-rev-1",
        interview_id=interview_id,
        revision_number=1,
        is_batch_final=False,
    )
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5000,
        text="Мы использовали Redis для кэша.",
        revision_id="trans-rev-1",
    )

    # Revision 2 (Batch high-accuracy STT)
    repo.create_transcript_revision(
        revision_id="trans-rev-2",
        interview_id=interview_id,
        revision_number=2,
        is_batch_final=True,
    )
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5100,
        text="Мы использовали Redis Cluster с репликацией на три зоны доступности.",
        revision_id="trans-rev-2",
    )

    # 1. Direct repository queries by revision
    rev1_segs = repo.get_transcript_segments(interview_id, revision_id="trans-rev-1")
    rev2_segs = repo.get_transcript_segments(interview_id, revision_id="trans-rev-2")

    assert len(rev1_segs) == 1
    assert len(rev2_segs) == 1
    assert rev1_segs[0]["text"] == "Мы использовали Redis для кэша."
    assert rev2_segs[0]["text"] == "Мы использовали Redis Cluster с репликацией на три зоны доступности."

    # 2. Revisions listing endpoint
    r_list = client.get(f"/api/v1/interviews/{interview_id}/revisions/transcript")
    assert r_list.status_code == 200
    data = r_list.json()
    assert len(data["revisions"]) == 2
    assert data["active_revision_id"] == "trans-rev-1"

    # 3. Segments endpoint with exact revision
    r_seg1 = client.get(f"/api/v1/interviews/{interview_id}/revisions/transcript/trans-rev-1/segments")
    assert r_seg1.status_code == 200
    assert r_seg1.json()["segments"][0]["text"] == "Мы использовали Redis для кэша."

    r_seg2 = client.get(f"/api/v1/interviews/{interview_id}/revisions/transcript/trans-rev-2/segments")
    assert r_seg2.status_code == 200
    assert "Redis Cluster" in r_seg2.json()["segments"][0]["text"]


def test_evidence_quote_resolves_in_its_own_revision(repo: Repository):
    """
    Acceptance Criteria 2: Старая цитата разрешается в своей версии без искажений.
    """
    interview_id = "inv-ev-resolves"
    _setup_interview_stage8(repo, interview_id)

    # In rev-1 candidate says: "Кэшировали сессии в Redis"
    trans_rev1 = TranscriptRevision(
        revision_id="trans-rev-1",
        interview_id=interview_id,
        segments=[
            TranscriptSegment(
                id="seg-1",
                track_id=TrackType.CANDIDATE,
                start_time_ms=0,
                end_time_ms=4000,
                text="Кэшировали сессии в Redis",
            )
        ],
    )

    prop = AssessmentProposal(
        id="prop-redis-1",
        interview_id=interview_id,
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        model_profile_id="gemini-3.8-flash",
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-redis",
                score=4.0,
                explanation="Упомянул кэширование сессий",
                evidence=[EvidenceRef(segment_id="seg-1", exact_quote="Кэшировали сессии в Redis")],
            )
        ],
    )

    # Valid in trans-rev-1
    val1 = validate_proposal(prop, trans_rev1)
    assert val1.is_valid is True

    # In trans-rev-2 candidate speech was corrected: "Кэшировали токены в Memcached"
    trans_rev2 = TranscriptRevision(
        revision_id="trans-rev-2",
        interview_id=interview_id,
        segments=[
            TranscriptSegment(
                id="seg-1",
                track_id=TrackType.CANDIDATE,
                start_time_ms=0,
                end_time_ms=4000,
                text="Кэшировали токены в Memcached",
            )
        ],
    )
    val2 = validate_proposal(prop, trans_rev2)
    assert val2.is_valid is False  # Quote broken in rev-2, but intact in rev-1!


@pytest.mark.asyncio
async def test_batch_retranscribe_marks_dependent_reviews_and_summary_stale(
    client: TestClient, repo: Repository
):
    """
    Acceptance Criteria 3 & 4: Изменение ответа помечает оценку и summary stale;
    финализация блокируется с причиной до повторного подтверждения.
    """
    interview_id = "inv-stale-flow-1"
    _setup_interview_stage8(repo, interview_id)

    # Setup Rev 1
    repo.create_transcript_revision("trans-rev-1", interview_id, 1, is_batch_final=False)
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=4000,
        text="Я настраивал Redis Sentinel для автоматического фейловера.",
        revision_id="trans-rev-1",
    )
    repo.save_association(
        assoc_id="a-1",
        interview_id=interview_id,
        question_id="q1",
        segment_id="seg-1",
        confidence=0.95,
        is_ambiguous=False,
    )

    # Save initial AI proposal
    repo.save_assessment_proposal(
        proposal_id="prop-1",
        interview_id=interview_id,
        question_id="q1",
        model_profile_id="gemini-3.8-flash",
        scores=[
            {
                "criterion_id": "crit-redis",
                "score": 5.0,
                "explanation": "Отличный ответ",
                "evidence": [
                    {"segment_id": "seg-1", "exact_quote": "Я настраивал Redis Sentinel"}
                ],
            }
        ],
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
    )

    # Transition to REVIEW status
    repo.update_interview_status(interview_id, "processing")
    repo.update_interview_status(interview_id, "review")

    # Human reviewer confirms assessment for q1
    repo.save_human_assessment(
        assessment_id="ha-1",
        interview_id=interview_id,
        question_id="q1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        reviewer_id="reviewer-alice",
        scores=[{"criterion_id": "crit-redis", "score": 5.0}],
        reviewer_notes="Кандидат детально знает Sentinel",
    )

    # Save and confirm executive summary
    repo.save_summary_proposal(
        proposal_id="sum-1",
        interview_id=interview_id,
        model_profile_id="gemini-3.8-flash",
        summary_data={
            "summary_markdown": "# Резюме кандидата",
            "strengths": ["Redis Sentinel"],
            "weaknesses": [],
            "recommendations": ["Hire"],
        },
    )
    repo.confirm_summary(
        interview_id=interview_id,
        reviewer_id="reviewer-alice",
        confirmed_markdown="# Подтвержденное резюме",
        confirmed_recommendation="Hire",
    )

    # Verify state before retranscription: human assessment fresh, summary confirmed
    ha_before = repo.get_human_assessments(interview_id)[0]
    assert ha_before["is_stale"] == 0
    sum_before = repo.get_summary_proposal(interview_id)
    assert sum_before["is_confirmed"] == 1

    # Run BATCH_RETRANSCRIBE worker job where text changed completely
    worker = PipelineWorker(repo)
    repo.enqueue_job(
        job_id="job-batch-stale-1",
        job_type="BATCH_RETRANSCRIBE",
        interview_id=interview_id,
        payload={
            "old_revision_id": "trans-rev-1",
            "new_revision_id": "trans-rev-2",
            "revision_number": 2,
            "segments": [
                {
                    "id": "seg-1",
                    "track_id": "candidate",
                    "start_time_ms": 0,
                    "end_time_ms": 4000,
                    "text": "Мы отказались от Redis и перешли на Apache Cassandra.",
                }
            ],
        },
    )

    processed = await worker.process_one_job()
    assert processed is True

    # 1. Verify active revision switched to trans-rev-2 upon successful completion
    inv_after = repo.get_interview(interview_id)
    assert inv_after["active_transcript_revision_id"] == "trans-rev-2"

    # 2. Verify human assessment is marked stale with reason (scores preserved in history)
    ha_after = repo.get_human_assessments(interview_id)[0]
    assert ha_after["is_stale"] == 1
    assert "расхождения" in ha_after["stale_reason"]
    assert ha_after["scores"][0]["score"] == 5.0

    # 3. Verify executive summary is invalidated (is_confirmed = 0)
    sum_after = repo.get_summary_proposal(interview_id)
    assert sum_after["is_confirmed"] == 0

    # 4. Verify Finalization is blocked with 409 Conflict due to stale human assessment
    r_fin = client.post(
        f"/api/v1/interviews/{interview_id}/report/finalize",
        json={
            "confirmed_by": "reviewer-alice",
            "summary_markdown": "# Резюме",
            "hiring_recommendation": "Hire",
        },
    )
    assert r_fin.status_code == 409
    assert "stale" in r_fin.json()["detail"].lower()


def test_optimistic_concurrency_blocks_stale_revisions(client: TestClient, repo: Repository):
    """
    Acceptance Criteria 5: Проверка optimistic concurrency и 409 при несовпадающих ревизиях.
    """
    interview_id = "inv-opt-conflict-1"
    _setup_interview_stage8(repo, interview_id)

    # Set active revision to trans-rev-2
    repo.update_interview_status(interview_id, "processing")
    repo.update_interview_status(interview_id, "review")
    repo.set_active_transcript_revision(interview_id, "trans-rev-2")

    # 1. Review submitted for trans-rev-1 must be blocked with 409
    r_rev = client.post(
        f"/api/v1/interviews/{interview_id}/assessments/q1/review",
        json={
            "expected_transcript_revision": "trans-rev-1",  # Stale!
            "scores": [{"criterion_id": "crit-redis", "score": 4.0}],
            "reviewer_notes": "Late review from outdated tab",
        },
    )
    assert r_rev.status_code == 409
    assert "conflict" in r_rev.json()["detail"].lower()
    assert "trans-rev-2" in r_rev.json()["detail"]

    # 2. Summary confirmation submitted for trans-rev-1 must be blocked with 409
    r_sum = client.post(
        f"/api/v1/interviews/{interview_id}/summary/confirm",
        json={
            "reviewer_id": "reviewer-alice",
            "confirmed_markdown": "# Markdown",
            "confirmed_recommendation": "Hire",
            "expected_transcript_revision": "trans-rev-1",  # Stale!
        },
    )
    assert r_sum.status_code == 409
    assert "conflict" in r_sum.json()["detail"].lower()

    # 3. Finalization submitted for trans-rev-1 must be blocked with 409
    r_fin = client.post(
        f"/api/v1/interviews/{interview_id}/report/finalize",
        json={
            "confirmed_by": "reviewer-alice",
            "summary_markdown": "# Markdown",
            "hiring_recommendation": "Hire",
            "expected_transcript_revision": "trans-rev-1",  # Stale!
        },
    )
    assert r_fin.status_code == 409
    assert "conflict" in r_fin.json()["detail"].lower()


@pytest.mark.asyncio
async def test_failed_batch_does_not_switch_active_transcript(repo: Repository):
    """
    Acceptance Criteria 4: Незавершённый batch не переключает активную стенограмму.
    """
    interview_id = "inv-failed-batch-1"
    _setup_interview_stage8(repo, interview_id)

    # Initial active revision
    assert repo.get_interview(interview_id)["active_transcript_revision_id"] == "trans-rev-1"

    # Worker with failing STT
    mock_failing_stt = MagicMock()
    mock_failing_stt.transcribe_audio = AsyncMock(side_effect=RuntimeError("Whisper upstream 503"))

    worker = PipelineWorker(repo, stt_adapter=mock_failing_stt)

    # Save audio chunk record with invalid file path
    chunk_file = Path("/tmp/nonexistent-audio-chunk.chunk")
    repo.enqueue_job(
        job_id="job-failing-batch-1",
        job_type="BATCH_RETRANSCRIBE",
        interview_id=interview_id,
        payload={
            "new_revision_id": "trans-rev-2",
            "old_revision_id": "trans-rev-1",
            "segments": None,  # Requests transcription from audio
        },
        max_attempts=1,
    )

    # Mock audio_chunks in repo
    repo.save_audio_chunk(
        interview_id=interview_id,
        track_id="candidate",
        capture_epoch=1,
        sequence=1,
        start_time_ms=0,
        end_time_ms=2000,
        sample_rate=16000,
        channels=1,
        sample_count=32000,
        format_str="pcm_s16le",
        checksum_sha256="abc",
        payload_bytes=b"dummy-pcm-bytes" * 100,
        spool_base_dir="/tmp/spool-test-stg8",
    )

    # Processing fails because mock STT throws
    processed = await worker.process_one_job()
    assert processed is False

    # CRITICAL INVARIANT: active_transcript_revision_id must still be trans-rev-1!
    inv = repo.get_interview(interview_id)
    assert inv["active_transcript_revision_id"] == "trans-rev-1"
