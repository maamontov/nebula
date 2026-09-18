import asyncio
import contextlib
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from backend.adapters.stt import STTTranscriptionResult
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker
from contracts.domain import InterviewStatus


def _save_dummy_chunk(
    repo: Repository,
    interview_id: str,
    track_id: str,
    seq: int,
    start_ms: int,
    end_ms: int,
    spool_dir: Path,
) -> None:
    sample_count = int((end_ms - start_ms) * 16)
    raw_bytes = b"\x01\x00" * sample_count
    sha = hashlib.sha256(raw_bytes).hexdigest()
    repo.save_audio_chunk(
        interview_id=interview_id,
        track_id=track_id,
        capture_epoch=1,
        sequence=seq,
        start_time_ms=start_ms,
        end_time_ms=end_ms,
        sample_rate=16000,
        channels=1,
        sample_count=sample_count,
        format_str="pcm_s16le",
        checksum_sha256=sha,
        payload_bytes=raw_bytes,
        spool_base_dir=spool_dir,
    )


def _setup_interview(repo: Repository, interview_id: str = "inv-stage4-1") -> str:
    plan = {
        "title": "Stage 4 Consumers Interview",
        "questions": [
            {
                "id": "q-kafka",
                "title": "Kafka",
                "prompt": "Explain Kafka architecture",
                "criteria": [
                    {
                        "id": "crit-1",
                        "title": "Partitions and ordering",
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
        title="Senior Backend Engineer",
        candidate_name="Алексей",
        role="Go Developer",
        status=InterviewStatus.RECORDING,
    )
    repo.save_plan(f"plan-{interview_id}", interview_id, plan, version=1)
    return interview_id


@pytest.mark.asyncio
async def test_stage4_independent_consumers_no_blocking_on_slow_jobs(tmp_path):
    """
    Acceptance 1:
    Slow/blocked assessment and background retranscribe jobs MUST NOT block
    assembly (TRANSCRIBE_AUDIO), live STT (TRANSCRIBE_TURN), and follow-ups.
    Uses synchronization barriers (asyncio.Event) rather than fragile sleeps.
    """
    db = Database(tmp_path / "test_consumers_isolation.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-stage4-iso")

    eval_barrier = asyncio.Event()
    bg_barrier = asyncio.Event()

    # 1. Enqueue slow EVALUATE_QUESTION (assessment consumer)
    repo.add_transcript_segment(
        segment_id="seg-iso-1",
        interview_id=inv_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=5000,
        text="Я настраивал Kafka cluster для real-time pipeline.",
        speaker_role="candidate",
    )
    repo.save_association(
        assoc_id="assoc-iso-1",
        interview_id=inv_id,
        question_id="q-kafka",
        segment_id="seg-iso-1",
        confidence=0.9,
    )
    repo.enqueue_job("job-eval-slow", "EVALUATE_QUESTION", inv_id, {"question_id": "q-kafka"})

    # 2. Enqueue slow GENERATE_SUMMARY (background consumer)
    repo.enqueue_job("job-summary-slow", "GENERATE_SUMMARY", inv_id, {"audio_health": {}})

    # 3. Enqueue fast live STT (live STT consumer)
    repo.enqueue_job("job-turn-fast", "TRANSCRIBE_TURN", inv_id, {
        "track_id": "candidate",
        "start_ms": 10000,
        "end_ms": 12000,
        "first_sequence": 1,
        "first_sample_offset": 0,
        "last_sequence": 1,
        "last_sample_offset": 32000,
        "capture_epoch": 1,
    })
    # Add dummy turn audio so it transcribes
    _save_dummy_chunk(repo, inv_id, "candidate", 1, 10000, 12000, tmp_path / "spool")

    # 4. Enqueue fast follow-ups (followups consumer)
    req_dict, _, _, _ = repo.create_followup_request_and_job(
        interview_id=inv_id,
        question_id="q-kafka",
        mode="probe",
        trigger="manual",
        candidate_fingerprint="fp-fu",
        context_hash="hash-fu",
        context_json=json.dumps({
            "role_title": "Backend",
            "decisions_history": [],
            "question": {
                "id": "q-kafka",
                "title": "Kafka",
                "prompt": "Explain Kafka architecture",
                "criteria": [
                    {
                        "id": "crit-1",
                        "title": "Kafka",
                        "description": "Partitions",
                        "min_score": 1,
                        "max_score": 5,
                        "weight": 1.0,
                    }
                ],
            },
        }),
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
    )
    job_fu_id = req_dict["job_id"]

    # Mock LLM Adapter
    async def mock_llm_execute(*args, **kwargs):
        # If this is evaluate, wait on eval_barrier
        schema_name = kwargs.get("schema_name", "")
        if "assessment" in schema_name or any("scores" in str(m) for m in kwargs.get("messages", [])):
            await eval_barrier.wait()
            return {
                "scores": [
                    {
                        "criterion_id": "crit-1",
                        "score": 4.0,
                        "explanation": "Отличные знания Kafka и партиционирования.",
                        "evidence": [{"segment_id": "seg-iso-1", "exact_quote": "Kafka cluster"}],
                    }
                ]
            }, "mock-gemini"
        elif "summary" in schema_name:
            await bg_barrier.wait()
            return {
                "key_strengths": [{"title": "Kafka", "evidence_quote": "Kafka cluster"}],
                "growth_areas": [],
                "recommendation": "hire",
            }, "mock-gemini"
        return {}, "mock-gemini"

    mock_llm = MagicMock()
    mock_llm.execute_request = AsyncMock(side_effect=mock_llm_execute)
    mock_llm.last_fallback_event = None

    # Mock STT Adapter
    mock_stt = MagicMock()
    mock_stt.transcribe_audio = AsyncMock(
        return_value=STTTranscriptionResult(
            text="Использовали partition key для сохранения порядка сообщений.",
            model_id="whisper-turbo",
            latency_seconds=0.1,
            raw_response={},
        )
    )

    # Mock Followup Adapter
    mock_fu = MagicMock()
    mock_fu.model.id = "google/gemini-3.8-flash"
    mock_fu.model.upstream_model_id = "mock-gemini-fu"
    mock_fu.provider.id = "plusvibe"
    mock_fu.execute_request = AsyncMock(
        return_value={
            "data": {
                "suggestions": [
                    {
                        "kind": "deepen",
                        "question_text": "Как решали проблему rebalance?",
                        "purpose": "Уточнить детали работы с consumer group",
                        "criterion_ids": ["crit-1"],
                    }
                ]
            }
        }
    )

    worker = PipelineWorker(
        repository=repo,
        stt_adapter=mock_stt,
        llm_adapter=mock_llm,
        followup_llm_adapter=mock_fu,
    )

    loop_task = asyncio.create_task(worker.run_loop(poll_interval_sec=0.05))

    try:
        # Wait for live STT and Follow-up jobs to finish while eval and background are still locked!
        for _ in range(50):
            stt_job = repo.get_interview_jobs_status(inv_id, job_ids=["job-turn-fast"])["active_jobs"]
            fu_job = repo.get_interview_jobs_status(inv_id, job_ids=[job_fu_id])["active_jobs"]
            if stt_job and stt_job[0]["status"] == "COMPLETED" and fu_job and fu_job[0]["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)

        # Confirm fast live jobs finished successfully without waiting for slow jobs!
        assert stt_job[0]["status"] == "COMPLETED"
        assert fu_job[0]["status"] == "COMPLETED"

        # Slow jobs must still be PROCESSING/PENDING
        eval_info = repo.get_interview_jobs_status(inv_id, job_ids=["job-eval-slow"])["active_jobs"][0]
        assert eval_info["status"] in ("PROCESSING", "PENDING")

        # Now release barriers
        eval_barrier.set()
        bg_barrier.set()

        for _ in range(50):
            eval_info = repo.get_interview_jobs_status(inv_id, job_ids=["job-eval-slow"])["active_jobs"][0]
            if eval_info["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert eval_info["status"] == "COMPLETED"

    finally:
        eval_barrier.set()
        bg_barrier.set()
        worker.stop()
        try:
            await asyncio.wait_for(loop_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task


@pytest.mark.asyncio
async def test_stage4_provider_concurrency_cap_strictly_enforced(tmp_path):
    """
    Acceptance 2:
    Overall in-flight calls to provider MUST NEVER exceed provider_concurrency_cap.
    """
    db = Database(tmp_path / "test_cap.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-stage4-cap")

    cap = 2
    current_in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def tracked_call(*args, **kwargs):
        nonlocal current_in_flight, max_in_flight
        async with lock:
            current_in_flight += 1
            if current_in_flight > max_in_flight:
                max_in_flight = current_in_flight
        await asyncio.sleep(0.1)
        async with lock:
            current_in_flight -= 1
        return STTTranscriptionResult(text="ok", model_id="m", latency_seconds=0.1, raw_response={})

    mock_stt = MagicMock()
    mock_stt.transcribe_audio = AsyncMock(side_effect=tracked_call)

    worker = PipelineWorker(
        repository=repo,
        stt_adapter=mock_stt,
        llm_adapter=MagicMock(),
        provider_concurrency_cap=cap,
    )

    # Enqueue 5 TRANSCRIBE_TURN jobs
    for i in range(5):
        _save_dummy_chunk(repo, inv_id, "candidate", i, i * 1000, (i + 1) * 1000, tmp_path / "spool")
        repo.enqueue_job(f"job-turn-{i}", "TRANSCRIBE_TURN", inv_id, {
            "track_id": "candidate",
            "start_ms": i * 1000,
            "end_ms": (i + 1) * 1000,
            "first_sequence": i,
            "first_sample_offset": 0,
            "last_sequence": i,
            "last_sample_offset": 16000,
            "capture_epoch": 1,
        })

    loop_task = asyncio.create_task(worker.run_loop(poll_interval_sec=0.02))

    try:
        for _ in range(100):
            status = repo.get_interview_jobs_status(inv_id)
            if status["counts"].get("COMPLETED") == 5:
                break
            await asyncio.sleep(0.05)

        assert max_in_flight <= cap
        assert max_in_flight >= 1
    finally:
        worker.stop()
        try:
            await asyncio.wait_for(loop_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task


@pytest.mark.asyncio
async def test_stage4_out_of_order_stt_ordered_timeline(tmp_path):
    """
    Acceptance 3:
    Out-of-order STT results have correct audio timeline, roles, and revision.
    """
    db = Database(tmp_path / "test_stt_order.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = _setup_interview(repo, "inv-stage4-order")

    # Enqueue 3 turns with start_ms 0, 3000, 6000
    for i in range(3):
        _save_dummy_chunk(repo, inv_id, "candidate", i, i * 3000, (i + 1) * 3000, tmp_path / "spool")
        repo.enqueue_job(f"job-turn-order-{i}", "TRANSCRIBE_TURN", inv_id, {
            "track_id": "candidate",
            "start_ms": i * 3000,
            "end_ms": (i + 1) * 3000,
            "first_sequence": i,
            "first_sample_offset": 0,
            "last_sequence": i,
            "last_sample_offset": 48000,
            "capture_epoch": 1,
            "transcript_revision_id": "trans-rev-1",
        })

    # Mock STT: turn 0 delays by 0.3s, turns 1 and 2 finish in 0.02s
    async def delayed_stt(audio_data, *args, **kwargs):
        filename = kwargs.get("filename", "")
        if "_0_3000" in filename:
            await asyncio.sleep(0.3)
            return STTTranscriptionResult(text="Первая реплика.", model_id="m", latency_seconds=0.3, raw_response={})
        elif "_3000_6000" in filename:
            await asyncio.sleep(0.02)
            return STTTranscriptionResult(text="Вторая реплика.", model_id="m", latency_seconds=0.02, raw_response={})
        else:
            await asyncio.sleep(0.02)
            return STTTranscriptionResult(text="Третья реплика.", model_id="m", latency_seconds=0.02, raw_response={})

    mock_stt = MagicMock()
    mock_stt.transcribe_audio = AsyncMock(side_effect=delayed_stt)

    worker = PipelineWorker(
        repository=repo,
        stt_adapter=mock_stt,
        llm_adapter=MagicMock(),
    )

    loop_task = asyncio.create_task(worker.run_loop(poll_interval_sec=0.02))

    try:
        for _ in range(80):
            st = repo.get_interview_jobs_status(inv_id)
            if st["counts"].get("COMPLETED") == 3:
                break
            await asyncio.sleep(0.05)

        segs = repo.get_transcript_segments(inv_id, revision_id="trans-rev-1")
        assert len(segs) == 3
        # Timeline must be strictly sorted by start_time_ms
        assert [s["start_time_ms"] for s in segs] == [0, 3000, 6000]
        assert segs[0]["text"] == "Первая реплика."
        assert segs[1]["text"] == "Вторая реплика."
        assert segs[2]["text"] == "Третья реплика."
    finally:
        worker.stop()
        try:
            await asyncio.wait_for(loop_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task


@pytest.mark.asyncio
async def test_stage4_http_client_reuse_and_close(tmp_path):
    """
    Acceptance 4:
    PipelineWorker reuses httpx.AsyncClient and properly closes it on shutdown/close.
    """
    db = Database(tmp_path / "test_client_reuse.db")
    db.init_schema()
    repo = Repository(db)

    custom_client = httpx.AsyncClient()
    worker = PipelineWorker(repository=repo, http_client=custom_client)
    assert worker._http_client is custom_client
    assert worker._owns_http_client is False

    # Closing worker with external client should not close external client
    await worker.close()
    assert not custom_client.is_closed
    await custom_client.aclose()

    # Worker with default client owns it and closes on close()
    worker_default = PipelineWorker(repository=repo)
    assert worker_default._owns_http_client is True
    default_client = worker_default._http_client
    assert not default_client.is_closed

    await worker_default.close()
    assert default_client.is_closed


def _setup_multi_question_interview(repo: Repository, interview_id: str, question_count: int) -> list[str]:
    """Creates an interview whose plan holds `question_count` independently evaluable questions."""
    questions = [
        {
            "id": f"q-{i}",
            "title": f"Question {i}",
            "prompt": f"Explain topic {i}",
            "criteria": [
                {
                    "id": f"crit-{i}",
                    "title": f"Criterion {i}",
                    "description": "Details",
                    "min_score": 1,
                    "max_score": 5,
                    "weight": 1.0,
                }
            ],
        }
        for i in range(question_count)
    ]
    repo.create_interview(
        interview_id=interview_id,
        title="Senior Backend Engineer",
        candidate_name="Алексей",
        role="Go Developer",
        status=InterviewStatus.RECORDING,
    )
    repo.save_plan(f"plan-{interview_id}", interview_id, {"title": "Plan", "questions": questions}, version=1)

    for i in range(question_count):
        repo.add_transcript_segment(
            segment_id=f"seg-{i}",
            interview_id=interview_id,
            track_id="candidate",
            start_time_ms=i * 1000,
            end_time_ms=(i + 1) * 1000,
            text=f"Ответ кандидата номер {i} про партиционирование и шардирование.",
            speaker_role="candidate",
        )
        repo.save_association(
            assoc_id=f"assoc-{i}",
            interview_id=interview_id,
            question_id=f"q-{i}",
            segment_id=f"seg-{i}",
            confidence=0.9,
        )
    return [q["id"] for q in questions]


def _assessment_response(criterion_id: str) -> tuple[dict, str]:
    return (
        {
            "scores": [
                {
                    "criterion_id": criterion_id,
                    "score": 4.0,
                    "explanation": "Ответ содержательный.",
                    "evidence": [],
                }
            ],
            "critical_errors": [],
        },
        "mock-model",
    )


@pytest.mark.asyncio
async def test_stage4_assessment_questions_evaluated_in_parallel(tmp_path):
    """
    Acceptance 5:
    Independent questions of one review run MUST be evaluated concurrently instead of
    one-by-one. Regression guard for the serial assessment consumer that made a 10-question
    review take as long as the sum of every question.
    """
    db = Database(tmp_path / "test_assessment_parallel.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = "inv-assess-parallel"
    question_count = 6
    q_ids = _setup_multi_question_interview(repo, inv_id, question_count)

    in_flight = 0
    max_in_flight = 0
    counter_lock = asyncio.Lock()
    all_slots_busy = asyncio.Event()
    expected_parallel = 5  # DEFAULT_ASSESSMENT_CONCURRENCY

    async def mock_llm_execute(*args, **kwargs):
        nonlocal in_flight, max_in_flight
        async with counter_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if in_flight >= expected_parallel:
                all_slots_busy.set()
        # Hold the slot so the peak concurrency is observable, then release together.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(all_slots_busy.wait(), timeout=3.0)
        async with counter_lock:
            in_flight -= 1
        return _assessment_response("crit-0")

    mock_llm = MagicMock()
    mock_llm.execute_request = AsyncMock(side_effect=mock_llm_execute)

    worker = PipelineWorker(repository=repo, llm_adapter=mock_llm)
    assert worker._assessment_concurrency == expected_parallel

    for i, q_id in enumerate(q_ids):
        repo.enqueue_job(f"job-par-eval-{i}", "EVALUATE_QUESTION", inv_id, {"question_id": q_id})

    loop_task = asyncio.create_task(worker.run_loop(poll_interval_sec=0.02))
    try:
        await asyncio.wait_for(all_slots_busy.wait(), timeout=5.0)
        assert max_in_flight >= expected_parallel

        for _ in range(100):
            counts = repo.get_interview_jobs_status(inv_id)["counts"]
            if counts.get("COMPLETED") == question_count:
                break
            await asyncio.sleep(0.05)
        assert repo.get_interview_jobs_status(inv_id)["counts"].get("COMPLETED") == question_count
    finally:
        all_slots_busy.set()
        worker.stop()
        try:
            await asyncio.wait_for(loop_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task


@pytest.mark.asyncio
async def test_stage4_assessment_parallelism_does_not_starve_live_stt(tmp_path):
    """
    Acceptance 6:
    Saturating every assessment slot MUST NOT block live recognition. This is the guarantee
    that makes widening assessment fan-out safe: the reservation is what fails if someone
    raises the fan-out without leaving permits for the live path.
    """
    db = Database(tmp_path / "test_no_starvation.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = "inv-nostarve"
    question_count = 5
    q_ids = _setup_multi_question_interview(repo, inv_id, question_count)

    release_assessments = asyncio.Event()
    in_flight = 0
    counter_lock = asyncio.Lock()
    all_slots_busy = asyncio.Event()

    async def mock_llm_execute(*args, **kwargs):
        nonlocal in_flight
        async with counter_lock:
            in_flight += 1
            if in_flight >= worker._assessment_concurrency:
                all_slots_busy.set()
        await release_assessments.wait()
        async with counter_lock:
            in_flight -= 1
        return _assessment_response("crit-0")

    mock_llm = MagicMock()
    mock_llm.execute_request = AsyncMock(side_effect=mock_llm_execute)

    mock_stt = MagicMock()
    mock_stt.transcribe_audio = AsyncMock(
        return_value=STTTranscriptionResult(
            text="Живая реплика кандидата.", model_id="m", latency_seconds=0.1, raw_response={}
        )
    )

    # Deliberately tight cap with a fan-out request larger than the cap. The reservation is the
    # only thing keeping permits free for live STT here: without it, every permit is taken by
    # blocked assessments and the live turn below can never run.
    worker = PipelineWorker(
        repository=repo,
        stt_adapter=mock_stt,
        llm_adapter=mock_llm,
        provider_concurrency_cap=5,
        assessment_concurrency=5,
    )
    assert worker._assessment_concurrency == 1
    assert worker._assessment_concurrency + 4 <= worker._provider_concurrency_cap

    for i, q_id in enumerate(q_ids):
        repo.enqueue_job(f"job-nostarve-eval-{i}", "EVALUATE_QUESTION", inv_id, {"question_id": q_id})

    loop_task = asyncio.create_task(worker.run_loop(poll_interval_sec=0.02))
    try:
        await asyncio.wait_for(all_slots_busy.wait(), timeout=5.0)

        # Every assessment slot is blocked. Live STT must still be served.
        _save_dummy_chunk(repo, inv_id, "candidate", 1, 20000, 22000, tmp_path / "spool")
        repo.enqueue_job(
            "job-nostarve-turn",
            "TRANSCRIBE_TURN",
            inv_id,
            {
                "track_id": "candidate",
                "start_ms": 20000,
                "end_ms": 22000,
                "first_sequence": 1,
                "first_sample_offset": 0,
                "last_sequence": 1,
                "last_sample_offset": 32000,
                "capture_epoch": 1,
            },
        )

        turn_status = None
        for _ in range(60):
            turn_status = repo.get_interview_jobs_status(inv_id, job_ids=["job-nostarve-turn"])["active_jobs"]
            if turn_status and turn_status[0]["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        assert turn_status and turn_status[0]["status"] == "COMPLETED", (
            "live STT was starved while all assessment slots were busy"
        )
    finally:
        release_assessments.set()
        worker.stop()
        try:
            await asyncio.wait_for(loop_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task


@pytest.mark.asyncio
async def test_stage4_assessment_concurrency_clamped_to_protect_live_path(tmp_path):
    """
    Acceptance 7:
    The fan-out defaults to the provider's declared max_concurrency, and is clamped so that it
    can never consume the permits reserved for live STT, follow-ups and background work.
    """
    db = Database(tmp_path / "test_clamp.db")
    db.init_schema()
    repo = Repository(db)

    client = httpx.AsyncClient()
    default_worker = PipelineWorker(repository=repo, http_client=client)
    # Provider profile declares max_concurrency=10, so the cap follows it instead of a guess.
    assert default_worker._provider_concurrency_cap == 10
    assert default_worker._assessment_concurrency == 5
    assert default_worker._live_stt_concurrency == 2

    # A cap with no room for the other consumers clamps assessment to a single consumer.
    small_worker = PipelineWorker(repository=repo, http_client=client, provider_concurrency_cap=5)
    assert small_worker._assessment_concurrency == 1

    # An explicit fan-out is honoured while the cap still leaves room for everyone else.
    wide_worker = PipelineWorker(
        repository=repo, http_client=client, provider_concurrency_cap=10, assessment_concurrency=6
    )
    assert wide_worker._assessment_concurrency == 6

    await client.aclose()


@pytest.mark.asyncio
async def test_stage4_global_cap_enforced_across_assessment_and_stt(tmp_path):
    """
    Acceptance 8:
    With assessment fanned out, the single provider semaphore must still bound the TOTAL number
    of in-flight provider calls (LLM and STT combined) by provider_concurrency_cap.
    """
    db = Database(tmp_path / "test_mixed_cap.db")
    db.init_schema()
    repo = Repository(db)
    inv_id = "inv-mixed-cap"
    q_ids = _setup_multi_question_interview(repo, inv_id, 6)

    cap = 8
    in_flight = 0
    max_in_flight = 0
    counter_lock = asyncio.Lock()

    async def tracked_llm(*args, **kwargs):
        nonlocal in_flight, max_in_flight
        async with counter_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.1)
        async with counter_lock:
            in_flight -= 1
        return _assessment_response("crit-0")

    async def tracked_stt(*args, **kwargs):
        nonlocal in_flight, max_in_flight
        async with counter_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.1)
        async with counter_lock:
            in_flight -= 1
        return STTTranscriptionResult(text="ok", model_id="m", latency_seconds=0.1, raw_response={})

    mock_llm = MagicMock()
    mock_llm.execute_request = AsyncMock(side_effect=tracked_llm)
    mock_stt = MagicMock()
    mock_stt.transcribe_audio = AsyncMock(side_effect=tracked_stt)

    worker = PipelineWorker(
        repository=repo, stt_adapter=mock_stt, llm_adapter=mock_llm, provider_concurrency_cap=cap
    )

    for i, q_id in enumerate(q_ids):
        repo.enqueue_job(f"job-mixed-eval-{i}", "EVALUATE_QUESTION", inv_id, {"question_id": q_id})
    for i in range(3):
        _save_dummy_chunk(repo, inv_id, "candidate", i, i * 1000, (i + 1) * 1000, tmp_path / "spool")
        repo.enqueue_job(
            f"job-mixed-turn-{i}",
            "TRANSCRIBE_TURN",
            inv_id,
            {
                "track_id": "candidate",
                "start_ms": i * 1000,
                "end_ms": (i + 1) * 1000,
                "first_sequence": i,
                "first_sample_offset": 0,
                "last_sequence": i,
                "last_sample_offset": 16000,
                "capture_epoch": 1,
            },
        )

    loop_task = asyncio.create_task(worker.run_loop(poll_interval_sec=0.02))
    try:
        for _ in range(120):
            counts = repo.get_interview_jobs_status(inv_id)["counts"]
            if counts.get("COMPLETED") == len(q_ids) + 3:
                break
            await asyncio.sleep(0.05)
        assert repo.get_interview_jobs_status(inv_id)["counts"].get("COMPLETED") == len(q_ids) + 3
        assert max_in_flight <= cap
        assert max_in_flight >= 1
    finally:
        worker.stop()
        try:
            await asyncio.wait_for(loop_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError):
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
