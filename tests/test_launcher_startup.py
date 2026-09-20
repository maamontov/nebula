"""Regression: embedded API readiness must precede worker schema initialization."""
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from backend.db.migrations import TARGET_VERSION


def test_api_is_migrated_before_worker_starts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "fresh.db"
    env = dict(os.environ, NEBULA_DB_PATH=str(db_path),
               NEBULA_BACKUP_DIR=str(tmp_path / "backups"), NEBULA_PARENT_PIPE="0")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    processes = []
    with (tmp_path / "api.log").open("w") as api_log, (tmp_path / "worker.log").open("w") as worker_log:
        try:
            api = subprocess.Popen(
                [sys.executable, "-m", "backend.launcher", "api", "--port", str(port)],
                cwd=root, env=env, stdout=api_log, stderr=subprocess.STDOUT,
            )
            processes.append(api)
            deadline = time.monotonic() + 30
            while True:
                assert api.poll() is None, "API exited during startup"
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                        assert response.status == 200
                    break
                except (urllib.error.URLError, TimeoutError):
                    assert time.monotonic() < deadline, "API readiness timed out"
                    time.sleep(0.05)
            # Query SQLite directly before making any API request that lazily initializes it.
            with sqlite3.connect(db_path) as conn:
                assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == TARGET_VERSION
                assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            worker = subprocess.Popen(
                [sys.executable, "-m", "backend.launcher", "worker"],
                cwd=root, env=env, stdout=worker_log, stderr=subprocess.STDOUT,
            )
            processes.append(worker)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                assert worker.poll() is None, "Worker failed after API became ready"
                assert api.poll() is None
                time.sleep(0.05)
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
