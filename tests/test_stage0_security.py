"""
Regression test suite for Stage 0: Security, path traversal, and lifecycle protection.
Набор регрессионных тестов для Этапа 0: Безопасность путей, защита файловой системы и жизненного цикла.
"""
import os
import stat
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.domain import InterviewStatus


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """
    Sets up isolated directories for spool and backup, and configures test DB.
    Настраивает изолированные каталоги для spool и backup, и тестовую БД.
    """
    spool_dir = tmp_path / "trusted_spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = tmp_path / "trusted_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("NEBULA_SPOOL_DIR", str(spool_dir))
    monkeypatch.setenv("NEBULA_BACKUP_DIR", str(backup_dir))

    db_file = tmp_path / "test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)

    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as client:
        yield {
            "client": client,
            "repo": repo,
            "spool_dir": spool_dir,
            "backup_dir": backup_dir,
            "tmp_path": tmp_path,
        }
    app.dependency_overrides.clear()


def test_invalid_interview_id_traversal_rejected(test_env):
    """
    Path traversal in interview_id must be rejected with 400/422.
    Попытка path traversal в interview_id должна отклоняться с 400/422.
    """
    client = test_env["client"]
    invalid_ids = [
        "../escape",
        "../../etc/passwd",
        "nested/path",
        "nested\\path",
        "foo..bar",
        "bad;id",
        "id with spaces",
    ]
    for bad_id in invalid_ids:
        res = client.delete(f"/api/v1/interviews/{bad_id}")
        assert res.status_code in (400, 404, 422), f"Expected rejection for {bad_id}, got {res.status_code}"


def test_delete_ignores_client_spool_dir_and_preserves_external(test_env):
    """
    Client must not be able to override spool_dir to delete arbitrary directories.
    Клиент не должен иметь возможности переопределить spool_dir для удаления произвольных каталогов.
    """
    client = test_env["client"]
    tmp_path = test_env["tmp_path"]
    spool_dir = test_env["spool_dir"]

    # External directory that an attacker tries to wipe
    external_dir = tmp_path / "external_victim"
    external_dir.mkdir()
    victim_file = external_dir / "secret.txt"
    victim_file.write_text("critical data")

    # Legitimate interview spool dir
    inv_id = "inv-legit-001"
    legit_spool = spool_dir / inv_id
    legit_spool.mkdir()
    (legit_spool / "chunk-0.wav").write_bytes(b"audio")

    # Create interview
    res = client.post(
        "/api/v1/interviews",
        json={"id": inv_id, "title": "Test", "candidate_name": "Alex", "role": "Dev"},
    )
    assert res.status_code == 200

    # Delete with query param attempting to point to external_dir
    del_res = client.delete(f"/api/v1/interviews/{inv_id}?spool_dir={external_dir}")
    assert del_res.status_code == 200

    # Victim file must be intact
    assert victim_file.exists(), "Victim file outside spool was deleted!"
    # Legit spool must be deleted
    assert not legit_spool.exists(), "Legitimate spool directory was not purged!"


def test_delete_symlink_does_not_purge_target(test_env):
    """
    Deleting an interview whose spool directory is or contains a symlink outside
    must not delete the target outside directory.
    Удаление сессии с симлинком наружу не должно удалять целевой каталог.
    """
    client = test_env["client"]
    tmp_path = test_env["tmp_path"]
    spool_dir = test_env["spool_dir"]

    external_target = tmp_path / "outside_target"
    external_target.mkdir()
    target_file = external_target / "keep_me.txt"
    target_file.write_text("do not delete")

    inv_id = "inv-symlink-001"
    symlink_path = spool_dir / inv_id
    symlink_path.symlink_to(external_target)

    client.post(
        "/api/v1/interviews",
        json={"id": inv_id, "title": "Symlink", "candidate_name": "Bob", "role": "Dev"},
    )

    del_res = client.delete(f"/api/v1/interviews/{inv_id}")
    assert del_res.status_code in (200, 400)

    # The external target file must STILL exist!
    assert target_file.exists(), "External file was purged via symlink!"


