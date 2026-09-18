"""
Reproducing and verification tests for R1: /stop manifest path traversal and security validation.
Тесты воспроизведения и приёмки R1: запись манифеста за пределами spool и валидация /stop.
"""
import os

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository, get_trusted_spool_dir
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.domain import InterviewStatus


@pytest.fixture
def r1_env(tmp_path):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)

    db = Database(str(tmp_path / "test.db"))
    db.init_schema()
    repo = Repository(db)

    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_trusted_spool_dir] = lambda: spool_dir

    with TestClient(app) as client:
        yield {
            "client": client,
            "repo": repo,
            "spool_dir": spool_dir,
            "outside_dir": outside_dir,
            "tmp_path": tmp_path,
        }
    app.dependency_overrides.clear()


def test_r1_track_id_traversal_reproduced(r1_env):
    """
    Shows the vulnerability: sending ../outside or absolute path in track_id
    must be rejected with 400 or 422, and no file created outside spool.
    """
    client = r1_env["client"]
    repo = r1_env["repo"]
    outside_dir = r1_env["outside_dir"]

    interview_id = "inv-r1-test"
    repo.create_interview(
        interview_id=interview_id,
        title="R1 Test",
        candidate_name="Alice",
        role="Engineer",
        capture_mode="dual_source",
    )
    repo.update_interview_status(interview_id, InterviewStatus.RECORDING)

    # Attempt path traversal via track_id pointing to outside_dir
    payload = {
        "manifests": [
            {
                "interview_id": interview_id,
                "track_id": str(outside_dir),
                "capture_epoch": 0,
                "total_chunks": 1,
                "total_duration_ms": 1000,
            }
        ]
    }

    res = client.post(f"/api/v1/interviews/{interview_id}/stop", json=payload)
    outside_manifest = outside_dir / "manifest.json"

    assert res.status_code in (400, 422), f"Expected 400/422 but got {res.status_code}: {res.text}"
    assert not outside_manifest.exists(), "Manifest was written outside trusted spool!"
    # Verify status did not transition to processing
    inv = repo.get_interview(interview_id)
    assert inv["status"] == "recording"


def test_r1_foreign_interview_id_rejected(r1_env):
    """
    Manifest with foreign interview_id must be rejected.
    """
    client = r1_env["client"]
    repo = r1_env["repo"]

    interview_id = "inv-r1-main"
    repo.create_interview(
        interview_id=interview_id,
        title="R1 Main",
        candidate_name="Alice",
        role="Engineer",
        capture_mode="dual_source",
    )
    repo.update_interview_status(interview_id, InterviewStatus.RECORDING)

    payload = {
        "manifests": [
            {
                "interview_id": "inv-r1-FOREIGN",
                "track_id": "interviewer",
                "capture_epoch": 0,
                "total_chunks": 1,
                "total_duration_ms": 1000,
            }
        ]
    }

    res = client.post(f"/api/v1/interviews/{interview_id}/stop", json=payload)
    assert res.status_code in (400, 422)
    inv = repo.get_interview(interview_id)
    assert inv["status"] == "recording"


def test_r1_unexpected_track_rejected(r1_env):
    """
    Manifest with unexpected track (e.g. shared track for dual_source interview) must be rejected.
    """
    client = r1_env["client"]
    repo = r1_env["repo"]

    interview_id = "inv-r1-dual"
    repo.create_interview(
        interview_id=interview_id,
        title="R1 Dual",
        candidate_name="Alice",
        role="Engineer",
        capture_mode="dual_source",
    )
    repo.update_interview_status(interview_id, InterviewStatus.RECORDING)

    payload = {
        "manifests": [
            {
                "interview_id": interview_id,
                "track_id": "shared",  # Unexpected for dual_source
                "capture_epoch": 0,
                "total_chunks": 1,
                "total_duration_ms": 1000,
            }
        ]
    }

    res = client.post(f"/api/v1/interviews/{interview_id}/stop", json=payload)
    assert res.status_code in (400, 422)
    inv = repo.get_interview(interview_id)
    assert inv["status"] == "recording"


def test_r1_symlink_in_spool_rejected(r1_env):
    """
    Symlink in spool directory pointing outside must be rejected.
    """
    client = r1_env["client"]
    repo = r1_env["repo"]
    spool_dir = r1_env["spool_dir"]
    outside_dir = r1_env["outside_dir"]

    interview_id = "inv-r1-sym"
    repo.create_interview(
        interview_id=interview_id,
        title="R1 Sym",
        candidate_name="Alice",
        role="Engineer",
        capture_mode="dual_source",
    )
    repo.update_interview_status(interview_id, InterviewStatus.RECORDING)

    # Create symlink inside spool pointing outside
    interview_spool = spool_dir / interview_id
    interview_spool.mkdir(parents=True, exist_ok=True)
    symlink_track = interview_spool / "interviewer"
    try:
        os.symlink(outside_dir, symlink_track)
    except OSError:
        pytest.skip("Symlinks not supported in this filesystem")

    payload = {
        "manifests": [
            {
                "interview_id": interview_id,
                "track_id": "interviewer",
                "capture_epoch": 0,
                "total_chunks": 1,
                "total_duration_ms": 1000,
            }
        ]
    }

    res = client.post(f"/api/v1/interviews/{interview_id}/stop", json=payload)
    assert res.status_code in (400, 422)
    assert not (outside_dir / "manifest.json").exists()


def test_r1_valid_stop_and_idempotent_retry(r1_env):
    """
    Valid manifests are written atomically inside trusted spool.
    Repeated stop is idempotent and returns 200 with status processing.
    """
    client = r1_env["client"]
    repo = r1_env["repo"]
    spool_dir = r1_env["spool_dir"]

    interview_id = "inv-r1-valid"
    repo.create_interview(
        interview_id=interview_id,
        title="R1 Valid",
        candidate_name="Alice",
        role="Engineer",
        capture_mode="dual_source",
    )
    repo.update_interview_status(interview_id, InterviewStatus.RECORDING)

    payload = {
        "manifests": [
            {
                "interview_id": interview_id,
                "track_id": "interviewer",
                "capture_epoch": 0,
                "total_chunks": 2,
                "total_duration_ms": 2000,
                "total_samples": 32000,
                "dropped_samples": 0,
                "is_sealed": True,
                "gaps": [],
            },
            {
                "interview_id": interview_id,
                "track_id": "candidate",
                "capture_epoch": 0,
                "total_chunks": 2,
                "total_duration_ms": 2000,
                "total_samples": 32000,
                "dropped_samples": 0,
                "is_sealed": True,
                "gaps": [],
            },
        ]
    }

    # 1. First stop
    res1 = client.post(f"/api/v1/interviews/{interview_id}/stop", json=payload)
    assert res1.status_code == 200
    assert res1.json()["status"] == "processing"

    # Verify manifests written on disk in trusted spool
    inv_m = spool_dir / interview_id / "interviewer" / "manifest.json"
    cand_m = spool_dir / interview_id / "candidate" / "manifest.json"
    assert inv_m.exists()
    assert cand_m.exists()

    # 2. Repeated stop (idempotent)
    res2 = client.post(f"/api/v1/interviews/{interview_id}/stop", json=payload)
    assert res2.status_code == 200
    assert res2.json()["status"] == "processing"
