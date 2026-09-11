import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.adapters.llm import OpenAICompatibleAdapter
from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository, RepositoryConflictError
from backend.workers.pipeline import PipelineWorker
from contracts.domain import (
    InterviewStatus,
)
from contracts.provider import ModelProfile, ProviderProfile


def _setup_test_interview(repo: Repository, interview_id: str = "inv-stage1-1") -> str:
    plan = {
        "title": "Backend Interview",
        "questions": [
            {
                "id": "q1",
                "title": "Asyncio Architecture",
                "prompt": "Расскажите про Event Loop в asyncio",
                "criteria": [
                    {
                        "id": "c1",
                        "title": "Event Loop",
                        "description": "Понимание цикла событий",
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
        title="Senior Python Engineer",
        candidate_name="Алексей",
        role="Python Backend",
        status=InterviewStatus.RECORDING,
    )
    repo.save_plan(f"plan-{interview_id}", interview_id, plan, version=1)

    repo.add_transcript_segment(
        segment_id=f"seg-{interview_id}-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=4000,
        text="Я использовал asyncio Event Loop для выполнения фоновых задач.",
        is_final=True,
        speaker_role="candidate",
    )
    repo.reassociate_segment(
        interview_id=interview_id,
        segment_id=f"seg-{interview_id}-1",
        new_question_id="q1",
    )
    return interview_id


def _get_job_row(repo: Repository, job_id: str):
    with repo.db.transaction() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


@pytest.mark.asyncio
async def test_followup_success_with_real_adapter_mock_transport(tmp_path, monkeypatch):
    """
    Stage 1 acceptance: Real LLM adapter with httpx.MockTransport returns valid JSON.
    Job must be COMPLETED, suggestions saved, metadata and usage recorded.
    """
    monkeypatch.setenv("TEST_KEY", "secret-test-token")
    db = Database(str(tmp_path / "stage1_test.db"))
    db.init_schema()
    repo = Repository(db)

    interview_id = _setup_test_interview(repo, "inv-s1-ok")

    response_payload = {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": 1720000000,
        "model": "google/gemini-2.5-flash-upstream",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "suggestions": [
                                {
                                    "kind": "clarify",
                                    "question_text": "Какие именно фоновые задачи вы запускали через asyncio?",
                                    "purpose": "Уточнить специфику использования корутин",
                                    "criterion_ids": ["c1"],
                                    "source_refs": [
                                        {
                                            "segment_id": f"seg-{interview_id}-1",
                                            "exact_quote": "Я использовал asyncio Event Loop",
                                        }
                                    ],
                                }
                            ]
                        }
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 45,
            "total_tokens": 165,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(handler)
    mock_client = httpx.AsyncClient(transport=transport)

    provider = ProviderProfile(
        id="plusvibe",
        name="Plusvibe",
        base_url="https://api.plusvibe.example/v1",
        api_key_env="TEST_KEY",
    )
    model = ModelProfile(
        id="google/gemini-3.8-flash",
        name="Gemini 3.8 Flash",
        provider_id="plusvibe",
        upstream_model_id="gemini-3.8-flash-upstream",
    )
    real_adapter = OpenAICompatibleAdapter(
        provider=provider,
        model=model,
        http_client=mock_client,
    )

    worker = PipelineWorker(repo, followup_llm_adapter=real_adapter)

    # Enqueue followup request
    req, is_new, wait_reason, cd = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        candidate_fingerprint="fp-1",
        context_hash="ch-1",
        context_json=json.dumps(
            {
                "question": {
                    "id": "q1",
                    "title": "Asyncio",
                    "prompt": "Расскажите про Event Loop",
                    "criteria": [{"id": "c1", "title": "Loop", "description": "Loop details"}],
                },
                "candidate_segments": [
                    {
                        "id": f"seg-{interview_id}-1",
                        "text": "Я использовал asyncio Event Loop для выполнения фоновых задач.",
                    }
                ],
            }
        ),
    )
    assert req is not None
    job_id = req["job_id"]

    # Process job
    did_work = await worker.process_one_job()
    assert did_work is True

    # Verify job is COMPLETED
    job = _get_job_row(repo, job_id)
    assert job is not None
    assert job["status"] == "COMPLETED"

    # Verify followup_requests outcome and metadata
    state = repo.get_followup_state(interview_id, "q1", mode="probe")
    latest_req = state["latest_request"]
    assert latest_req["outcome"] == "ready"
    assert latest_req["usage_tokens"] == 165
    assert len(state["suggestions"]) == 1
    assert state["suggestions"][0]["kind"] == "clarify"

    await mock_client.aclose()


@pytest.mark.asyncio
async def test_followup_no_suggestions_outcome(tmp_path, monkeypatch):
    """
    Stage 1 acceptance: Model returns empty suggestions array. Outcome must be no_suggestions, job COMPLETED.
    """
    monkeypatch.setenv("TEST_KEY", "secret-test-token")
    db = Database(str(tmp_path / "stage1_test_nosug.db"))
    db.init_schema()
    repo = Repository(db)

    interview_id = _setup_test_interview(repo, "inv-s1-nosug")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"suggestions": []})}}],
                "usage": {"total_tokens": 80},
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_adapter = OpenAICompatibleAdapter(
        provider=ProviderProfile(id="p1", name="P1", base_url="https://api.p1.example/v1", api_key_env="TEST_KEY"),
        model=ModelProfile(id="m1", name="M1", provider_id="p1", upstream_model_id="m1-up"),
        http_client=mock_client,
    )
    worker = PipelineWorker(repo, followup_llm_adapter=real_adapter)

    req, is_new, wait_reason, cd = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        candidate_fingerprint="fp-2",
        context_hash="ch-2",
        context_json=json.dumps(
            {
                "question": {
                    "id": "q1",
                    "title": "Asyncio",
                    "prompt": "Расскажите про Event Loop",
                    "criteria": [{"id": "c1", "title": "Loop", "description": "Loop details"}],
                },
                "candidate_segments": [
                    {
                        "id": f"seg-{interview_id}-1",
                        "text": "Я использовал asyncio Event Loop для выполнения фоновых задач.",
                    }
                ],
            }
        ),
    )
    assert req is not None

    did_work = await worker.process_one_job()
    assert did_work is True

    job = _get_job_row(repo, req["job_id"])
    assert job["status"] == "COMPLETED"

    state = repo.get_followup_state(interview_id, "q1", mode="probe")
    assert state["latest_request"]["outcome"] == "no_suggestions"
    assert len(state["suggestions"]) == 0

    await mock_client.aclose()


