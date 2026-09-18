import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.domain import (
    InterviewStatus,
)


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "followup_api_test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)

    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client, repo
    app.dependency_overrides.clear()


def _create_recording_interview(
    client: TestClient,
    repo: Repository,
    interview_id: str = "inv-api-1",
    with_candidate_answer: bool = True,
) -> str:
    # 1. Create interview
    res = client.post(
        "/api/v1/interviews",
        json={
            "id": interview_id,
            "title": "Senior Python Engineer",
            "candidate_name": "Дмитрий",
            "role": "Python Backend",
            "plan": {
                "title": "Backend Interview",
                "questions": [
                    {
                        "id": "q1",
                        "title": "Asyncio & Architecture",
                        "prompt": "Расскажите про Event Loop в asyncio",
                        "criteria": [
                            {"id": "c1", "title": "Event Loop", "description": "Понимание цикла событий", "min_score": 1, "max_score": 5, "weight": 1.0}
                        ],
                    }
                ],
            },
        },
    )
    assert res.status_code == 200

    # 2. Transition to ready
    res_ready = client.post(
        f"/api/v1/interviews/{interview_id}/status",
        json={"current_status": "draft", "target_status": "ready"},
    )
    assert res_ready.status_code == 200

    # 3. Transition to recording with consent
    res_status = client.post(
        f"/api/v1/interviews/{interview_id}/status",
        json={
            "current_status": "ready",
            "target_status": "recording",
            "consent_confirmed_at": "2026-09-10T00:00:00Z",
            "consent_version": "consent-v1",
        },
    )
    assert res_status.status_code == 200

    # 4. Add candidate segment and associate with q1
    if with_candidate_answer:
        repo.add_transcript_segment(
            segment_id=f"seg-{interview_id}",
            interview_id=interview_id,
            track_id="candidate",
            start_time_ms=0,
            end_time_ms=5000,
            text="Я использовал asyncio Event Loop для запуска фоновых корутин.",
            is_final=True,
            speaker_role="candidate",
        )
        repo.reassociate_segment(
            interview_id=interview_id,
            segment_id=f"seg-{interview_id}",
            new_question_id="q1",
        )

    return interview_id


def test_get_followups_state(client):
    test_client, repo = client
    interview_id = _create_recording_interview(test_client, repo, "inv-state-1")

    res = test_client.get(f"/api/v1/interviews/{interview_id}/followups?question_id=q1&mode=probe")
    assert res.status_code == 200
    data = res.json()
    assert data["interview_id"] == interview_id
    assert data["question_id"] == "q1"
    assert data["mode"] == "probe"
    assert data["suggestions"] == []
    assert data["has_new_answer"] is False

    # 404 for non-existent interview
    res_404 = test_client.get("/api/v1/interviews/non-existent/followups?question_id=q1")
    assert res_404.status_code == 404


def test_generate_followups_probe_and_cached(client):
    test_client, repo = client
    interview_id = _create_recording_interview(test_client, repo, "inv-gen-1")

    # 1. Enqueue generation
    gen_res = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={
            "question_id": "q1",
            "mode": "probe",
            "trigger": "manual",
        },
    )
    assert gen_res.status_code == 202
    gen_data = gen_res.json()
    assert gen_data["status"] == "enqueued"
    assert gen_data["mode"] == "probe"
    req_id = gen_data["request_id"]
    job_id = gen_data["job_id"]
    assert req_id is not None
    assert job_id is not None

    # 2. Duplicate generation with identical context returns cached
    gen_dup = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={
            "question_id": "q1",
            "mode": "probe",
            "trigger": "manual",
        },
    )
    assert gen_dup.status_code == 200
    dup_data = gen_dup.json()
    assert dup_data["status"] == "cached"
    assert dup_data["request_id"] == req_id


def test_generate_followups_lifecycle_restrictions(client):
    test_client, repo = client
    interview_id = _create_recording_interview(test_client, repo, "inv-life-1")

    # Pause interview
    test_client.post(f"/api/v1/interviews/{interview_id}/pause")

    # In paused: auto trigger is rejected (409)
    auto_paused = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={"question_id": "q1", "mode": "probe", "trigger": "auto"},
    )
    assert auto_paused.status_code == 409

    # In paused: manual trigger is permitted (202)
    manual_paused = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={"question_id": "q1", "mode": "guide", "trigger": "manual"},
    )
    assert manual_paused.status_code == 202
    assert manual_paused.json()["status"] == "enqueued"

    # Transition to review
    repo.update_interview_status(interview_id, InterviewStatus.REVIEW)

    # In review: generating new followups is strictly forbidden (409 Conflict)
    review_res = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={"question_id": "q1", "mode": "probe", "trigger": "manual"},
    )
    assert review_res.status_code == 409


