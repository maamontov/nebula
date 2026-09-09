"""
SQLite Database Connection Manager with WAL Mode.
Provides connection pooling, WAL pragma setup, and transaction management.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB_PATH = os.getenv("NEBULA_DB_PATH", "data/nebula.db")


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
