import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.adapters.llm import LLMAuthenticationError
from backend.adapters.resilient_llm import ResilientLLMAdapter
from backend.adapters.stt import OpenAICompatibleSTTAdapter, STTRateLimitError
from backend.core.turn_assembler import AssembledTurn
from backend.db.database import Database
from backend.db.repository import Repository, RepositoryConflictError
from backend.workers.pipeline import PipelineWorker
from contracts.domain import InterviewStatus


def _setup_interview(repo: Repository, interview_id: str = "inv-stage3-1") -> str:
    plan = {
        "title": "Stage 3 Concurrency Interview",
        "questions": [
            {
                "id": "q1",
                "title": "Architecture",
                "prompt": "Explain Event Loop",
                "criteria": [
                    {
                        "id": "c1",
                        "title": "Event Loop",
                        "description": "Details",
                        "min_score": 1,
                        "max_score": 5,
                        "weight": 1.0,
                    }
                ],
            }
        ],
    }
    repo.create_interview(
        interview_id=interview_id,
        title="Senior Engineer",
        candidate_name="Сергей",
        role="Backend",
        status=InterviewStatus.RECORDING,
    )
    repo.save_plan(f"plan-{interview_id}", interview_id, plan, version=1)
    return interview_id


def test_stt_result_rejected_after_owner_change_or_lease_expiry(tmp_path):
    """
    Acceptance 1: If job ownership changes or lease expires during provider call,
    the old worker cannot write STT results (atomically rejected with conflict).
    """
    db = Database(tmp_path / "test_owner_change.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-owner-1")

    repo.enqueue_job("job-stt-1", "TRANSCRIBE_TURN", inv_id, {"track_id": "candidate"})
    claimed = repo.claim_next_job(lock_duration_sec=60)
    assert claimed is not None
    old_owner = claimed["locked_by"]

    # Simulate worker crash / lease timeout: another worker reclaims the job
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET locked_by = 'new-worker-token-xyz' WHERE id = 'job-stt-1'")

    # Old worker tries to commit STT result via save_turn_transcript_segment
    with pytest.raises(RepositoryConflictError, match="owner token mismatch"):
        repo.save_turn_transcript_segment(
            segment_id="seg-late-1",
            interview_id=inv_id,
            track_id="candidate",
            start_time_ms=0,
            end_time_ms=2000,
            text="Late transcription",
            target_revision_id="trans-rev-1",
            owner_token=old_owner,
            job_id="job-stt-1",
        )

    # Verify no segment was written
    segs = repo.get_transcript_segments(inv_id, revision_id="trans-rev-1")
    assert len(segs) == 0


def test_revision_change_does_not_overwrite_manual_edits(tmp_path):
    """
    Acceptance 2: If a user created a new transcript revision with manual edits,
    a late STT result targeting the old revision does not overwrite the manual edit in active_rev.
    """
    db = Database(tmp_path / "test_rev_safety.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-rev-1")

    # 1. User manual edit creates revision 2 with corrected text and human role
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=inv_id,
        track_id="candidate",
        start_time_ms=1000,
        end_time_ms=3000,
        text="Ручная правка кандидата: правильный ответ",
        revision_id="trans-rev-2",
        speaker_role="candidate",
    )
    # Update active revision in interview to trans-rev-2
    with db.transaction() as conn:
        conn.execute("UPDATE interviews SET active_transcript_revision_id = 'trans-rev-2' WHERE id = ?", (inv_id,))

    # 2. STT job was running for trans-rev-1 and finishes with raw speech
    repo.enqueue_job("job-stt-rev", "TRANSCRIBE_TURN", inv_id, {"track_id": "candidate"})
    claimed = repo.claim_next_job()
    token = claimed["locked_by"]

    # Save turn STT segment targeting trans-rev-1
    repo.save_turn_transcript_segment(
        segment_id="seg-1",
        interview_id=inv_id,
        track_id="candidate",
        start_time_ms=1000,
        end_time_ms=3000,
        text="Сырой распознанный текст сомнительного качества",
        target_revision_id="trans-rev-1",
        speaker_role="unknown",
        owner_token=token,
        job_id="job-stt-rev",
    )

    # 3. Verify that trans-rev-2 still has the manual human edit intact!
    segs_rev2 = repo.get_transcript_segments(inv_id, revision_id="trans-rev-2")
    assert len(segs_rev2) == 1
    assert segs_rev2[0]["text"] == "Ручная правка кандидата: правильный ответ"
    assert segs_rev2[0]["speaker_role"] == "candidate"

    # Trans-rev-1 has the raw STT text recorded for audit history
    segs_rev1 = repo.get_transcript_segments(inv_id, revision_id="trans-rev-1")
    assert len(segs_rev1) == 1
    assert segs_rev1[0]["text"] == "Сырой распознанный текст сомнительного качества"