def test_patch_followup_suggestion_and_occ(client):
    test_client, repo = client
    interview_id = _create_recording_interview(test_client, repo, "inv-patch-1")

    # Generate request
    gen_res = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={"question_id": "q1", "mode": "probe", "trigger": "manual"},
    )
    assert gen_res.status_code == 202
    req_id = gen_res.json()["request_id"]

    # Claim job and save suggestion
    job = repo.claim_next_job(lock_duration_sec=30, include_types=["GENERATE_FOLLOWUPS"])
    repo.save_followup_suggestions(
        job_id=job["id"],
        request_id=req_id,
        owner_token=job["locked_by"],
        suggestions=[
            {
                "kind": "clarify",
                "question_text": "Как вы управляли жизненным циклом Task?",
                "purpose": "Проверка понимания Task cancellation.",
                "criterion_ids": ["c1"],
                "source_refs": [],
            }
        ],
        outcome="success",
    )

    state = test_client.get(f"/api/v1/interviews/{interview_id}/followups?question_id=q1").json()
    assert len(state["suggestions"]) == 1
    sug = state["suggestions"][0]
    sug_id = sug["id"]
    assert sug["status"] == "suggested"
    assert sug["decision_version"] == 1

    # 1. Successful patch: transition to asked
    patch_res = test_client.patch(
        f"/api/v1/interviews/{interview_id}/followups/{sug_id}",
        json={
            "status": "asked",
            "asked_text": "Как вы отменяли Task при выходе?",
            "expected_decision_version": 1,
        },
    )
    assert patch_res.status_code == 200
    patched_data = patch_res.json()
    assert patched_data["status"] == "asked"
    assert patched_data["decision_version"] == 2
    assert patched_data["asked_text"] == "Как вы отменяли Task при выходе?"

    # 2. OCC Conflict: trying to patch with stale version 1
    stale_patch = test_client.patch(
        f"/api/v1/interviews/{interview_id}/followups/{sug_id}",
        json={
            "status": "dismissed",
            "expected_decision_version": 1,
        },
    )
    assert stale_patch.status_code == 409


def test_retry_followup_request_api(client):
    test_client, repo = client
    interview_id = _create_recording_interview(test_client, repo, "inv-retry-api")

    gen_res = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={"question_id": "q1", "mode": "probe", "trigger": "manual"},
    )
    assert gen_res.status_code == 202
    req_id = gen_res.json()["request_id"]
    job_id = gen_res.json()["job_id"]

    # Mark failed
    repo.fail_job(job_id, "Provider 503 error", is_terminal=True)
    with repo.db.transaction() as conn:
        conn.execute("UPDATE followup_requests SET outcome = 'error' WHERE id = ?", (req_id,))

    # Retry via API
    retry_res = test_client.post(f"/api/v1/interviews/{interview_id}/followups/requests/{req_id}/retry")
    assert retry_res.status_code == 200
    assert retry_res.json()["status"] == "retried"
    assert retry_res.json()["request_id"] == req_id


def test_direct_enqueue_generate_followups_is_forbidden(client):
    test_client, repo = client
    interview_id = _create_recording_interview(test_client, repo, "inv-block-1")

    res = test_client.post(
        f"/api/v1/interviews/{interview_id}/jobs/enqueue",
        json={
            "id": "job-hack-1",
            "type": "GENERATE_FOLLOWUPS",
            "payload": {"question_id": "q1"},
        },
    )
    assert res.status_code == 400
    assert "GENERATE_FOLLOWUPS must be requested via dedicated" in res.json()["detail"]


