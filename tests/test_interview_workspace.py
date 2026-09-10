"""
Comprehensive Tests for Interview Workspace:
- Job Templates Management (CRUD, duplicate, archive, copy question)
- Interview Pagination and Advanced Filtering
- Draft Interview Updates and Template Plan Isolation
- Reopen Finalized Revision Cycle and Multi-revision Export History
- Interview Plan Readiness Validation
"""
import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository


@pytest.fixture
def workspace_client(tmp_path):
    db_file = tmp_path / "workspace_test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)

    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_job_templates_lifecycle(workspace_client):
    # 1. Default seeded template exists
    res = workspace_client.get("/api/v1/job-templates")
    assert res.status_code == 200
    templates = res.json()
    assert len(templates) >= 1
    assert any(t["id"] == "tpl-backend-senior" for t in templates)

    # 2. Create custom template
    create_payload = {
        "id": "tpl-frontend-lead",
        "title": "Lead Frontend Engineer",
        "role": "Frontend",
        "level": "Lead",
        "description": "Руководство фронтенд-командой, React, Web Vitals",
        "questions": [
            {
                "id": "q-fe-arch",
                "title": "Архитектура фронтенда",
                "prompt": "Как вы организуете стейт-менеджмент в крупных React-приложениях?",
                "weight": 1.2,
                "criteria": [
                    {
                        "id": "crit-fe-state",
                        "title": "Стейт-менеджмент",
                        "description": "Понимание Redux, Zustand, React Query",
                        "min_score": 1.0,
                        "max_score": 5.0,
                        "weight": 1.0,
                    }
                ],
            }
        ],
    }
    create_res = workspace_client.post("/api/v1/job-templates", json=create_payload)
    assert create_res.status_code == 200
    created = create_res.json()
    assert created["id"] == "tpl-frontend-lead"
    assert created["version"] == 1
    assert len(created["questions"]) == 1

    # 3. Duplicate template
    dup_res = workspace_client.post("/api/v1/job-templates/tpl-frontend-lead/duplicate", json={"title_suffix": " (Fork)"})
    assert dup_res.status_code == 200
    duplicated = dup_res.json()
    assert duplicated["id"] != "tpl-frontend-lead"
    assert duplicated["title"] == "Lead Frontend Engineer (Fork)"
    assert len(duplicated["questions"]) == 1

    # 4. Copy question to another template
    copy_res = workspace_client.post(
        f"/api/v1/job-templates/tpl-backend-senior/questions/q-backend-saga/copy-to/{duplicated['id']}"
    )
    assert copy_res.status_code == 200
    updated_fork = copy_res.json()
    assert len(updated_fork["questions"]) == 2

    # 5. Archive and list
    arch_res = workspace_client.post(f"/api/v1/job-templates/{duplicated['id']}/archive")
    assert arch_res.status_code == 200
    assert arch_res.json()["is_archived"] == 1

    active_list = workspace_client.get("/api/v1/job-templates").json()
    assert all(t["id"] != duplicated["id"] for t in active_list)

    all_list = workspace_client.get("/api/v1/job-templates?include_archived=true").json()
    assert any(t["id"] == duplicated["id"] for t in all_list)

    # 6. Unarchive
    unarch_res = workspace_client.post(f"/api/v1/job-templates/{duplicated['id']}/unarchive")
    assert unarch_res.status_code == 200
    assert unarch_res.json()["is_archived"] == 0


def test_job_template_validation(workspace_client):
    # Missing criteria
    invalid_payload = {
        "title": "Invalid Template",
        "role": "QA",
        "questions": [
            {
                "id": "q-nocrit",
                "title": "Вопрос без критериев",
                "prompt": "Prompt",
                "criteria": [],
            }
        ],
    }
    res = workspace_client.post("/api/v1/job-templates", json=invalid_payload)
    assert res.status_code == 422

    # Duplicate question IDs
    dup_q_payload = {
        "title": "Dup Q Template",
        "role": "QA",
        "questions": [
            {
                "id": "q-same",
                "title": "Q1",
                "criteria": [{"id": "c1", "title": "C1"}],
            },
            {
                "id": "q-same",
                "title": "Q2",
                "criteria": [{"id": "c2", "title": "C2"}],
            },
        ],
    }
    res2 = workspace_client.post("/api/v1/job-templates", json=dup_q_payload)
    assert res2.status_code == 422