@pytest.mark.asyncio
async def test_followup_transient_error_durable_backoff(tmp_path, monkeypatch):
    """
    Stage 1 acceptance: Transient error (429/503) sets PENDING with future retry timestamp.
    Immediate claim_next_job must return None until retry timestamp passes.
    """
    monkeypatch.setenv("TEST_KEY", "secret-test-token")
    db = Database(str(tmp_path / "stage1_transient.db"))
    db.init_schema()
    repo = Repository(db)

    interview_id = _setup_test_interview(repo, "inv-s1-transient")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "15"}, json={"error": "Rate limit exceeded"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_adapter = OpenAICompatibleAdapter(
        provider=ProviderProfile(id="p1", name="P1", base_url="https://api.p1.example/v1", api_key_env="TEST_KEY"),
        model=ModelProfile(id="m1", name="M1", provider_id="p1", upstream_model_id="m1-up"),
        http_client=mock_client,
    )
    worker = PipelineWorker(repo, followup_llm_adapter=real_adapter)

    req, is_new, wait_reason, cd = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        candidate_fingerprint="fp-3",
        context_hash="ch-3",
        context_json=json.dumps(
            {
                "question": {
                    "id": "q1",
                    "title": "Asyncio",
                    "prompt": "Расскажите про Event Loop",
                    "criteria": [{"id": "c1", "title": "Loop", "description": "Loop details"}],
                },
                "candidate_segments": [
                    {
                        "id": f"seg-{interview_id}-1",
                        "text": "Я использовал asyncio Event Loop для выполнения фоновых задач.",
                    }
                ],
            }
        ),
    )
    assert req is not None
    job_id = req["job_id"]

    did_work = await worker.process_one_job()
    assert did_work is False

    job = _get_job_row(repo, job_id)
    assert job["status"] == "PENDING"
    assert job["locked_until"] is not None
    assert job["locked_by"] is None
    assert job["attempts"] == 1

    # Immediate claim is impossible due to future locked_until
    assert repo.claim_next_job() is None

    await mock_client.aclose()