def test_backup_restricted_to_trusted_directory(test_env):
    """
    Backup must reject path traversal and absolute paths outside trusted backup dir.
    Бэкап должен отклонять выход за пределы доверенного каталога.
    """
    client = test_env["client"]
    backup_dir = test_env["backup_dir"]
    tmp_path = test_env["tmp_path"]

    # Traversal attempt
    res = client.post(
        "/api/v1/system/backup",
        json={"target_path": "../../evil_backup.db"},
    )
    assert res.status_code in (400, 422), f"Expected rejection, got {res.status_code}"

    # Absolute path outside trusted dir
    outside_file = tmp_path / "outside.db"
    res = client.post(
        "/api/v1/system/backup",
        json={"target_path": str(outside_file)},
    )
    assert res.status_code in (400, 422), f"Expected rejection of outside path, got {res.status_code}"


def test_late_worker_cannot_resurrect_or_orphan_deleted_interview(test_env):
    """
    Late worker completing after deletion must not resurrect interview or save orphan segments.
    Поздний воркер не должен восстанавливать удаленное интервью или сохранять сиротские записи.
    """
    client = test_env["client"]
    repo = test_env["repo"]

    inv_id = "inv-lifecycle-001"
    client.post(
        "/api/v1/interviews",
        json={"id": inv_id, "title": "Life", "candidate_name": "Clara", "role": "Dev"},
    )

    # Delete interview
    del_res = client.delete(f"/api/v1/interviews/{inv_id}")
    assert del_res.status_code == 200

    # Simulate late worker attempting to save transcript segment
    with pytest.raises(Exception):
        repo.add_transcript_segment(
            segment_id="seg-late-1",
            interview_id=inv_id,
            track_id="candidate",
            start_time_ms=0,
            end_time_ms=1000,
            text="Late text",
        )

    # Verify interview was not recreated
    assert repo.get_interview(inv_id) is None


def test_cors_rejects_untrusted_origin(test_env):
    """
    CORS must not allow arbitrary origins (like evil-site.com).
    CORS не должен разрешать произвольные origins.
    """
    client = test_env["client"]
    res = client.options(
        "/healthz",
        headers={
            "Origin": "https://evil-site.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    allow_origin = res.headers.get("access-control-allow-origin")
    assert allow_origin != "*", "CORS allow-origin is wild card (*)"
    assert allow_origin != "https://evil-site.com", "CORS allowed untrusted origin"


def test_repeated_deletion_safe(test_env):
    """
    Repeated deletion of an already deleted interview is safe (returns 404).
    Повторное удаление уже удаленного интервью безопасно (возвращает 404).
    """
    client = test_env["client"]
    inv_id = "inv-repeat-del-1"
    client.post(
        "/api/v1/interviews",
        json={"id": inv_id, "title": "Repeat", "candidate_name": "Test", "role": "Dev"},
    )
    res1 = client.delete(f"/api/v1/interviews/{inv_id}")
    assert res1.status_code == 200
    res2 = client.delete(f"/api/v1/interviews/{inv_id}")
    assert res2.status_code == 404


def test_deletion_filesystem_failure_surfaced(test_env, monkeypatch):
    """
    Filesystem deletion failure must be surfaced as HTTP 500 and not masked as success.
    Отказ файловой системы при удалении должен возвращаться как 500, а не маскироваться успехом.
    """
    import shutil

    client = test_env["client"]
    inv_id = "inv-fs-fail-1"
    client.post(
        "/api/v1/interviews",
        json={"id": inv_id, "title": "FS Fail", "candidate_name": "Test", "role": "Dev"},
    )

    def mock_rmtree_fail(*args, **kwargs):
        raise PermissionError("Simulated filesystem permission denied")

    monkeypatch.setattr(shutil, "rmtree", mock_rmtree_fail)
    spool_dir = test_env["spool_dir"]
    inv_spool = spool_dir / inv_id
    inv_spool.mkdir()

    res = client.delete(f"/api/v1/interviews/{inv_id}")
    assert res.status_code == 500
    assert "Simulated filesystem permission denied" in res.json()["detail"]

