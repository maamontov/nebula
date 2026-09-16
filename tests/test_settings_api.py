"""
Integration and functional tests for AI Settings API endpoints (Step 8).
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app
from backend.core.credential_store import CredentialStore
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.domain import InterviewStatus


@pytest.fixture
def test_setup(tmp_path, monkeypatch):
    db_file = tmp_path / "api_test.db"
    db = Database(str(db_file))
    db.init_schema()

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEBULA_DATA_DIR", str(data_dir))
    monkeypatch.delenv("NEBULA_STT_API_KEY", raising=False)
    monkeypatch.delenv("NEBULA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ROUTERAI_API_KEY", raising=False)

    from backend.api.app import get_db, get_repository
    repo = Repository(db)
    cred_store = CredentialStore(data_dir=data_dir)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_repository] = lambda: repo

    client = TestClient(app)

    yield {"client": client, "repo": repo, "cred_store": cred_store, "db": db}

    app.dependency_overrides.clear()


def test_get_ai_settings_default(test_setup):
    client = test_setup["client"]
    res = client.get("/api/v1/settings/ai")
    assert res.status_code == 200
    data = res.json()
    assert data["revision"] == 0
    assert data["source"] == "environment"
    assert "transcription" in data
    assert "text_analysis" in data
    assert "stt_credentials" in data
    assert "llm_credentials" in data
    # Ensure raw secret values are NEVER present
    assert "api_key" not in data["stt_credentials"]
    assert "api_key" not in data["llm_credentials"]


def test_put_ai_settings_success_and_get(test_setup):
    client = test_setup["client"]
    cred_store = test_setup["cred_store"]

    update_payload = {
        "expected_revision": 0,
        "transcription": {
            "preset": "custom",
            "provider_id": "custom-stt",
            "provider_name": "Custom STT",
            "endpoint_url": "http://127.0.0.1:8080/v1/audio/transcriptions",
            "model_id": "custom-whisper-v1",
            "auth_mode": "bearer",
            "language": "en",
            "timeout_seconds": 45.0,
            "max_concurrency": 3,
        },
        "text_analysis": {
            "preset": "custom",
            "provider_id": "custom-llm",
            "provider_name": "Custom LLM",
            "base_url": "http://127.0.0.1:8080/v1",
            "auth_mode": "bearer",
            "timeout_seconds": 50.0,
            "max_concurrency": 8,
            "primary_model": {
                "model_id": "custom-model-primary",
                "structured_output_mode": "json_object",
                "reasoning_policy": "disable_enable_thinking",
                "supports_temperature": True,
                "temperature": 0.2,
                "max_output_tokens": 2048,
                "context_window_tokens": 16384,
            },
            "fallback_models": [],
        },
        "stt_api_key": {
            "action": "replace",
            "value": "stt-secret-key-123",
        },
        "llm_api_key": {
            "action": "replace",
            "value": "llm-secret-key-456",
        },
    }

    put_res = client.put("/api/v1/settings/ai", json=update_payload)
    assert put_res.status_code == 200
    saved = put_res.json()
    assert saved["revision"] == 1
    assert saved["source"] == "database"
    assert saved["transcription"]["model_id"] == "custom-whisper-v1"
    assert saved["text_analysis"]["primary_model"]["model_id"] == "custom-model-primary"
    assert saved["stt_credentials"]["configured"] is True
    assert saved["stt_credentials"]["source"] == "runtime_file"
    assert saved["llm_credentials"]["configured"] is True
    assert saved["llm_credentials"]["source"] == "runtime_file"

    # Verify snapshot on disk contains keys
    snapshot = cred_store.read_snapshot(1)
    assert snapshot["NEBULA_STT_API_KEY"] == "stt-secret-key-123"
    assert snapshot["NEBULA_LLM_API_KEY"] == "llm-secret-key-456"

    # Subsequent GET returns revision 1
    get_res = client.get("/api/v1/settings/ai")
    assert get_res.status_code == 200
    assert get_res.json()["revision"] == 1


def test_put_ai_settings_occ_conflict(test_setup):
    client = test_setup["client"]

    # First update to rev 1
    payload = {
        "expected_revision": 0,
        "transcription": {
            "preset": "custom",
            "provider_id": "c1",
            "provider_name": "C1",
            "endpoint_url": "http://127.0.0.1:8080/v1/audio/transcriptions",
            "model_id": "m1",
            "auth_mode": "none",
            "language": "ru",
        },
        "text_analysis": {
            "preset": "custom",
            "provider_id": "c1",
            "provider_name": "C1",
            "base_url": "http://127.0.0.1:8080/v1",
            "auth_mode": "none",
            "primary_model": {"model_id": "m1"},
        },
        "stt_api_key": {"action": "preserve"},
        "llm_api_key": {"action": "preserve"},
    }
    res1 = client.put("/api/v1/settings/ai", json=payload)
    assert res1.status_code == 200

    # Repeat with expected_revision=0 should fail with 409
    res2 = client.put("/api/v1/settings/ai", json=payload)
    assert res2.status_code == 409
    assert "conflict: expected revision" in res2.json()["detail"].lower()


def test_put_ai_settings_blocker_recording_interview(test_setup):
    client = test_setup["client"]
    repo = test_setup["repo"]

    # Create active interview
    repo.create_interview("inv-active-1", "Active Job", "Cand", "Role", status=InterviewStatus.RECORDING)

    payload = {
        "expected_revision": 0,
        "transcription": {
            "preset": "custom",
            "provider_id": "c1",
            "provider_name": "C1",
            "endpoint_url": "http://127.0.0.1:8080/v1/audio/transcriptions",
            "model_id": "m1",
            "auth_mode": "none",
            "language": "ru",
        },
        "text_analysis": {
            "preset": "custom",
            "provider_id": "c1",
            "provider_name": "C1",
            "base_url": "http://127.0.0.1:8080/v1",
            "auth_mode": "none",
            "primary_model": {"model_id": "m1"},
        },
        "stt_api_key": {"action": "preserve"},
        "llm_api_key": {"action": "preserve"},
    }
    res = client.put("/api/v1/settings/ai", json=payload)
    assert res.status_code == 409
    assert "active work present" in res.json()["detail"].lower()


def test_put_ai_settings_blocker_pending_jobs(test_setup):
    client = test_setup["client"]
    repo = test_setup["repo"]

    repo.create_interview("inv-draft-1", "Draft Job", "Cand", "Role", status=InterviewStatus.DRAFT)
    repo.enqueue_job("job-pending-1", "EVALUATE_QUESTION", "inv-draft-1", {})

    payload = {
        "expected_revision": 0,
        "transcription": {
            "preset": "custom",
            "provider_id": "c1",
            "provider_name": "C1",
            "endpoint_url": "http://127.0.0.1:8080/v1/audio/transcriptions",
            "model_id": "m1",
            "auth_mode": "none",
            "language": "ru",
        },
        "text_analysis": {
            "preset": "custom",
            "provider_id": "c1",
            "provider_name": "C1",
            "base_url": "http://127.0.0.1:8080/v1",
            "auth_mode": "none",
            "primary_model": {"model_id": "m1"},
        },
        "stt_api_key": {"action": "preserve"},
        "llm_api_key": {"action": "preserve"},
    }
    res = client.put("/api/v1/settings/ai", json=payload)
    assert res.status_code == 409
    assert "pending" in res.json()["detail"].lower()


def test_put_ai_settings_env_managed_key_modification_forbidden(test_setup, monkeypatch):
    client = test_setup["client"]
    monkeypatch.setenv("NEBULA_STT_API_KEY", "env-protected-stt-key")

    payload = {
        "expected_revision": 0,
        "transcription": {
            "preset": "custom",
            "provider_id": "c1",
            "provider_name": "C1",
            "endpoint_url": "http://127.0.0.1:8080/v1/audio/transcriptions",
            "model_id": "m1",
            "auth_mode": "none",
            "language": "ru",
        },
        "text_analysis": {
            "preset": "custom",
            "provider_id": "c1",
            "provider_name": "C1",
            "base_url": "http://127.0.0.1:8080/v1",
            "auth_mode": "none",
            "primary_model": {"model_id": "m1"},
        },
        "stt_api_key": {"action": "replace", "value": "new-attempted-key"},
        "llm_api_key": {"action": "preserve"},
    }
    res = client.put("/api/v1/settings/ai", json=payload)
    assert res.status_code == 422
    assert "process environment" in res.json()["detail"].lower()


def test_post_ai_settings_test_stt_success(test_setup):
    client = test_setup["client"]

    req_payload = {
        "target": "stt",
        "transcription": {
            "preset": "custom",
            "provider_id": "custom-stt",
            "provider_name": "Custom STT",
            "endpoint_url": "http://127.0.0.1:8080/v1/audio/transcriptions",
            "model_id": "whisper-test-probe",
            "auth_mode": "none",
            "language": "ru",
        },
    }

    with patch("backend.api.app.OpenAICompatibleSTTAdapter.transcribe_audio", new_callable=AsyncMock) as mock_stt:
        mock_stt.return_value = AsyncMock(model_id="whisper-test-probe", latency_seconds=0.05)
        res = client.post("/api/v1/settings/ai/test", json=req_payload)
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["target"] == "stt"
        assert data["provider_id"] == "custom-stt"
        assert data["model_id"] == "whisper-test-probe"
        assert "probe successful" in data["message"].lower()


def test_post_ai_settings_test_llm_success(test_setup):
    client = test_setup["client"]

    req_payload = {
        "target": "llm",
        "text_analysis": {
            "preset": "custom",
            "provider_id": "custom-llm",
            "provider_name": "Custom LLM",
            "base_url": "http://127.0.0.1:8080/v1",
            "auth_mode": "bearer",
            "primary_model": {
                "model_id": "probe-model",
            },
        },
        "api_key": "test-key-probe",
    }

    with patch("backend.api.app.OpenAICompatibleAdapter.execute_request", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = {"data": {"status": "ok"}}
        res = client.post("/api/v1/settings/ai/test", json=req_payload)
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["target"] == "llm"
        assert data["model_id"] == "probe-model"
        assert "probe successful" in data["message"].lower()


def test_system_models_endpoint_reflects_db_update(test_setup):
    client = test_setup["client"]

    # Before update: environment defaults
    res0 = client.get("/api/v1/system/models")
    assert res0.status_code == 200
    m0 = res0.json()
    assert m0["settings_revision"] == 0
    assert m0["settings_source"] == "environment"

    # Perform update to rev 1
    update_payload = {
        "expected_revision": 0,
        "transcription": {
            "preset": "custom",
            "provider_id": "stt-new",
            "provider_name": "STT New",
            "endpoint_url": "http://127.0.0.1:8080/v1/audio/transcriptions",
            "model_id": "stt-model-v2",
            "auth_mode": "none",
            "language": "ru",
        },
        "text_analysis": {
            "preset": "custom",
            "provider_id": "llm-new",
            "provider_name": "LLM New",
            "base_url": "http://127.0.0.1:8080/v1",
            "auth_mode": "none",
            "primary_model": {
                "model_id": "llm-primary-v2",
            },
            "fallback_models": [
                {"model_id": "llm-fallback-v2"},
            ],
        },
        "stt_api_key": {"action": "preserve"},
        "llm_api_key": {"action": "preserve"},
    }
    client.put("/api/v1/settings/ai", json=update_payload)

    # After update: reflects rev 1 and new models
    res1 = client.get("/api/v1/system/models")
    assert res1.status_code == 200
    m1 = res1.json()
    assert m1["settings_revision"] == 1
    assert m1["stt"]["upstream_model_id"] == "stt-model-v2"
    assert m1["stt"]["model_profile_id"] == "stt-new-stt-model-v2"
    assert m1["llm"]["upstream_model_id"] == "llm-primary-v2"
    assert m1["llm"]["fallback_models"] == ["llm-fallback-v2"]
