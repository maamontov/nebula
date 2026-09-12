"""
Tests for Stage 1 (R2): Isolation and correct migration of transcript revisions.
"""
import sqlite3

import pytest

from backend.db.database import Database
from backend.db.migrations import run_migrations
from backend.db.repository import Repository


def test_interviews_have_independent_revisions(tmp_path):
    db_path = str(tmp_path / "test_iso.db")
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)

    # 1. Create two interviews
    repo.create_interview("int-1", "Interview 1", "Alice", "Developer")
    repo.create_interview("int-2", "Interview 2", "Bob", "Developer")

    # Add segments to int-1 and int-2 in trans-rev-1
    repo.add_transcript_segment("s1", "int-1", "shared", 0, 1000, "Hello Alice", revision_id="trans-rev-1", speaker_role="unknown")
    repo.add_transcript_segment("s2", "int-2", "shared", 0, 1000, "Hello Bob", revision_id="trans-rev-1", speaker_role="unknown")

    # 2. Role update on int-1 creates trans-rev-2 for int-1
    repo.update_segment_speaker_role("int-1", "s1", "interviewer")
    int1_revs = repo.get_transcript_revisions("int-1")
    int2_revs = repo.get_transcript_revisions("int-2")

    assert len(int1_revs) == 2
    assert [r["id"] for r in int1_revs] == ["trans-rev-1", "trans-rev-2"]
    # int-2 is unaffected by int-1's update and still only has trans-rev-1
    assert len(int2_revs) == 1
    assert [r["id"] for r in int2_revs] == ["trans-rev-1"]

    # 3. Role update on int-2 creates trans-rev-2 for int-2 independently
    repo.update_segment_speaker_role("int-2", "s2", "candidate")
    int2_revs = repo.get_transcript_revisions("int-2")
    assert len(int2_revs) == 2
    assert [r["id"] for r in int2_revs] == ["trans-rev-1", "trans-rev-2"]

    # 4. Split on int-1 creates trans-rev-3 for int-1
    repo.split_transcript_segment("int-1", "s1", split_time_ms=500, text_part1="Hello", text_part2="Alice")
    int1_revs = repo.get_transcript_revisions("int-1")
    assert len(int1_revs) == 3
    assert [r["id"] for r in int1_revs] == ["trans-rev-1", "trans-rev-2", "trans-rev-3"]

    # int-2 still has only 2 revisions
    int2_revs = repo.get_transcript_revisions("int-2")
    assert len(int2_revs) == 2

    # 5. Active revision checks
    assert repo.get_interview("int-1")["active_transcript_revision_id"] == "trans-rev-3"
    assert repo.get_interview("int-2")["active_transcript_revision_id"] == "trans-rev-2"

    # Cannot activate non-existent revision
    with pytest.raises(ValueError, match="does not exist"):
        repo.set_active_transcript_revision("int-2", "trans-rev-99")

    # 6. Deleting int-1 cascades only int-1
    repo.delete_interview("int-1")
    assert repo.get_interview("int-1") is None
    assert repo.get_transcript_revisions("int-1") == []

    # int-2 is fully intact
    int2_after = repo.get_transcript_revisions("int-2")
    assert len(int2_after) == 2
    assert [r["id"] for r in int2_after] == ["trans-rev-1", "trans-rev-2"]
    assert len(repo.get_transcript_segments("int-2", revision_id="trans-rev-2")) == 1


def test_migration_recovers_missing_revisions_from_legacy_db(tmp_path):
    """Simulate a legacy v11 database with global PK collision and verify migration 12 recovery."""
    db_path = str(tmp_path / "legacy_v11.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF;")

    # Create schema migrations table at version 11
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """
    )
    for v in range(1, 12):
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (v, f"v{v}", "2026-09-12T10:00:00Z"))

    # Create legacy tables
    conn.execute(
        """
        CREATE TABLE interviews (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            candidate_name TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            active_rubric_revision_id TEXT DEFAULT 'rub-rev-1',
            active_transcript_revision_id TEXT DEFAULT 'trans-rev-2',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE transcript_revisions (
            id TEXT PRIMARY KEY,
            interview_id TEXT NOT NULL,
            revision_number INTEGER NOT NULL DEFAULT 1,
            is_batch_final INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE (interview_id, revision_number)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE transcript_segments (
            id TEXT NOT NULL,
            interview_id TEXT NOT NULL,
            track_id TEXT NOT NULL,
            start_time_ms INTEGER NOT NULL,
            end_time_ms INTEGER NOT NULL,
            text TEXT NOT NULL,
            is_final INTEGER NOT NULL DEFAULT 1,
            revision_id TEXT NOT NULL DEFAULT 'trans-rev-1',
            speaker_role TEXT NOT NULL DEFAULT 'unknown',
            parent_segment_id TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (interview_id, revision_id, id)
        );
        """
    )

    # Insert two interviews
    conn.execute("INSERT INTO interviews VALUES ('int-1', 'T1', 'C1', 'R1', 'draft', 'rub-rev-1', 'trans-rev-2', '2026-09-12T10:00:00Z', '2026-09-12T10:00:00Z')")
    conn.execute("INSERT INTO interviews VALUES ('int-2', 'T2', 'C2', 'R2', 'draft', 'rub-rev-1', 'trans-rev-2', '2026-09-12T10:00:00Z', '2026-09-12T10:00:00Z')")

    # Due to global PK, int-1 got trans-rev-1 and trans-rev-2 into transcript_revisions
    conn.execute("INSERT INTO transcript_revisions VALUES ('trans-rev-1', 'int-1', 1, 0, '2026-09-12T10:00:00Z')")
    conn.execute("INSERT INTO transcript_revisions VALUES ('trans-rev-2', 'int-1', 2, 1, '2026-09-12T10:00:00Z')")

    # But int-2 has segments in trans-rev-1 and trans-rev-2, and active revision trans-rev-2, but ZERO rows in transcript_revisions!
    conn.execute("INSERT INTO transcript_segments VALUES ('s1', 'int-2', 'shared', 0, 1000, 'Hello from int2', 1, 'trans-rev-1', 'unknown', NULL, '2026-09-12T10:00:00Z')")
    conn.execute("INSERT INTO transcript_segments VALUES ('s1', 'int-2', 'shared', 0, 1000, 'Hello from int2 rev2', 1, 'trans-rev-2', 'candidate', NULL, '2026-09-12T10:00:00Z')")
    conn.commit()
    conn.close()

    # Run migration
    db = Database(db_path)
    new_ver = run_migrations(db)
    assert new_ver == 12

    # Verify integrity
    assert db.verify_integrity() is True

    repo = Repository(db)
    int1_revs = repo.get_transcript_revisions("int-1")
    int2_revs = repo.get_transcript_revisions("int-2")

    assert len(int1_revs) == 2
    assert [r["id"] for r in int1_revs] == ["trans-rev-1", "trans-rev-2"]

    # int-2 revisions must now be recovered!
    assert len(int2_revs) == 2
    assert [r["id"] for r in int2_revs] == ["trans-rev-1", "trans-rev-2"]

    # Re-running migrations is idempotent
    new_ver_2 = run_migrations(db)
    assert new_ver_2 == 12
    assert db.verify_integrity() is True