@pytest.mark.asyncio
async def test_followup_auth_error_terminal(tmp_path, monkeypatch):
    """
    Stage 1 acceptance: Auth error (401) results in FAILED status immediately without further retries.
    """
    monkeypatch.setenv("TEST_KEY", "bad-key")
    db = Database(str(tmp_path / "stage1_auth.db"))
    db.init_schema()
    repo = Repository(db)

    interview_id = _setup_test_interview(repo, "inv-s1-auth")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid API key"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_adapter = OpenAICompatibleAdapter(
        provider=ProviderProfile(id="p1", name="P1", base_url="https://api.p1.example/v1", api_key_env="TEST_KEY"),
        model=ModelProfile(id="m1", name="M1", provider_id="p1", upstream_model_id="m1-up"),
        http_client=mock_client,
    )
    worker = PipelineWorker(repo, followup_llm_adapter=real_adapter)

    req, is_new, wait_reason, cd = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        candidate_fingerprint="fp-4",
        context_hash="ch-4",
        context_json=json.dumps(
            {
                "question": {
                    "id": "q1",
                    "title": "Asyncio",
                    "prompt": "Расскажите про Event Loop",
                    "criteria": [{"id": "c1", "title": "Loop", "description": "Loop details"}],
                },
                "candidate_segments": [
                    {
                        "id": f"seg-{interview_id}-1",
                        "text": "Я использовал asyncio Event Loop для выполнения фоновых задач.",
                    }
                ],
            }
        ),
    )
    assert req is not None
    job_id = req["job_id"]

    did_work = await worker.process_one_job()
    assert did_work is False

    job = _get_job_row(repo, job_id)
    assert job["status"] == "FAILED"
    assert job["attempts"] == 1
    assert repo.claim_next_job() is None

    # Request outcome is also recorded as failed with auth_error
    state = repo.get_followup_state(interview_id, "q1", mode="probe")
    assert state["latest_request"]["outcome"] == "failed"
    assert state["latest_request"]["error_code"] == "auth_error"

    await mock_client.aclose()


def test_fail_job_ownership_and_terminal_protection(tmp_path):
    """
    Stage 1 acceptance: Old owner token cannot fail a job. Terminal states cannot be overwritten.
    """
    db = Database(str(tmp_path / "stage1_ownership.db"))
    db.init_schema()
    repo = Repository(db)

    _setup_test_interview(repo, "inv-s1-own")

    # Enqueue a test job
    job_id = "job-stage1-own"
    repo.enqueue_job(job_id, "TRANSCRIBE_AUDIO", "inv-s1-own", {"audio": "test"})

    # Claim job by worker A
    claimed = repo.claim_next_job()
    assert claimed is not None
    owner_a = claimed["locked_by"]
    assert owner_a is not None

    # Worker B with stale token attempts fail_job -> must fail with RepositoryConflictError
    with pytest.raises(RepositoryConflictError):
        repo.fail_job(job_id, "Worker B error", owner_token="worker-B-stale")

    # Worker A completes the job
    repo.complete_job(job_id, owner_token=owner_a)
    job = _get_job_row(repo, job_id)
    assert job["status"] == "COMPLETED"

    # Stale callback of worker A attempts to fail COMPLETED job -> must raise RepositoryConflictError
    with pytest.raises(RepositoryConflictError):
        repo.fail_job(job_id, "Belated error", owner_token=owner_a)

    # Unauthenticated call cannot revert terminal COMPLETED job
    with pytest.raises(RepositoryConflictError):
        repo.fail_job(job_id, "Unauthenticated error")


def test_followup_api_status_normalized_error(tmp_path):
    """
    Stage 1 acceptance: When request/job fails, GET /followups returns status='error' with normalized error message.
    """
    db = Database(str(tmp_path / "stage1_api.db"))
    db.init_schema()
    repo = Repository(db)
    interview_id = _setup_test_interview(repo, "inv-s1-api")

    req, is_new, wait_reason, cd = repo.create_followup_request_and_job(
        interview_id=interview_id,
        question_id="q1",
        mode="probe",
        trigger="manual",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        candidate_fingerprint="fp-api",
        context_hash="ch-api",
        context_json="{}",
    )
    assert req is not None
    job_id = req["job_id"]

    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as client:
        # Mark failed with auth error
        repo.fail_job(job_id, "401 Unauthorized API key", is_terminal=True)
        with repo.db.transaction() as conn:
            conn.execute(
                "UPDATE followup_requests SET outcome = 'failed', error_code = 'auth_error' WHERE id = ?",
                (req["id"],),
            )

        res = client.get(f"/api/v1/interviews/{interview_id}/followups?question_id=q1&mode=probe")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "error"
        assert "авторизации" in data["error_message"].lower()

    app.dependency_overrides.clear()