def test_interview_template_isolation_and_draft_updates(workspace_client):
    # 1. Create interview from template
    inv_res = workspace_client.post(
        "/api/v1/interviews",
        json={
            "id": "inv-iso-1",
            "title": "Собеседование Backend",
            "candidate_name": "Дмитрий",
            "role": "Senior Backend Engineer",
            "template_id": "tpl-backend-senior",
        },
    )
    assert inv_res.status_code == 200
    detail = workspace_client.get("/api/v1/interviews/inv-iso-1").json()
    assert detail["interview"]["template_id"] == "tpl-backend-senior"
    assert len(detail["plan"]["questions"]) == 2

    # 2. Update job template: add a question
    workspace_client.put(
        "/api/v1/job-templates/tpl-backend-senior",
        json={
            "title": "Senior Backend Engineer v2",
            "questions": [
                {
                    "id": "q-brand-new",
                    "title": "Новый вопрос",
                    "prompt": "Новый вопрос",
                    "criteria": [{"id": "c-new", "title": "C", "min_score": 1, "max_score": 5, "weight": 1}],
                }
            ],
        },
    )

    # 3. Verify interview plan remained isolated!
    detail_after = workspace_client.get("/api/v1/interviews/inv-iso-1").json()
    assert len(detail_after["plan"]["questions"]) == 2
    assert detail_after["plan"]["questions"][0]["id"] == "q-backend-saga"

    # 4. Update interview draft metadata and custom plan
    put_res = workspace_client.put(
        "/api/v1/interviews/inv-iso-1",
        json={
            "candidate_name": "Дмитрий Иванович",
            "title": "Собеседование Дмитрий Иванович",
            "role": "Tech Lead",
            "plan": {
                "id": "plan-custom-dmitry",
                "title": "Индивидуальный план",
                "role": "Tech Lead",
                "questions": [
                    {
                        "id": "q-custom-1",
                        "title": "Кастомный вопрос",
                        "prompt": "Кастомный вопрос для Дмитрия",
                        "weight": 2.0,
                        "criteria": [{"id": "c-cust", "title": "Критерий", "min_score": 1, "max_score": 5, "weight": 1}],
                    }
                ],
            },
        },
    )
    assert put_res.status_code == 200
    assert put_res.json()["candidate_name"] == "Дмитрий Иванович"

    detail_updated = workspace_client.get("/api/v1/interviews/inv-iso-1").json()
    assert detail_updated["plan"]["title"] == "Индивидуальный план"
    assert len(detail_updated["plan"]["questions"]) == 1
    assert detail_updated["plan"]["questions"][0]["id"] == "q-custom-1"


def test_interview_pagination_and_filters(workspace_client):
    # Populate multiple interviews
    for i in range(15):
        role = "Frontend" if i % 2 == 0 else "Backend"
        candidate = f"Кандидат {i:02d}"
        status = "draft" if i < 10 else "ready"
        workspace_client.post(
            "/api/v1/interviews",
            json={
                "id": f"inv-pag-{i:02d}",
                "title": f"Interview {i}",
                "candidate_name": candidate,
                "role": role,
            },
        )
        if status == "ready":
            workspace_client.post(
                f"/api/v1/interviews/inv-pag-{i:02d}/status",
                json={"target_status": "ready"},
            )

    # 1. Default pagination
    page1 = workspace_client.get("/api/v1/interviews?limit=5&offset=0").json()
    assert len(page1["items"]) == 5
    assert page1["total"] >= 15
    assert page1["limit"] == 5
    assert page1["offset"] == 0

    page2 = workspace_client.get("/api/v1/interviews?limit=5&offset=5").json()
    assert len(page2["items"]) == 5
    # Ensure distinct items
    p1_ids = {x["id"] for x in page1["items"]}
    p2_ids = {x["id"] for x in page2["items"]}
    assert len(p1_ids.intersection(p2_ids)) == 0

    # 2. Search filter
    search_res = workspace_client.get("/api/v1/interviews?search=Кандидат 07").json()
    assert len(search_res["items"]) == 1
    assert search_res["items"][0]["candidate_name"] == "Кандидат 07"

    # 3. Role filter
    fe_res = workspace_client.get("/api/v1/interviews?role=Frontend&limit=20").json()
    assert len(fe_res["items"]) > 0
    assert all(x["role"] == "Frontend" for x in fe_res["items"])

    # 4. Status filter
    ready_res = workspace_client.get("/api/v1/interviews?status=ready").json()
    assert len(ready_res["items"]) == 5
    assert all(x["status"] == "ready" for x in ready_res["items"])