def test_turn_assembly_cas_and_anti_regression(tmp_path):
    """
    Acceptance 3: Concurrent flush and assembly cannot move cursor backwards
    and CAS rejects commits if the cursor was advanced in between.
    """
    db = Database(tmp_path / "test_cas.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-cas-1")

    turn1 = AssembledTurn(
        track_id="candidate",
        capture_epoch=1,
        first_sequence=0,
        first_sample_offset=0,
        last_sequence=2,
        last_sample_offset=16000,
        start_ms=0,
        end_ms=2000,
        sample_rate=16000,
        channels=1,
        language="ru",
    )

    # First assembly advances cursor from (0, 0) to (3, 0)
    jobs1 = repo.commit_turn_assembly_results(
        interview_id=inv_id,
        track_id="candidate",
        capture_epoch=1,
        next_sequence=3,
        next_sample_offset=0,
        turns=[turn1],
        expected_sequence=0,
        expected_sample_offset=0,
    )
    assert len(jobs1) == 1
    seq, off = repo.get_turn_assembly_cursor(inv_id, "candidate", 1)
    assert seq == 3
    assert off == 0

    # Concurrent stale process expected cursor (0, 0) and tries to commit (2, 0) -> Rejected by CAS and regression
    stale_jobs = repo.commit_turn_assembly_results(
        interview_id=inv_id,
        track_id="candidate",
        capture_epoch=1,
        next_sequence=2,
        next_sample_offset=0,
        turns=[turn1],
        expected_sequence=0,
        expected_sample_offset=0,
    )
    assert stale_jobs == []
    # Cursor remained at (3, 0)
    seq_after, off_after = repo.get_turn_assembly_cursor(inv_id, "candidate", 1)
    assert seq_after == 3
    assert off_after == 0

    # Advance cursor cleanly to (5, 0)
    turn2 = AssembledTurn(
        track_id="candidate",
        capture_epoch=1,
        first_sequence=3,
        first_sample_offset=0,
        last_sequence=4,
        last_sample_offset=16000,
        start_ms=2000,
        end_ms=4000,
        sample_rate=16000,
        channels=1,
        language="ru",
    )
    jobs2 = repo.commit_turn_assembly_results(
        interview_id=inv_id,
        track_id="candidate",
        capture_epoch=1,
        next_sequence=5,
        next_sample_offset=0,
        turns=[turn2],
        expected_sequence=3,
        expected_sample_offset=0,
    )
    assert len(jobs2) == 1
    seq_final, _ = repo.get_turn_assembly_cursor(inv_id, "candidate", 1)
    assert seq_final == 5


def test_finalized_interview_rejects_modifications(tmp_path):
    """
    Acceptance 4: Finalized interview is immutable and rejects STT/proposals/commits.
    """
    db = Database(tmp_path / "test_finalized.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-fin-1")

    with db.transaction() as conn:
        conn.execute("UPDATE interviews SET status = 'finalized' WHERE id = ?", (inv_id,))

    # Cannot save STT
    with pytest.raises(RepositoryConflictError, match="finalized and immutable"):
        repo.save_turn_transcript_segment(
            segment_id="seg-f",
            interview_id=inv_id,
            track_id="candidate",
            start_time_ms=0,
            end_time_ms=1000,
            text="hello",
            target_revision_id="trans-rev-1",
        )

    # Cannot save proposal
    with pytest.raises(RepositoryConflictError, match="finalized and immutable"):
        repo.save_assessment_proposal(
            proposal_id="prop-f",
            interview_id=inv_id,
            question_id="q1",
            model_profile_id="m",
            scores=[],
        )


