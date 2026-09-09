from fastapi.testclient import TestClient
from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository
import pytest


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "api_test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)

    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_create_and_retrieve_interview(client):
    res = client.post(
        "/api/v1/interviews",
        json={
            "id": "inv-test-1",
            "title": "Senior Rust Engineer",
            "candidate_name": "Павел",
            "role": "Rust Core",
            "plan": {"questions": [{"id": "q1", "text": "What is Send + Sync?"}]},
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "inv-test-1"
    assert data["status"] == "draft"

    # Fetch interview detail
    get_res = client.get("/api/v1/interviews/inv-test-1")
    assert get_res.status_code == 200
    detail = get_res.json()
    assert detail["interview"]["candidate_name"] == "Павел"
    assert detail["plan"]["questions"][0]["id"] == "q1"


def test_transition_status_and_consent(client):
    client.post(
        "/api/v1/interviews",
        json={"id": "inv-test-2", "title": "Lead", "candidate_name": "Анна", "role": "Lead"},
    )

    # Transition to READY
    res1 = client.post(
        "/api/v1/interviews/inv-test-2/status",
        json={"current_status": "draft", "target_status": "ready"},
    )
    assert res1.status_code == 200
    assert res1.json()["status"] == "ready"

    # Transition to RECORDING with consent
    res2 = client.post(
        "/api/v1/interviews/inv-test-2/status",
        json={
            "current_status": "ready",
            "target_status": "recording",
            "consent_confirmed_at": "2026-09-10T00:00:00Z",
            "consent_version": "consent-v1",
        },
    )
    assert res2.status_code == 200
    assert res2.json()["status"] == "recording"


def test_add_segments_and_jobs(client):
    client.post(
        "/api/v1/interviews",
        json={"id": "inv-test-3", "title": "DevOps", "candidate_name": "Илья", "role": "SRE"},
    )

    # Add segment
    seg_res = client.post(
        "/api/v1/interviews/inv-test-3/segments",
        json={
            "id": "seg-1",
            "track_id": "candidate",
            "start_time_ms": 0,
            "end_time_ms": 5000,
            "text": "Terraform state locks in DynamoDB prevent concurrent applies.",
        },
    )
    assert seg_res.status_code == 200

    # Enqueue job
    job_res = client.post(
        "/api/v1/interviews/inv-test-3/jobs/enqueue",
        json={
            "id": "job-1",
            "type": "EVALUATE_QUESTION",
            "payload": {"question_id": "q-tf"},
        },
    )
    assert job_res.status_code == 200

    # Verify in detail
    detail = client.get("/api/v1/interviews/inv-test-3").json()
    assert len(detail["transcript_segments"]) == 1
    assert detail["transcript_segments"][0]["text"].startswith("Terraform")