def test_reopen_and_report_revisions_export(workspace_client):
    # 1. Create and setup an interview with plan
    inv_id = "inv-reopen-flow"
    workspace_client.post(
        "/api/v1/interviews",
        json={
            "id": inv_id,
            "title": "Архитектор систем",
            "candidate_name": "Сергей",
            "role": "Solution Architect",
            "plan": {
                "id": "plan-arch",
                "title": "Архитектура",
                "role": "Solution Architect",
                "questions": [
                    {
                        "id": "q-arch-1",
                        "title": "High Availability",
                        "prompt": "Как обеспечить 99.999% SLA?",
                        "weight": 1.0,
                        "criteria": [
                            {"id": "c-sla", "title": "SLA & Redundancy", "min_score": 1.0, "max_score": 5.0, "weight": 1.0}
                        ],
                    }
                ],
            },
        },
    )

    # Move through states: draft -> ready -> recording -> review
    workspace_client.post(f"/api/v1/interviews/{inv_id}/status", json={"target_status": "ready"})
    workspace_client.post(
        f"/api/v1/interviews/{inv_id}/status",
        json={"target_status": "recording", "consent_confirmed_at": "2026-09-10T00:00:00Z", "consent_version": "1.0"},
    )
    # Add transcript segment
    workspace_client.post(
        f"/api/v1/interviews/{inv_id}/segments",
        json={
            "id": "seg-1",
            "track_id": "candidate",
            "start_time_ms": 1000,
            "end_time_ms": 5000,
            "text": "Мы используем multi-region активный кластер и автоматический failover.",
            "is_final": True,
            "speaker_role": "candidate",
        },
    )
    workspace_client.post(f"/api/v1/interviews/{inv_id}/status", json={"target_status": "processing"})
    workspace_client.post(f"/api/v1/interviews/{inv_id}/status", json={"target_status": "review"})

    # Review assessment
    workspace_client.post(
        f"/api/v1/interviews/{inv_id}/assessments/q-arch-1/review",
        json={
            "expected_transcript_revision": "trans-rev-1",
            "scores": [{"criterion_id": "c-sla", "score": 4.0}],
            "reviewer_notes": "Хороший ответ по failover",
            "is_manually_adjusted": True,
        },
    )

    # Confirm summary
    workspace_client.post(
        f"/api/v1/interviews/{inv_id}/summary/confirm",
        json={
            "reviewer_id": "lead-reviewer",
            "confirmed_markdown": "Кандидат подтвердил глубокие знания HA.",
            "confirmed_recommendation": "HIRE",
            "expected_transcript_revision": "trans-rev-1",
        },
    )

    # Finalize report revision 1
    fin_res1 = workspace_client.post(
        f"/api/v1/interviews/{inv_id}/report/finalize",
        json={
            "summary_markdown": "Кандидат подтвердил глубокие знания HA.",
            "hiring_recommendation": "HIRE",
            "confirmed_by": "lead-reviewer",
            "expected_transcript_revision": "trans-rev-1",
        },
    )
    assert fin_res1.status_code == 200
    rev1_data = fin_res1.json()
    assert rev1_data["revision_number"] == 1
    assert rev1_data["final_score_100"] == 75.0

    # Verify interview is finalized
    inv_final = workspace_client.get(f"/api/v1/interviews/{inv_id}").json()
    assert inv_final["interview"]["status"] == "finalized"

    # 2. Reopen revision
    reopen_res = workspace_client.post(
        f"/api/v1/interviews/{inv_id}/revisions/reopen",
        json={"reviewer_id": "head-of-eng", "reason": "Дополнительная проверка требований по безопасности"},
    )
    assert reopen_res.status_code == 200
    assert reopen_res.json()["status"] == "review"

    # List interviews shows it as reopened!
    list_res = workspace_client.get("/api/v1/interviews?status=reopened").json()
    assert any(item["id"] == inv_id for item in list_res["items"])

    # 3. Update scores in revision 2
    workspace_client.post(
        f"/api/v1/interviews/{inv_id}/assessments/q-arch-1/review",
        json={
            "expected_transcript_revision": "trans-rev-1",
            "scores": [{"criterion_id": "c-sla", "score": 5.0}],
            "reviewer_notes": "Оценка повышена до 5.0 после ревизии",
            "is_manually_adjusted": True,
        },
    )

    # Finalize report revision 2
    fin_res2 = workspace_client.post(
        f"/api/v1/interviews/{inv_id}/report/finalize",
        json={
            "summary_markdown": "Обновлённое заключение: высший балл.",
            "hiring_recommendation": "STRONG_HIRE",
            "confirmed_by": "head-of-eng",
            "expected_transcript_revision": "trans-rev-1",
        },
    )
    assert fin_res2.status_code == 200
    rev2_data = fin_res2.json()
    assert rev2_data["revision_number"] == 2
    assert rev2_data["final_score_100"] == 100.0

    # 4. Check revisions history endpoint
    revs_list = workspace_client.get(f"/api/v1/interviews/{inv_id}/revisions/reports").json()
    assert len(revs_list) == 2
    assert revs_list[0]["revision_number"] == 1
    assert revs_list[0]["final_score_100"] == 75.0
    assert revs_list[1]["revision_number"] == 2
    assert revs_list[1]["final_score_100"] == 100.0

    # 5. Check historical exports
    exp_rev1 = workspace_client.get(f"/api/v1/interviews/{inv_id}/export?revision_number=1").json()
    assert exp_rev1["revision_number"] == 1
    assert exp_rev1["final_score_100"] == 75.0
    assert exp_rev1["is_draft"] is False

    exp_rev2 = workspace_client.get(f"/api/v1/interviews/{inv_id}/export?revision_number=2").json()
    assert exp_rev2["revision_number"] == 2
    assert exp_rev2["final_score_100"] == 100.0
    assert exp_rev2["is_draft"] is False