@pytest.mark.asyncio
async def test_resilient_llm_adapter_auth_error_no_fallback_and_clean_event():
    """
    Acceptance 8: LLMAuthenticationError does not trigger fallback loop on shared provider,
    and last_fallback_event is strictly isolated and reset per call.
    """
    adapter = ResilientLLMAdapter()

    # Mock primary adapter raising LLMAuthenticationError
    adapter.primary_adapter.execute_request = AsyncMock(side_effect=LLMAuthenticationError("Invalid API key 401"))
    adapter.fallback_adapter_1.execute_request = AsyncMock()

    with pytest.raises(LLMAuthenticationError):
        await adapter.execute_request(messages=[{"role": "user", "content": "hi"}])

    # Must NOT call fallback adapter 1 when auth fails
    adapter.fallback_adapter_1.execute_request.assert_not_called()
    assert adapter.last_fallback_event is None

    # Now test normal fallback on transient error:
    adapter.primary_adapter.execute_request = AsyncMock(side_effect=RuntimeError("500 internal server error"))
    adapter.fallback_adapter_1.execute_request = AsyncMock(return_value={"scores": []})

    res, actual_model = await adapter.execute_request(messages=[{"role": "user", "content": "hi"}])
    assert actual_model == adapter.fallback_model_1.upstream_model_id
    assert adapter.last_fallback_event is not None
    assert adapter.last_fallback_event["target_model"] == adapter.fallback_model_1.upstream_model_id

    # Next call succeeds on primary: last_fallback_event must be reset to None!
    adapter.primary_adapter.execute_request = AsyncMock(return_value={"scores": [1]})
    res2, actual_model2 = await adapter.execute_request(messages=[{"role": "user", "content": "hi"}])
    assert actual_model2 == adapter.primary_model.upstream_model_id
    assert adapter.last_fallback_event is None


@pytest.mark.asyncio
async def test_job_execution_deadline_and_rate_limit_backoff(tmp_path):
    """
    Acceptance 5 & 6:
    - If job execution exceeds deadline, it raises TimeoutError and is moved to durable retry.
    - STT 429 raises STTRateLimitError with retry_after and does not sleep inside adapter.
    """
    db = Database(tmp_path / "test_deadline.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-dead-1")

    # 1. Test STT Rate Limit does not sleep in adapter
    def rate_limit_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "15"}, text="Too Many Requests")

    from backend.core.profiles import get_plusvibe_whisper_stt
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(rate_limit_handler))
    stt = OpenAICompatibleSTTAdapter(
        profile=get_plusvibe_whisper_stt(),
        http_client=mock_client,
    )
    t0 = time.perf_counter()
    with pytest.raises(STTRateLimitError) as exc_info:
        await stt.transcribe_audio(b"fake wav bytes", max_retries=1)
    duration = time.perf_counter() - t0
    # Must raise immediately without sleeping 15 seconds
    assert duration < 1.0
    assert exc_info.value.retry_after == 15.0

    # 2. Test worker deadline handling
    repo.add_transcript_segment(
        segment_id="seg-cand-1",
        interview_id=inv_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5000,
        text="Я использовал PostgreSQL и Redis для кэширования.",
        speaker_role="candidate",
    )
    repo.save_association(
        assoc_id="assoc-1",
        interview_id=inv_id,
        question_id="q1",
        segment_id="seg-cand-1",
        confidence=0.9,
    )
    repo.enqueue_job("job-timeout-1", "EVALUATE_QUESTION", inv_id, {"question_id": "q1"})

    # Worker with slow mock adapter exceeding deadline
    async def slow_execute(*args, **kwargs):
        await asyncio.sleep(5.0)
        return {"scores": []}, "mock-model"

    mock_llm = AsyncMock()
    mock_llm.execute_request = slow_execute

    worker = PipelineWorker(repo, llm_adapter=mock_llm)
    # Monkeypatch deadline for EVALUATE_QUESTION to 0.1s for fast test
    from backend.workers import pipeline
    orig_deadline = pipeline.DEFAULT_JOB_DEADLINES["EVALUATE_QUESTION"]
    pipeline.DEFAULT_JOB_DEADLINES["EVALUATE_QUESTION"] = 0.1

    try:
        did_work = await worker.process_one_job()
        assert did_work is False

        # Job must be in PENDING status with retry delay, attempts incremented
        status = repo.get_interview_jobs_status(inv_id, job_ids=["job-timeout-1"])
        job_info = status["active_jobs"][0]
        assert job_info["status"] == "PENDING"
        assert job_info["attempts"] == 1
        assert "TimeoutError" in str(job_info["error_message"]) or "timed out" in str(job_info["error_message"])
    finally:
        pipeline.DEFAULT_JOB_DEADLINES["EVALUATE_QUESTION"] = orig_deadline