def test_get_interview_and_export_contains_followups(client):
    test_client, repo = client
    interview_id = _create_recording_interview(test_client, repo, "inv-exp-1")

    # Generate and mark as asked
    gen_res = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={"question_id": "q1", "mode": "probe", "trigger": "manual"},
    )
    assert gen_res.status_code == 202
    req_id = gen_res.json()["request_id"]
    job = repo.claim_next_job(lock_duration_sec=30, include_types=["GENERATE_FOLLOWUPS"])
    repo.save_followup_suggestions(
        job_id=job["id"],
        request_id=req_id,
        owner_token=job["locked_by"],
        suggestions=[
            {
                "kind": "clarify",
                "question_text": "Вопрос про async with",
                "purpose": "Проверка контекстных менеджеров",
                "criterion_ids": ["c1"],
                "source_refs": [],
            }
        ],
        outcome="success",
    )
    state = test_client.get(f"/api/v1/interviews/{interview_id}/followups?question_id=q1").json()
    sug_id = state["suggestions"][0]["id"]
    test_client.patch(
        f"/api/v1/interviews/{interview_id}/followups/{sug_id}",
        json={"status": "asked", "asked_text": "Вопрос про async with", "expected_decision_version": 1},
    )

    # 1. GET /interviews/{id} contains asked_followups
    get_res = test_client.get(f"/api/v1/interviews/{interview_id}")
    assert get_res.status_code == 200
    detail = get_res.json()
    assert "asked_followups" in detail
    assert len(detail["asked_followups"]) == 1
    assert detail["asked_followups"][0]["id"] == sug_id

    # 2. GET /interviews/{id}/export contains followup_questions
    exp_res = test_client.get(f"/api/v1/interviews/{interview_id}/export")
    assert exp_res.status_code == 200
    export_data = exp_res.json()
    assert "followup_questions" in export_data
    assert len(export_data["followup_questions"]) == 1
    assert export_data["followup_questions"][0]["id"] == sug_id


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:1420", "http://127.0.0.1:1420", "tauri://localhost", "https://tauri.localhost"],
)
def test_followup_decision_patch_preflight_allowed_for_desktop_origins(client, origin):
    """
    Desktop sends PATCH /followups/{suggestion_id} when the interviewer marks a suggestion as asked.
    CORS must allow PATCH, otherwise the browser/WebView preflight is rejected and fetch fails
    with an opaque network error (e.g. "Load failed") instead of reaching the endpoint.
    Десктоп отправляет PATCH при нажатии «Задан»; без PATCH в CORS preflight запрос не доходит до API.
    """
    test_client, _ = client
    res = test_client.options(
        "/api/v1/interviews/inv-preflight/followups/sug-preflight",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert res.status_code == 200, res.text
    assert res.headers["access-control-allow-origin"] == origin
    allowed = {m.strip().upper() for m in res.headers["access-control-allow-methods"].split(",")}
    assert "PATCH" in allowed


def test_guide_available_without_candidate_answer(client):
    """
    Наводящий вопрос можно запросить до ответа кандидата: состояние не блокирует,
    генерация принимается, а уточняющий вопрос по-прежнему ждёт ответа.
    """
    test_client, repo = client
    interview_id = _create_recording_interview(
        test_client, repo, "inv-guide-noanswer-1", with_candidate_answer=False
    )

    # Probe без ответа кандидата остаётся заблокированным
    probe_state = test_client.get(
        f"/api/v1/interviews/{interview_id}/followups?question_id=q1&mode=probe"
    ).json()
    assert probe_state["can_generate"] is False
    assert probe_state["wait_reason"] in ("waiting_for_candidate", "needs_role_assignment", "needs_association")

    probe_gen = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={"question_id": "q1", "mode": "probe", "trigger": "manual"},
    )
    assert probe_gen.status_code == 200
    assert probe_gen.json()["status"] == "waiting"

    # Guide без ответа кандидата доступен и принимается в очередь
    guide_state = test_client.get(
        f"/api/v1/interviews/{interview_id}/followups?question_id=q1&mode=guide"
    ).json()
    assert guide_state["can_generate"] is True
    assert guide_state["wait_reason"] is None

    guide_gen = test_client.post(
        f"/api/v1/interviews/{interview_id}/followups/generate",
        json={"question_id": "q1", "mode": "guide", "trigger": "manual"},
    )
    assert guide_gen.status_code == 202
    guide_data = guide_gen.json()
    assert guide_data["status"] == "enqueued"
    assert guide_data["mode"] == "guide"
    assert guide_data["job_id"]

    # Очередь действительно содержит задание на генерацию подсказки
    with repo.db.transaction() as conn:
        job_row = conn.execute(
            "SELECT type, status FROM jobs WHERE id = ?", (guide_data["job_id"],)
        ).fetchone()
    assert job_row is not None
    assert job_row["type"] == "GENERATE_FOLLOWUPS"
    assert job_row["status"] in ("PENDING", "PROCESSING")
