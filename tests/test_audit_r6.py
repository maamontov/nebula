"""
Tests for R6: Capture mode persistence in Setup and draft updates.
"""
import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository


@pytest.fixture
def test_client(tmp_path):
    db_file = tmp_path / "test_r6.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)

    def _get_repo():
        return repo

    app.dependency_overrides[get_repository] = _get_repo
    client = TestClient(app)
    try:
        yield client, repo
    finally:
        app.dependency_overrides.clear()


def test_r6_create_and_update_capture_mode_persistence(test_client):
    client, repo = test_client
    inv_id = "inv-r6-draft-1"

    # 1. Create draft with single_source
    res = client.post(
        "/api/v1/interviews",
        json={
            "id": inv_id,
            "title": "Backend Eng - Alice",
            "candidate_name": "Alice",
            "role": "Backend Eng",
            "capture_mode": "single_source",
        },
    )
    assert res.status_code == 200
    inv = repo.get_interview(inv_id)
    assert inv["capture_mode"] == "single_source"
    assert inv["expected_tracks_json"] == '["shared"]'

    # 2. Update draft to dual_source
    upd_res = client.put(
        f"/api/v1/interviews/{inv_id}",
        json={
            "capture_mode": "dual_source",
            "candidate_name": "Alice Cooper",
        },
    )
    assert upd_res.status_code == 200
    inv = repo.get_interview(inv_id)
    assert inv["capture_mode"] == "dual_source"
    assert inv["candidate_name"] == "Alice Cooper"
    assert inv["expected_tracks_json"] == '["interviewer", "candidate"]'

    # 3. Update draft back to single_source
    upd_res2 = client.put(
        f"/api/v1/interviews/{inv_id}",
        json={
            "capture_mode": "single_source",
        },
    )
    assert upd_res2.status_code == 200
    inv = repo.get_interview(inv_id)
    assert inv["capture_mode"] == "single_source"
    assert inv["expected_tracks_json"] == '["shared"]'


def test_r6_invalid_capture_mode_rejected(test_client):
    client, _ = test_client
    inv_id = "inv-r6-draft-2"

    client.post(
        "/api/v1/interviews",
        json={
            "id": inv_id,
            "title": "Test",
            "candidate_name": "Bob",
            "role": "QA",
            "capture_mode": "dual_source",
        },
    )

    res = client.put(
        f"/api/v1/interviews/{inv_id}",
        json={"capture_mode": "quad_source"},
    )
    assert res.status_code == 400
    assert "Invalid capture_mode" in res.json()["detail"]


def test_r6_capture_mode_immutable_after_start(test_client):
    client, repo = test_client
    inv_id = "inv-r6-draft-3"

    client.post(
        "/api/v1/interviews",
        json={
            "id": inv_id,
            "title": "Test",
            "candidate_name": "Charlie",
            "role": "Dev",
            "capture_mode": "single_source",
        },
    )

    # Transition to ready then in_progress
    repo.update_interview_status(inv_id, "ready")
    repo.update_interview_status(inv_id, "in_progress")

    # Attempt to update draft in status in_progress
    res = client.put(
        f"/api/v1/interviews/{inv_id}",
        json={"capture_mode": "dual_source"},
    )
    assert res.status_code in (400, 409)
    inv = repo.get_interview(inv_id)
    assert inv["capture_mode"] == "single_source"
