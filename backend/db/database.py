"""
SQLite Database Connection Manager with WAL Mode.
Provides connection pooling, WAL pragma setup, and transaction management.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = os.getenv("NEBULA_DB_PATH", str(PROJECT_ROOT / "data" / "nebula.db"))


class Database:
    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def init_schema(self, schema_file: str | Path | None = None) -> None:
        if schema_file is None:
            schema_file = Path(__file__).parent / "schema.sql"
        with open(schema_file, encoding="utf-8") as f:
            sql = f.read()

        with self.transaction() as conn:
            conn.executescript(sql)

        from backend.db.migrations import run_migrations
        run_migrations(self)

    def backup(self, target_path: str, trusted_backup_dir: str | Path | None = None) -> str:
        """
        Performs consistent online SQLite WAL backup to target_path.
        If trusted_backup_dir is provided, target_path is strictly constrained inside it.
        Выполняет согласованный онлайн-бэкап SQLite WAL.
        Если передан trusted_backup_dir, target_path строго ограничивается этим каталогом.
        """
        if trusted_backup_dir is not None:
            trusted_dir = Path(trusted_backup_dir).resolve()
            filename = Path(target_path).name
            dest_path = (trusted_dir / filename).resolve()
            if not dest_path.is_relative_to(trusted_dir):
                raise ValueError(f"Target path {target_path} escapes trusted backup directory {trusted_dir}")
        else:
            dest_path = Path(target_path).resolve()

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        source_conn = self.get_connection()
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            source_conn.close()
        return str(dest_path)

    def verify_integrity(self) -> bool:
        """Verifies database integrity using PRAGMA integrity_check."""
        with self.transaction() as conn:
            row = conn.execute("PRAGMA integrity_check;").fetchone()
            return row[0] == "ok" if row else False

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# Global default database instance
_global_db: Database | None = None


def get_db(db_path: str = DEFAULT_DB_PATH) -> Database:
    global _global_db
    if _global_db is None or _global_db.db_path != db_path:
        _global_db = Database(db_path)
    return _global_db
