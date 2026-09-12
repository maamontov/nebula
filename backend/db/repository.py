"""
Repository Layer for Nebula Domain Entities and Durable Jobs.
Provides safe database operations with JSON serialization and timestamps.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("nebula.repository")

from backend.core.scoring import calculate_interview_score
from backend.core.state_machine import InvalidStateTransitionError
from backend.core.turn_assembler import (
    AssembledTurn,
    AudioChunkRef,
    TurnAssembler,
    TurnAssemblyCursor,
)
from backend.db.database import Database
from contracts.domain import (
    HumanCriterionScore,
    HumanQuestionAssessment,
    InterviewStatus,
    PlannedQuestion,
    RubricCriterion,
    RubricRevision,
)


class RepositoryConflictError(Exception):
    """Raised when an operation conflicts with current state (e.g. modifying finalized interview)."""


class RepositoryNotFoundError(Exception):
    """Raised when a requested entity is not found in the repository."""



def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -------------------------------------------------------------
    # Interviews
    # -------------------------------------------------------------
    def create_interview(
        self,
        interview_id: str,
        title: str,
        candidate_name: str,
        role: str,
        status: InterviewStatus | str = InterviewStatus.DRAFT,
        consent_confirmed_at: str | None = None,
        consent_version: str | None = None,
        capture_mode: str = "dual_source",
        expected_tracks: list[str] | None = None,
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        if expected_tracks is None:
            expected_tracks = ["shared"] if capture_mode == "single_source" else ["interviewer", "candidate"]
        expected_tracks_json = json.dumps(expected_tracks)

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO interviews (
                    id, title, candidate_name, role, status,
                    consent_confirmed_at, consent_version, capture_mode, expected_tracks_json,
                    template_id, template_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interview_id,
                    title,
                    candidate_name,
                    role,
                    status.value if isinstance(status, InterviewStatus) else str(status),
                    consent_confirmed_at,
                    consent_version,
                    capture_mode,
                    expected_tracks_json,
                    template_id,
                    template_version,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO transcript_revisions (id, interview_id, revision_number, is_batch_final, created_at)
                VALUES ('trans-rev-1', ?, 1, 0, ?)
                ON CONFLICT(interview_id, id) DO NOTHING
                """,
                (interview_id, now),
            )
        return self.get_interview(interview_id)  # type: ignore[return-value]

    def update_interview_draft(
        self,
        interview_id: str,
        title: str | None = None,
        candidate_name: str | None = None,
        role: str | None = None,
        template_id: str | None = None,
        template_version: int | None = None,
        capture_mode: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv:
                raise KeyError(f"Interview {interview_id} not found")
            if inv["status"] not in ("draft", "ready"):
                raise RepositoryConflictError(
                    f"Cannot update interview in status '{inv['status']}', expected 'draft' or 'ready'"
                )

            new_title = title if title is not None else inv["title"]
            new_candidate = candidate_name if candidate_name is not None else inv["candidate_name"]
            new_role = role if role is not None else inv["role"]
            new_template_id = template_id if template_id is not None else inv["template_id"]
            new_template_version = template_version if template_version is not None else inv["template_version"]

            if capture_mode is not None:
                if capture_mode not in ("single_source", "dual_source"):
                    raise ValueError(f"Invalid capture_mode '{capture_mode}', expected 'single_source' or 'dual_source'")
                new_capture_mode = capture_mode
                new_expected_tracks = ["shared"] if capture_mode == "single_source" else ["interviewer", "candidate"]
                new_expected_tracks_json = json.dumps(new_expected_tracks)
            else:
                new_capture_mode = inv["capture_mode"]
                new_expected_tracks_json = inv["expected_tracks_json"]

            conn.execute(
                """
                UPDATE interviews
                SET title = ?, candidate_name = ?, role = ?, template_id = ?, template_version = ?,
                    capture_mode = ?, expected_tracks_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_title,
                    new_candidate,
                    new_role,
                    new_template_id,
                    new_template_version,
                    new_capture_mode,
                    new_expected_tracks_json,
                    now,
                    interview_id,
                ),
            )
        return self.get_interview(interview_id)  # type: ignore[return-value]

    def get_interview(self, interview_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM interviews WHERE id = ?", (interview_id,)
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def list_interviews(
        self,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        role: str | None = None,
        status: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                i.id,
                i.title,
                i.candidate_name,
                i.role,
                i.status,
                i.created_at,
                i.updated_at,
                i.capture_mode,
                i.template_id,
                i.template_version,
                lr.final_score_100,
                lr.coverage_percentage,
                lr.hiring_recommendation,
                lr.revision_number AS latest_report_revision,
                (SELECT COUNT(*) FROM report_revisions rr WHERE rr.interview_id = i.id) AS report_revisions_count
            FROM interviews i
            LEFT JOIN (
                SELECT r1.interview_id, r1.final_score_100, r1.coverage_percentage, r1.hiring_recommendation, r1.revision_number
                FROM report_revisions r1
                JOIN (
                    SELECT interview_id, MAX(revision_number) AS max_rev
                    FROM report_revisions
                    GROUP BY interview_id
                ) r2 ON r1.interview_id = r2.interview_id AND r1.revision_number = r2.max_rev
            ) lr ON lr.interview_id = i.id
            WHERE i.status != 'deleted'
        """
        params: list[Any] = []
        if search:
            query += " AND (i.candidate_name LIKE ? OR i.title LIKE ? OR i.role LIKE ?)"
            term = f"%{search.strip()}%"
            params.extend([term, term, term])
        if role:
            query += " AND i.role = ?"
            params.append(role)
        if status:
            if status == "reopened":
                query += " AND i.status = 'review' AND lr.revision_number IS NOT NULL"
            else:
                query += " AND i.status = ?"
                params.append(status.lower())
        if from_date:
            query += " AND i.created_at >= ?"
            params.append(from_date)
        if to_date:
            query += " AND i.created_at <= ?"
            params.append(to_date)

        query += " ORDER BY i.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self.db.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
            res = []
            for r in rows:
                item = dict(r)
                rev_count = item.pop("report_revisions_count", 0)
                is_reopened = bool(rev_count > 0 and item["status"] != "finalized")
                item["is_reopened"] = is_reopened
                if is_reopened:
                    item["last_finalized_score"] = item["final_score_100"]
                    item["last_finalized_recommendation"] = item["hiring_recommendation"]
                    item["last_finalized_revision"] = item.get("latest_report_revision")
                res.append(item)
            return res

    def count_interviews(
        self,
        search: str | None = None,
        role: str | None = None,
        status: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> int:
        query = "SELECT COUNT(*) FROM interviews i WHERE i.status != 'deleted'"
        params: list[Any] = []
        if search:
            query += " AND (i.candidate_name LIKE ? OR i.title LIKE ? OR i.role LIKE ?)"
            term = f"%{search.strip()}%"
            params.extend([term, term, term])
        if role:
            query += " AND i.role = ?"
            params.append(role)
        if status:
            if status == "reopened":
                query += """ AND i.status = 'review' AND EXISTS (
                    SELECT 1 FROM report_revisions rr WHERE rr.interview_id = i.id
                )"""
            else:
                query += " AND i.status = ?"
                params.append(status.lower())
        if from_date:
            query += " AND i.created_at >= ?"
            params.append(from_date)
        if to_date:
            query += " AND i.created_at <= ?"
            params.append(to_date)

        with self.db.transaction() as conn:
            row = conn.execute(query, params).fetchone()
            return row[0] if row else 0

    def update_interview_status(
        self,
        interview_id: str,
        new_status: InterviewStatus | str,
        consent_confirmed_at: str | None = None,
        consent_version: str | None = None,
    ) -> None:
        now = utc_now_iso()
        status_val = new_status.value if isinstance(new_status, InterviewStatus) else str(new_status)
        with self.db.transaction() as conn:
            if consent_confirmed_at:
                conn.execute(
                    """
                    UPDATE interviews
                    SET status = ?, consent_confirmed_at = ?, consent_version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status_val, consent_confirmed_at, consent_version, now, interview_id),
                )
            else:
                conn.execute(
                    "UPDATE interviews SET status = ?, updated_at = ? WHERE id = ?",
                    (status_val, now, interview_id),
                )

    def delete_interview(self, interview_id: str, spool_dir: str | Path | None = None) -> bool:
        """
        Permanently deletes an interview and cascades across all SQLite WAL tables.
        Cancels associated jobs and safely purges audio spool files preventing path traversal and symlink escapes.
        Удаляет сессию, отменяет связанные задачи и безопасно очищает spool-файлы,
        предотвращая path traversal и выход через симлинки.
        """
        if not interview_id or ".." in interview_id or "/" in interview_id or "\\" in interview_id:
            raise ValueError(f"Unsafe interview ID: {interview_id}")

        with self.db.transaction() as conn:
            row = conn.execute("SELECT id FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not row:
                return False

            # Cancel / delete jobs for this interview
            conn.execute("DELETE FROM jobs WHERE interview_id = ?", (interview_id,))
            # Delete audit events
            conn.execute("DELETE FROM audit_events WHERE interview_id = ?", (interview_id,))
            # Delete interview (triggers cascading delete for all child tables)
            conn.execute("DELETE FROM interviews WHERE id = ?", (interview_id,))

        # Physical audio spool deletion
        if spool_dir:
            trusted_spool = Path(spool_dir).resolve()
            raw_path = trusted_spool / interview_id
            if raw_path.is_symlink():
                raw_path.unlink()
            else:
                spool_path = raw_path.resolve()
                if not spool_path.is_relative_to(trusted_spool) or spool_path == trusted_spool:
                    raise ValueError(f"Path traversal detected: {spool_path} is outside {trusted_spool}")
                if spool_path.exists() and spool_path.is_dir():
                    shutil.rmtree(spool_path, ignore_errors=False)

        return True

    # -------------------------------------------------------------
    # Interview Plans
    # -------------------------------------------------------------
    def save_plan(
        self,
        plan_id: str,
        interview_id: str,
        payload: dict[str, Any],
        version: int = 1,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO interview_plans (id, interview_id, version, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (plan_id, interview_id, version, json.dumps(payload, ensure_ascii=False), now),
            )

    def get_latest_plan(self, interview_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM interview_plans
                WHERE interview_id = ?
                ORDER BY version DESC LIMIT 1
                """,
                (interview_id,),
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            data["payload"] = json.loads(data["payload_json"])
            return data

    # -------------------------------------------------------------
    # Transcript Segments
    # -------------------------------------------------------------
    def add_transcript_segment(
        self,
        segment_id: str,
        interview_id: str,
        track_id: str,
        start_time_ms: int,
        end_time_ms: int,
        text: str,
        is_final: bool = True,
        revision_id: str | None = None,
        speaker_role: str | None = None,
        parent_segment_id: str | None = None,
    ) -> None:
        if speaker_role is None:
            speaker_role = track_id if track_id in ("candidate", "interviewer") else "unknown"
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status, active_transcript_revision_id FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot add segment: interview {interview_id} does not exist or is deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            effective_revision_id = revision_id or inv["active_transcript_revision_id"] or "trans-rev-1"

            conn.execute(
                """
                INSERT INTO transcript_segments (
                    id, interview_id, track_id, start_time_ms, end_time_ms, text, is_final, revision_id,
                    speaker_role, parent_segment_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interview_id, revision_id, id) DO UPDATE SET
                    track_id = excluded.track_id,
                    start_time_ms = excluded.start_time_ms,
                    end_time_ms = excluded.end_time_ms,
                    text = excluded.text,
                    is_final = excluded.is_final,
                    speaker_role = CASE
                        WHEN transcript_segments.speaker_role IN ('candidate', 'interviewer') AND excluded.speaker_role = 'unknown'
                        THEN transcript_segments.speaker_role
                        ELSE excluded.speaker_role
                    END,
                    parent_segment_id = excluded.parent_segment_id
                """,
                (
                    segment_id,
                    interview_id,
                    track_id,
                    start_time_ms,
                    end_time_ms,
                    text,
                    1 if is_final else 0,
                    effective_revision_id,
                    speaker_role,
                    parent_segment_id,
                    now,
                ),
            )

    def save_turn_transcript_segment(
        self,
        segment_id: str,
        interview_id: str,
        track_id: str,
        start_time_ms: int,
        end_time_ms: int,
        text: str,
        target_revision_id: str,
        speaker_role: str | None = None,
        is_final: bool = True,
        owner_token: str | None = None,
        job_id: str | None = None,
        parent_segment_id: str | None = None,
    ) -> bool:
        """
        Atomically validates job lease/owner, interview lifecycle, target revision,
        and saves transcript segment without overwriting manual human edits or stale revisions.
        Returns True if segment was inserted/preserved.
        """
        if speaker_role is None:
            speaker_role = track_id if track_id in ("candidate", "interviewer") else "unknown"
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute(
                "SELECT id, status, active_transcript_revision_id FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot save segment: interview {interview_id} does not exist or is deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            if job_id is not None:
                j_row = conn.execute(
                    "SELECT id, status, locked_by, locked_until FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if not j_row:
                    raise RepositoryConflictError(f"Cannot save segment: job {job_id} not found")
                if j_row["status"] != "PROCESSING":
                    raise RepositoryConflictError(
                        f"Cannot save segment: job {job_id} is no longer PROCESSING (status: {j_row['status']})"
                    )
                if owner_token is not None and j_row["locked_by"] and j_row["locked_by"] != owner_token:
                    raise RepositoryConflictError(f"Cannot save segment: job {job_id} owner token mismatch")
                if j_row["locked_until"] and j_row["locked_until"] < now:
                    raise RepositoryConflictError(f"Cannot save segment: job {job_id} lease expired")

            active_rev = inv["active_transcript_revision_id"] or "trans-rev-1"

            # Always record the segment into the target revision for complete audit / historical record
            conn.execute(
                """
                INSERT INTO transcript_segments (
                    id, interview_id, track_id, start_time_ms, end_time_ms, text, is_final, revision_id,
                    speaker_role, parent_segment_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interview_id, revision_id, id) DO UPDATE SET
                    track_id = excluded.track_id,
                    start_time_ms = excluded.start_time_ms,
                    end_time_ms = excluded.end_time_ms,
                    text = excluded.text,
                    is_final = excluded.is_final,
                    speaker_role = CASE
                        WHEN transcript_segments.speaker_role IN ('candidate', 'interviewer') AND excluded.speaker_role = 'unknown'
                        THEN transcript_segments.speaker_role
                        ELSE excluded.speaker_role
                    END,
                    parent_segment_id = excluded.parent_segment_id
                """,
                (
                    segment_id,
                    interview_id,
                    track_id,
                    start_time_ms,
                    end_time_ms,
                    text,
                    1 if is_final else 0,
                    target_revision_id,
                    speaker_role,
                    parent_segment_id,
                    now,
                ),
            )

            # If the target revision is still active, we're done
            if target_revision_id == active_rev:
                return True

            # If target revision is stale (user created a new revision during live capture):
            # Check if active revision already has segments covering this time window on this track.
            # We must NOT overwrite manual edits or retranscriptions in active_rev!
            overlapping = conn.execute(
                """
                SELECT id, speaker_role, text FROM transcript_segments
                WHERE interview_id = ? AND revision_id = ? AND track_id = ?
                  AND ((start_time_ms <= ? AND end_time_ms > ?) OR (start_time_ms < ? AND end_time_ms >= ?))
                """,
                (interview_id, active_rev, track_id, start_time_ms, start_time_ms, end_time_ms, end_time_ms),
            ).fetchall()

            if not overlapping:
                # Safe to forward live segment into active revision without losing new speech
                conn.execute(
                    """
                    INSERT OR IGNORE INTO transcript_segments (
                        id, interview_id, track_id, start_time_ms, end_time_ms, text, is_final, revision_id,
                        speaker_role, parent_segment_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment_id,
                        interview_id,
                        track_id,
                        start_time_ms,
                        end_time_ms,
                        text,
                        1 if is_final else 0,
                        active_rev,
                        speaker_role,
                        parent_segment_id,
                        now,
                    ),
                )
            return True

    def _allocate_next_transcript_revision_in_tx(
        self,
        conn: sqlite3.Connection,
        interview_id: str,
        base_revision_id: str | None = None,
        now_iso: str | None = None,
    ) -> tuple[str, int]:
        """
        Allocates and records the next unique transcript revision for an interview.
        Ensures base_revision_id is recorded if provided, and generates next revision
        without colliding on PRIMARY KEY (interview_id, id) or UNIQUE (interview_id, revision_number).
        """
        now = now_iso or utc_now_iso()
        rev_rows = conn.execute(
            "SELECT id, revision_number FROM transcript_revisions WHERE interview_id = ?",
            (interview_id,),
        ).fetchall()
        existing_ids = {r["id"] for r in rev_rows}
        existing_nums = {r["revision_number"] for r in rev_rows}

        if base_revision_id and base_revision_id not in existing_ids:
            base_num = 1
            if base_revision_id.startswith("trans-rev-"):
                with contextlib.suppress(ValueError):
                    base_num = int(base_revision_id.split("-")[-1])
            if base_num in existing_nums:
                base_num = (max(existing_nums) if existing_nums else 0) + 1
            conn.execute(
                """
                INSERT INTO transcript_revisions (id, interview_id, revision_number, is_batch_final, created_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(interview_id, id) DO NOTHING
                """,
                (base_revision_id, interview_id, base_num, now),
            )
            existing_ids.add(base_revision_id)
            existing_nums.add(base_num)

        max_num = max(existing_nums) if existing_nums else 0
        if base_revision_id and base_revision_id.startswith("trans-rev-"):
            with contextlib.suppress(ValueError):
                max_num = max(max_num, int(base_revision_id.split("-")[-1]))

        next_num = max_num + 1
        while next_num in existing_nums or f"trans-rev-{next_num}" in existing_ids:
            next_num += 1
        new_rev_id = f"trans-rev-{next_num}"

        conn.execute(
            """
            INSERT INTO transcript_revisions (id, interview_id, revision_number, is_batch_final, created_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(interview_id, id) DO UPDATE SET
                revision_number = excluded.revision_number,
                is_batch_final = excluded.is_batch_final
            """,
            (new_rev_id, interview_id, next_num, now),
        )
        return new_rev_id, next_num

    def update_segment_speaker_role(
        self,
        interview_id: str,
        segment_id: str,
        speaker_role: str,
        expected_revision_id: str | None = None,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Updates speaker_role for a transcript segment by creating a new revision and preserving provenance."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status, active_transcript_revision_id FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Interview {interview_id} not found or deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            active_rev = inv["active_transcript_revision_id"] or "trans-rev-1"
            if expected_revision_id and expected_revision_id != active_rev:
                raise RepositoryConflictError(
                    f"Revision conflict: active transcript revision is '{active_rev}', but expected was '{expected_revision_id}'"
                )

            curr_rev = revision_id or active_rev
            row = conn.execute(
                "SELECT * FROM transcript_segments WHERE interview_id = ? AND revision_id = ? AND id = ?",
                (interview_id, curr_rev, segment_id),
            ).fetchone()
            if not row:
                raise ValueError(f"Segment {segment_id} not found in active revision {curr_rev}")

            new_rev_id, next_num = self._allocate_next_transcript_revision_in_tx(
                conn, interview_id, base_revision_id=curr_rev, now_iso=now
            )


            curr_segs = conn.execute(
                "SELECT * FROM transcript_segments WHERE interview_id = ? AND revision_id = ? ORDER BY start_time_ms ASC",
                (interview_id, curr_rev),
            ).fetchall()
            for s in curr_segs:
                role = speaker_role if s["id"] == segment_id else s["speaker_role"]
                conn.execute(
                    """
                    INSERT INTO transcript_segments (
                        id, interview_id, track_id, start_time_ms, end_time_ms, text,
                        is_final, revision_id, speaker_role, parent_segment_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        s["id"],
                        interview_id,
                        s["track_id"],
                        s["start_time_ms"],
                        s["end_time_ms"],
                        s["text"],
                        s["is_final"],
                        new_rev_id,
                        role,
                        s["parent_segment_id"],
                        s["created_at"],
                    ),
                )

            curr_assocs = conn.execute(
                "SELECT * FROM question_associations WHERE interview_id = ? AND revision_id = ?",
                (interview_id, curr_rev),
            ).fetchall()
            affected_questions = set()
            for a in curr_assocs:
                new_assoc_id = f"assoc-{uuid.uuid4().hex[:8]}"
                conn.execute(
                    """
                    INSERT INTO question_associations (
                        id, interview_id, revision_id, question_id, segment_id,
                        confidence, is_ambiguous, is_manually_adjusted, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_assoc_id,
                        interview_id,
                        new_rev_id,
                        a["question_id"],
                        a["segment_id"],
                        a["confidence"],
                        a["is_ambiguous"],
                        a["is_manually_adjusted"],
                        a["notes"],
                        a["created_at"],
                    ),
                )
                if a["segment_id"] == segment_id:
                    affected_questions.add(a["question_id"])

            all_proposals = conn.execute(
                "SELECT id, question_id, scores_json FROM assessment_proposals WHERE interview_id = ?",
                (interview_id,),
            ).fetchall()
            for p in all_proposals:
                if p["scores_json"] and segment_id in p["scores_json"]:
                    affected_questions.add(p["question_id"])

            conn.execute(
                "UPDATE interviews SET active_transcript_revision_id = ?, updated_at = ? WHERE id = ?",
                (new_rev_id, now, interview_id),
            )

            for q_id in affected_questions:
                reason = f"Стенограмма обновлена до {new_rev_id}. Изменена роль спикера в сегменте {segment_id}."
                conn.execute(
                    "UPDATE assessment_proposals SET is_stale = 1, stale_reason = ? WHERE interview_id = ? AND question_id = ?",
                    (reason, interview_id, q_id),
                )
                conn.execute(
                    "UPDATE human_assessments SET is_stale = 1, stale_reason = ? WHERE interview_id = ? AND question_id = ?",
                    (reason, interview_id, q_id),
                )
            conn.execute("UPDATE summary_proposals SET is_confirmed = 0 WHERE interview_id = ?", (interview_id,))

            updated_row = conn.execute(
                "SELECT * FROM transcript_segments WHERE interview_id = ? AND revision_id = ? AND id = ?",
                (interview_id, new_rev_id, segment_id),
            ).fetchone()
            return dict(updated_row)

    def split_transcript_segment(
        self,
        interview_id: str,
        segment_id: str,
        split_time_ms: int,
        text_part1: str,
        text_part2: str,
        role_part1: str = "interviewer",
        role_part2: str = "candidate",
        expected_revision_id: str | None = None,
        revision_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Splits a single speech segment into two sequential segments in a new revision."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status, active_transcript_revision_id FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Interview {interview_id} not found or deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            active_rev = inv["active_transcript_revision_id"] or "trans-rev-1"
            if expected_revision_id and expected_revision_id != active_rev:
                raise RepositoryConflictError(
                    f"Revision conflict: active transcript revision is '{active_rev}', but expected was '{expected_revision_id}'"
                )

            curr_rev = revision_id or active_rev
            orig = conn.execute(
                "SELECT * FROM transcript_segments WHERE interview_id = ? AND revision_id = ? AND id = ?",
                (interview_id, curr_rev, segment_id),
            ).fetchone()
            if not orig:
                raise ValueError(f"Original segment {segment_id} not found in active revision {curr_rev}")

            orig_dict = dict(orig)
            start_ms = orig_dict["start_time_ms"]
            end_ms = orig_dict["end_time_ms"]
            track_id = orig_dict["track_id"]

            if not (start_ms < split_time_ms < end_ms):
                raise ValueError(f"Split time {split_time_ms} must be strictly between {start_ms} and {end_ms}")

            new_rev_id, next_num = self._allocate_next_transcript_revision_in_tx(
                conn, interview_id, base_revision_id=curr_rev, now_iso=now
            )


            part2_id = f"seg-{uuid.uuid4().hex[:8]}"

            curr_segs = conn.execute(
                "SELECT * FROM transcript_segments WHERE interview_id = ? AND revision_id = ? ORDER BY start_time_ms ASC",
                (interview_id, curr_rev),
            ).fetchall()
            for s in curr_segs:
                if s["id"] == segment_id:
                    conn.execute(
                        """
                        INSERT INTO transcript_segments (
                            id, interview_id, track_id, start_time_ms, end_time_ms, text,
                            is_final, revision_id, speaker_role, parent_segment_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            segment_id,
                            interview_id,
                            track_id,
                            s["start_time_ms"],
                            split_time_ms,
                            text_part1.strip(),
                            new_rev_id,
                            role_part1,
                            s["parent_segment_id"],
                            s["created_at"],
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO transcript_segments (
                            id, interview_id, track_id, start_time_ms, end_time_ms, text,
                            is_final, revision_id, speaker_role, parent_segment_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                        """,
                        (
                            part2_id,
                            interview_id,
                            track_id,
                            split_time_ms,
                            end_ms,
                            text_part2.strip(),
                            new_rev_id,
                            role_part2,
                            segment_id,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO transcript_segments (
                            id, interview_id, track_id, start_time_ms, end_time_ms, text,
                            is_final, revision_id, speaker_role, parent_segment_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            s["id"],
                            interview_id,
                            s["track_id"],
                            s["start_time_ms"],
                            s["end_time_ms"],
                            s["text"],
                            s["is_final"],
                            new_rev_id,
                            s["speaker_role"],
                            s["parent_segment_id"],
                            s["created_at"],
                        ),
                    )

            curr_assocs = conn.execute(
                "SELECT * FROM question_associations WHERE interview_id = ? AND revision_id = ?",
                (interview_id, curr_rev),
            ).fetchall()
            affected_questions = set()
            for a in curr_assocs:
                new_assoc_id = f"assoc-{uuid.uuid4().hex[:8]}"
                conn.execute(
                    """
                    INSERT INTO question_associations (
                        id, interview_id, revision_id, question_id, segment_id,
                        confidence, is_ambiguous, is_manually_adjusted, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_assoc_id,
                        interview_id,
                        new_rev_id,
                        a["question_id"],
                        a["segment_id"],
                        a["confidence"],
                        a["is_ambiguous"],
                        a["is_manually_adjusted"],
                        a["notes"],
                        a["created_at"],
                    ),
                )
                if a["segment_id"] == segment_id:
                    affected_questions.add(a["question_id"])
                    assoc2_id = f"assoc-{uuid.uuid4().hex[:8]}"
                    conn.execute(
                        """
                        INSERT INTO question_associations (
                            id, interview_id, revision_id, question_id, segment_id,
                            confidence, is_ambiguous, is_manually_adjusted, notes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            assoc2_id,
                            interview_id,
                            new_rev_id,
                            a["question_id"],
                            part2_id,
                            a["confidence"],
                            a["is_ambiguous"],
                            a["is_manually_adjusted"],
                            a["notes"],
                            now,
                        ),
                    )

            all_proposals = conn.execute(
                "SELECT id, question_id, scores_json FROM assessment_proposals WHERE interview_id = ?",
                (interview_id,),
            ).fetchall()
            for p in all_proposals:
                if p["scores_json"] and segment_id in p["scores_json"]:
                    affected_questions.add(p["question_id"])

            conn.execute(
                "UPDATE interviews SET active_transcript_revision_id = ?, updated_at = ? WHERE id = ?",
                (new_rev_id, now, interview_id),
            )

            for q_id in affected_questions:
                reason = f"Стенограмма обновлена до {new_rev_id}. Сегмент {segment_id} был разделён."
                conn.execute(
                    "UPDATE assessment_proposals SET is_stale = 1, stale_reason = ? WHERE interview_id = ? AND question_id = ?",
                    (reason, interview_id, q_id),
                )
                conn.execute(
                    "UPDATE human_assessments SET is_stale = 1, stale_reason = ? WHERE interview_id = ? AND question_id = ?",
                    (reason, interview_id, q_id),
                )
            conn.execute("UPDATE summary_proposals SET is_confirmed = 0 WHERE interview_id = ?", (interview_id,))

            row1 = conn.execute(
                "SELECT * FROM transcript_segments WHERE interview_id = ? AND revision_id = ? AND id = ?",
                (interview_id, new_rev_id, segment_id),
            ).fetchone()
            row2 = conn.execute(
                "SELECT * FROM transcript_segments WHERE interview_id = ? AND revision_id = ? AND id = ?",
                (interview_id, new_rev_id, part2_id),
            ).fetchone()

            return dict(row1), dict(row2)


    def get_transcript_segments(self, interview_id: str, revision_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if revision_id is not None:
                rev = revision_id
            else:
                inv = conn.execute("SELECT active_transcript_revision_id FROM interviews WHERE id = ?", (interview_id,)).fetchone()
                rev = inv["active_transcript_revision_id"] if inv and inv["active_transcript_revision_id"] else "trans-rev-1"

            rows = conn.execute(
                """
                SELECT * FROM transcript_segments
                WHERE interview_id = ? AND revision_id = ?
                ORDER BY start_time_ms ASC
                """,
                (interview_id, rev),
            ).fetchall()
            return [dict(r) for r in rows]


    def create_transcript_revision(
        self,
        revision_id: str,
        interview_id: str,
        revision_number: int = 1,
        is_batch_final: bool = False,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            existing_for_num = conn.execute(
                "SELECT id FROM transcript_revisions WHERE interview_id = ? AND revision_number = ?",
                (interview_id, revision_number),
            ).fetchone()
            if existing_for_num and existing_for_num["id"] != revision_id:
                max_num_row = conn.execute(
                    "SELECT MAX(revision_number) FROM transcript_revisions WHERE interview_id = ?",
                    (interview_id,),
                ).fetchone()
                revision_number = (max_num_row[0] or 0) + 1

            conn.execute(
                """
                INSERT INTO transcript_revisions (id, interview_id, revision_number, is_batch_final, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(interview_id, id) DO UPDATE SET
                    is_batch_final = excluded.is_batch_final
                """,
                (revision_id, interview_id, revision_number, 1 if is_batch_final else 0, now),
            )

    def get_transcript_revision(self, interview_id: str, revision_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM transcript_revisions WHERE interview_id = ? AND id = ?",
                (interview_id, revision_id),
            ).fetchone()
            return dict(row) if row else None

    def get_transcript_revisions(self, interview_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM transcript_revisions WHERE interview_id = ? ORDER BY revision_number ASC",
                (interview_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_active_transcript_revision(self, interview_id: str, revision_id: str) -> None:
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Interview {interview_id} not found or deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            # Revision must exist for this interview
            rev = conn.execute(
                "SELECT id FROM transcript_revisions WHERE interview_id = ? AND id = ?",
                (interview_id, revision_id),
            ).fetchone()
            if not rev:
                seg = conn.execute(
                    "SELECT 1 FROM transcript_segments WHERE interview_id = ? AND revision_id = ? LIMIT 1",
                    (interview_id, revision_id),
                ).fetchone()
                if not seg:
                    raise ValueError(f"Transcript revision '{revision_id}' does not exist for interview {interview_id}")

            conn.execute(
                "UPDATE interviews SET active_transcript_revision_id = ? WHERE id = ?",
                (revision_id, interview_id),
            )

    def publish_batch_transcript_revision(
        self,
        interview_id: str,
        expected_old_revision_id: str,
        target_revision_id: str,
        owner_token: str | None = None,
        job_id: str | None = None,
        modified_question_ids: list[str] | None = None,
        stale_reason: str | None = None,
    ) -> None:
        """
        Atomically activates a batch transcript revision and invalidates dependent
        scoring/evidence, ensuring no concurrent manual edits or race conditions
        overwrite newer data.

        Raises:
            ValueError: if interview or target_revision does not exist, or target revision is empty.
            RepositoryConflictError: if interview is finalized/deleted, active revision != expected_old_revision_id,
                                     or job lease is invalid/expired.
        """
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute(
                "SELECT id, status, active_transcript_revision_id FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(
                    f"Cannot publish transcript revision: interview {interview_id} does not exist or is deleted"
                )
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            if job_id is not None:
                j_row = conn.execute(
                    "SELECT id, status, locked_by, locked_until FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if not j_row:
                    raise RepositoryConflictError(f"Cannot publish transcript revision: job {job_id} not found")
                if j_row["status"] != "PROCESSING":
                    raise RepositoryConflictError(
                        f"Cannot publish transcript revision: job {job_id} is no longer PROCESSING (status: {j_row['status']})"
                    )
                if owner_token is not None and j_row["locked_by"] and j_row["locked_by"] != owner_token:
                    raise RepositoryConflictError(
                        f"Cannot publish transcript revision: job {job_id} owner token mismatch"
                    )
                if j_row["locked_until"] and j_row["locked_until"] < now:
                    raise RepositoryConflictError(
                        f"Cannot publish transcript revision: job {job_id} lease expired"
                    )

            active_rev = inv["active_transcript_revision_id"] or "trans-rev-1"
            if active_rev != expected_old_revision_id:
                raise RepositoryConflictError(
                    f"Transcript revision conflict: active revision is '{active_rev}', "
                    f"but expected '{expected_old_revision_id}'. Concurrent manual edit detected."
                )

            # Target revision must exist in transcript_revisions for this interview
            rev = conn.execute(
                "SELECT id FROM transcript_revisions WHERE interview_id = ? AND id = ?",
                (interview_id, target_revision_id),
            ).fetchone()
            if not rev:
                raise ValueError(
                    f"Transcript revision '{target_revision_id}' does not exist for interview {interview_id}"
                )

            # Ensure target revision has at least one segment
            seg_count = conn.execute(
                "SELECT COUNT(1) as cnt FROM transcript_segments WHERE interview_id = ? AND revision_id = ?",
                (interview_id, target_revision_id),
            ).fetchone()["cnt"]
            if seg_count == 0:
                raise ValueError(
                    f"Target revision '{target_revision_id}' has 0 segments, cannot activate empty transcript"
                )

            # Atomically set active revision
            conn.execute(
                "UPDATE interviews SET active_transcript_revision_id = ?, updated_at = ? WHERE id = ?",
                (target_revision_id, now, interview_id),
            )

            # Invalidate modified questions atomically if any
            if modified_question_ids:
                reason = (
                    stale_reason
                    or f"Стенограмма обновлена до {target_revision_id}. Обнаружены расхождения в тексте."
                )
                for q_id in modified_question_ids:
                    conn.execute(
                        "UPDATE assessment_proposals SET is_stale = 1, stale_reason = ? WHERE interview_id = ? AND question_id = ?",
                        (reason, interview_id, q_id),
                    )
                    conn.execute(
                        "UPDATE human_assessments SET is_stale = 1, stale_reason = ? WHERE interview_id = ? AND question_id = ?",
                        (reason, interview_id, q_id),
                    )
                conn.execute(
                    "UPDATE summary_proposals SET is_confirmed = 0, is_stale = 1, stale_reason = ? WHERE interview_id = ?",
                    (reason, interview_id),
                )


    def set_active_rubric_revision(self, interview_id: str, revision_id: str) -> None:
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Interview {interview_id} not found or deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")
            conn.execute(
                "UPDATE interviews SET active_rubric_revision_id = ? WHERE id = ?",
                (revision_id, interview_id),
            )

    # -------------------------------------------------------------
    # Assessment Proposals
    # -------------------------------------------------------------
    def save_assessment_proposal(
        self,
        proposal_id: str,
        interview_id: str,
        question_id: str,
        model_profile_id: str,
        scores: list[dict[str, Any]],
        critical_errors: list[str] | None = None,
        rubric_revision_id: str = "rub-rev-1",
        transcript_revision_id: str = "trans-rev-1",
        is_stale: bool = False,
        stale_reason: str | None = None,
        is_manually_adjusted: bool = False,
        is_rejected: bool = False,
        validation_errors: list[str] | None = None,
        provider_id: str | None = None,
        fallback_metadata: dict[str, Any] | None = None,
        owner_token: str | None = None,
        job_id: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot save proposal: interview {interview_id} does not exist or is deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            if job_id is not None:
                j_row = conn.execute(
                    "SELECT id, status, locked_by, locked_until FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if not j_row:
                    raise RepositoryConflictError(f"Cannot save proposal: job {job_id} not found")
                if j_row["status"] != "PROCESSING":
                    raise RepositoryConflictError(f"Cannot save proposal: job {job_id} is no longer PROCESSING (status: {j_row['status']})")
                if owner_token is not None and j_row["locked_by"] and j_row["locked_by"] != owner_token:
                    raise RepositoryConflictError(f"Cannot save proposal: job {job_id} owner token mismatch")
                if j_row["locked_until"] and j_row["locked_until"] < now:
                    raise RepositoryConflictError(f"Cannot save proposal: job {job_id} lease expired")

            conn.execute(
                """
                INSERT INTO assessment_proposals (
                    id, interview_id, question_id, rubric_revision_id, transcript_revision_id,
                    model_profile_id, scores_json, critical_errors_json, is_approved,
                    is_stale, stale_reason, is_manually_adjusted,
                    is_rejected, validation_errors_json, provider_id, fallback_metadata_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    rubric_revision_id = excluded.rubric_revision_id,
                    transcript_revision_id = excluded.transcript_revision_id,
                    model_profile_id = excluded.model_profile_id,
                    scores_json = excluded.scores_json,
                    critical_errors_json = excluded.critical_errors_json,
                    is_stale = excluded.is_stale,
                    stale_reason = excluded.stale_reason,
                    is_manually_adjusted = excluded.is_manually_adjusted,
                    is_rejected = excluded.is_rejected,
                    validation_errors_json = excluded.validation_errors_json,
                    provider_id = excluded.provider_id,
                    fallback_metadata_json = excluded.fallback_metadata_json
                """,
                (
                    proposal_id,
                    interview_id,
                    question_id,
                    rubric_revision_id,
                    transcript_revision_id,
                    model_profile_id,
                    json.dumps(scores, ensure_ascii=False),
                    json.dumps(critical_errors or [], ensure_ascii=False),
                    1 if is_stale else 0,
                    stale_reason,
                    1 if is_manually_adjusted else 0,
                    1 if is_rejected else 0,
                    json.dumps(validation_errors or [], ensure_ascii=False),
                    provider_id,
                    json.dumps(fallback_metadata, ensure_ascii=False) if fallback_metadata else None,
                    now,
                ),
            )

    def get_assessment_proposals(self, interview_id: str, question_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if question_id:
                rows = conn.execute(
                    """
                    SELECT * FROM assessment_proposals
                    WHERE interview_id = ? AND question_id = ?
                    ORDER BY created_at ASC
                    """,
                    (interview_id, question_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM assessment_proposals
                    WHERE interview_id = ?
                    ORDER BY created_at ASC
                    """,
                    (interview_id,),
                ).fetchall()
            res = []
            for r in rows:
                item = dict(r)
                item["scores"] = json.loads(item["scores_json"])
                item["critical_errors"] = json.loads(item["critical_errors_json"])
                item["validation_errors"] = json.loads(item.get("validation_errors_json") or "[]")
                item["is_rejected"] = bool(item.get("is_rejected", 0))
                if item["reviewed_scores_json"]:
                    item["reviewed_scores"] = json.loads(item["reviewed_scores_json"])
                res.append(item)
            return res

    def get_assessment_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM assessment_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["scores"] = json.loads(item["scores_json"])
            item["critical_errors"] = json.loads(item["critical_errors_json"])
            item["validation_errors"] = json.loads(item.get("validation_errors_json") or "[]")
            item["is_rejected"] = bool(item.get("is_rejected", 0))
            if item["reviewed_scores_json"]:
                item["reviewed_scores"] = json.loads(item["reviewed_scores_json"])
            return item

    def approve_assessment(
        self,
        proposal_id: str,
        reviewed_scores: list[dict[str, Any]] | None = None,
        reviewer_notes: str | None = None,
        interview_id: str | None = None,
        question_id: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM assessment_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if not row and interview_id:
                conn.execute(
                    """
                    INSERT INTO assessment_proposals (
                        id, interview_id, question_id, rubric_revision_id, transcript_revision_id,
                        model_profile_id, scores_json, critical_errors_json, is_approved,
                        reviewed_scores_json, reviewer_notes, reviewed_at, created_at
                    ) VALUES (?, ?, ?, 'rub-rev-1', 'trans-rev-1', 'manual/reviewer', ?, '[]', 1, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        interview_id,
                        question_id or "q-default",
                        json.dumps(reviewed_scores or [], ensure_ascii=False),
                        json.dumps(reviewed_scores or [], ensure_ascii=False),
                        reviewer_notes or "Оценка подтверждена экспертом",
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE assessment_proposals
                    SET is_approved = 1,
                        reviewed_scores_json = ?,
                        reviewer_notes = ?,
                        reviewed_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(reviewed_scores, ensure_ascii=False) if reviewed_scores else None,
                        reviewer_notes,
                        now,
                        proposal_id,
                    ),
                )

    def mark_proposals_stale(
        self,
        interview_id: str,
        question_ids: list[str],
        stale_reason: str,
    ) -> None:
        with self.db.transaction() as conn:
            for q_id in question_ids:
                conn.execute(
                    """
                    UPDATE assessment_proposals
                    SET is_stale = 1, stale_reason = ?
                    WHERE interview_id = ? AND question_id = ?
                    """,
                    (stale_reason, interview_id, q_id),
                )
                conn.execute(
                    """
                    UPDATE human_assessments
                    SET is_stale = 1, stale_reason = ?
                    WHERE interview_id = ? AND question_id = ?
                    """,
                    (stale_reason, interview_id, q_id),
                )
            # Invalidate dependent summary if interview transcript changed
            conn.execute(
                """
                UPDATE summary_proposals
                SET is_confirmed = 0, is_stale = 1, stale_reason = ?
                WHERE interview_id = ?
                """,
                (stale_reason, interview_id),
            )

    # -------------------------------------------------------------
    # Human Assessments (Confirmed/Overridden Decisions)
    # -------------------------------------------------------------
    def save_human_assessment(
        self,
        assessment_id: str,
        interview_id: str,
        question_id: str,
        rubric_revision_id: str,
        transcript_revision_id: str,
        scores: list[dict[str, Any]],
        reviewer_notes: str | None = None,
        reviewer_id: str | None = None,
        is_manually_adjusted: bool = True,
        is_stale: bool = False,
        stale_reason: str | None = None,
        is_excluded: bool = False,
        exclusion_reason: str | None = None,
    ) -> None:
        now = utc_now_iso()
        # Guarantee globally unique ID per interview and question
        if not assessment_id.startswith(f"ha-{interview_id}"):
            assessment_id = f"ha-{interview_id}-{question_id}"

        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot save human assessment: interview {interview_id} does not exist or is deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            conn.execute(
                """
                INSERT INTO human_assessments (
                    id, interview_id, question_id, rubric_revision_id, transcript_revision_id,
                    reviewer_id, scores_json, reviewer_notes, is_manually_adjusted,
                    is_stale, stale_reason, is_excluded, exclusion_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interview_id, question_id) DO UPDATE SET
                    rubric_revision_id = excluded.rubric_revision_id,
                    transcript_revision_id = excluded.transcript_revision_id,
                    reviewer_id = excluded.reviewer_id,
                    scores_json = excluded.scores_json,
                    reviewer_notes = excluded.reviewer_notes,
                    is_manually_adjusted = excluded.is_manually_adjusted,
                    is_stale = excluded.is_stale,
                    stale_reason = excluded.stale_reason,
                    is_excluded = excluded.is_excluded,
                    exclusion_reason = excluded.exclusion_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    assessment_id,
                    interview_id,
                    question_id,
                    rubric_revision_id,
                    transcript_revision_id,
                    reviewer_id,
                    json.dumps(scores, ensure_ascii=False),
                    reviewer_notes,
                    1 if is_manually_adjusted else 0,
                    1 if is_stale else 0,
                    stale_reason,
                    1 if is_excluded else 0,
                    exclusion_reason,
                    now,
                    now,
                ),
            )

            # Human edit invalidates dependent unconfirmed summary proposals
            conn.execute(
                """
                UPDATE summary_proposals
                SET is_stale = 1,
                    stale_reason = 'Оценки были изменены экспертом'
                WHERE interview_id = ? AND is_confirmed = 0
                """,
                (interview_id,),
            )

    def get_human_assessments(self, interview_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM human_assessments WHERE interview_id = ? ORDER BY created_at ASC",
                (interview_id,),
            ).fetchall()
            res = []
            for r in rows:
                item = dict(r)
                item["scores"] = json.loads(item["scores_json"])
                res.append(item)
            return res

    def get_human_assessment_for_question(self, interview_id: str, question_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM human_assessments WHERE interview_id = ? AND question_id = ? ORDER BY updated_at DESC LIMIT 1",
                (interview_id, question_id),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["scores"] = json.loads(item["scores_json"])
            return item

    # -------------------------------------------------------------
    # Executive Summary Proposals
    # -------------------------------------------------------------
    def _compute_decisions_snapshot_hash_conn(self, conn: sqlite3.Connection, interview_id: str) -> str:
        ha_rows = conn.execute(
            """
            SELECT question_id, updated_at, is_stale, is_excluded, scores_json
            FROM human_assessments
            WHERE interview_id = ?
            ORDER BY question_id ASC
            """,
            (interview_id,),
        ).fetchall()
        prop_rows = conn.execute(
            """
            SELECT question_id, created_at, is_stale, is_rejected, scores_json
            FROM assessment_proposals
            WHERE interview_id = ?
            ORDER BY question_id ASC, created_at ASC
            """,
            (interview_id,),
        ).fetchall()
        inv_row = conn.execute(
            "SELECT active_transcript_revision_id, active_rubric_revision_id FROM interviews WHERE id = ?",
            (interview_id,),
        ).fetchone()

        data = {
            "trans_rev": inv_row["active_transcript_revision_id"] if inv_row else None,
            "rub_rev": inv_row["active_rubric_revision_id"] if inv_row else None,
            "ha": [dict(r) for r in ha_rows],
            "prop": [dict(r) for r in prop_rows],
        }
        dumped = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(dumped.encode("utf-8")).hexdigest()

    def compute_decisions_snapshot_hash(self, interview_id: str) -> str:
        with self.db.transaction() as conn:
            return self._compute_decisions_snapshot_hash_conn(conn, interview_id)

    def save_summary_proposal(
        self,
        proposal_id: str,
        interview_id: str,
        model_profile_id: str,
        summary_data: dict[str, Any],
        transcript_revision_id: str | None = None,
        rubric_revision_id: str | None = None,
        decisions_snapshot_hash: str | None = None,
        is_stale: bool = False,
        stale_reason: str | None = None,
        owner_token: str | None = None,
        job_id: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute(
                "SELECT id, status, active_transcript_revision_id, active_rubric_revision_id FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot save summary proposal: interview {interview_id} does not exist or is deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            if job_id is not None:
                j_row = conn.execute(
                    "SELECT id, status, locked_by, locked_until FROM jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                if not j_row:
                    raise RepositoryConflictError(f"Cannot save summary proposal: job {job_id} not found")
                if j_row["status"] != "PROCESSING":
                    raise RepositoryConflictError(f"Cannot save summary proposal: job {job_id} is no longer PROCESSING (status: {j_row['status']})")
                if owner_token is not None and j_row["locked_by"] and j_row["locked_by"] != owner_token:
                    raise RepositoryConflictError(f"Cannot save summary proposal: job {job_id} owner token mismatch")
                if j_row["locked_until"] and j_row["locked_until"] < now:
                    raise RepositoryConflictError(f"Cannot save summary proposal: job {job_id} lease expired")

            active_trans = inv["active_transcript_revision_id"] or "trans-rev-1"
            active_rub = inv["active_rubric_revision_id"] or "rub-rev-1"
            trans_rev = transcript_revision_id or active_trans
            rub_rev = rubric_revision_id or active_rub

            # Verify if decisions snapshot changed during generation
            if decisions_snapshot_hash is not None:
                current_hash = self._compute_decisions_snapshot_hash_conn(conn, interview_id)
                if current_hash != decisions_snapshot_hash:
                    is_stale = True
                    stale_reason = stale_reason or "Оценки были изменены во время генерации резюме"

            if trans_rev != active_trans:
                is_stale = True
                stale_reason = stale_reason or f"Стенограмма обновилась до {active_trans}"

            conn.execute(
                """
                INSERT INTO summary_proposals (
                    id, interview_id, transcript_revision_id, rubric_revision_id,
                    model_profile_id, summary_data_json, decisions_snapshot_hash,
                    is_confirmed, is_stale, stale_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    transcript_revision_id = excluded.transcript_revision_id,
                    rubric_revision_id = excluded.rubric_revision_id,
                    model_profile_id = excluded.model_profile_id,
                    summary_data_json = excluded.summary_data_json,
                    decisions_snapshot_hash = excluded.decisions_snapshot_hash,
                    is_stale = excluded.is_stale,
                    stale_reason = excluded.stale_reason
                """,
                (
                    proposal_id,
                    interview_id,
                    trans_rev,
                    rub_rev,
                    model_profile_id,
                    json.dumps(summary_data, ensure_ascii=False),
                    decisions_snapshot_hash,
                    1 if is_stale else 0,
                    stale_reason,
                    now,
                ),
            )

    def get_summary_proposal(self, interview_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM summary_proposals WHERE interview_id = ? ORDER BY created_at DESC LIMIT 1",
                (interview_id,),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["summary_data"] = json.loads(item["summary_data_json"])
            item["is_confirmed"] = bool(item.get("is_confirmed", 0))
            item["is_stale"] = bool(item.get("is_stale", 0))

            # If current latest proposal is not confirmed, check if there is a previously confirmed summary
            # so human confirmed text is never lost when a new AI proposal is generated!
            if not item["is_confirmed"]:
                confirmed_row = conn.execute(
                    """
                    SELECT confirmed_markdown, confirmed_recommendation, confirmed_by, confirmed_at
                    FROM summary_proposals
                    WHERE interview_id = ? AND is_confirmed = 1
                    ORDER BY confirmed_at DESC LIMIT 1
                    """,
                    (interview_id,),
                ).fetchone()
                if confirmed_row and confirmed_row["confirmed_markdown"]:
                    item["confirmed_markdown"] = confirmed_row["confirmed_markdown"]
                    item["confirmed_recommendation"] = confirmed_row["confirmed_recommendation"]
                    item["confirmed_by"] = confirmed_row["confirmed_by"]
                    item["confirmed_at"] = confirmed_row["confirmed_at"]

            return item

    def confirm_summary(
        self,
        interview_id: str,
        reviewer_id: str,
        confirmed_markdown: str,
        confirmed_recommendation: str,
        summary_id: str | None = None,
        expected_transcript_revision: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv_row = conn.execute(
                "SELECT id, status, active_transcript_revision_id, active_rubric_revision_id FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
            if not inv_row or inv_row["status"] == "deleted":
                raise ValueError(f"Cannot confirm summary: interview {interview_id} does not exist or is deleted")
            inv = dict(inv_row)
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            active_rev = inv.get("active_transcript_revision_id") or "trans-rev-1"
            if expected_transcript_revision and expected_transcript_revision != active_rev:
                raise RepositoryConflictError(
                    f"Revision conflict: active transcript revision is '{active_rev}', "
                    f"but summary confirmation was submitted for '{expected_transcript_revision}'."
                )

            if summary_id:
                row = conn.execute(
                    "SELECT * FROM summary_proposals WHERE id = ? AND interview_id = ?",
                    (summary_id, interview_id),
                ).fetchone()
                if not row:
                    raise RepositoryConflictError(f"Summary proposal {summary_id} not found for interview {interview_id}")
            else:
                row = conn.execute(
                    "SELECT * FROM summary_proposals WHERE interview_id = ? ORDER BY created_at DESC LIMIT 1",
                    (interview_id,),
                ).fetchone()

            active_rub = inv.get("active_rubric_revision_id") or "rub-rev-1"
            if not row:
                # Human expert confirms manual summary without prior AI proposal
                prop_id = f"sum-manual-{uuid.uuid4().hex[:8]}"
                conn.execute(
                    """
                    INSERT INTO summary_proposals (
                        id, interview_id, transcript_revision_id, rubric_revision_id,
                        model_profile_id, summary_data_json, is_confirmed,
                        confirmed_by, confirmed_markdown, confirmed_recommendation,
                        is_stale, created_at, confirmed_at
                    ) VALUES (?, ?, ?, ?, 'manual/expert', '{}', 1, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        prop_id,
                        interview_id,
                        active_rev,
                        active_rub,
                        reviewer_id,
                        confirmed_markdown,
                        confirmed_recommendation,
                        now,
                        now,
                    ),
                )
                return

            if summary_id and row["transcript_revision_id"] and row["transcript_revision_id"] != active_rev:
                raise RepositoryConflictError(
                    f"Revision conflict: summary proposal is based on revision '{row['transcript_revision_id']}', but active revision is '{active_rev}'"
                )

            # Human confirmation resolves stale flag and binds to active revision
            conn.execute(
                """
                UPDATE summary_proposals
                SET is_confirmed = 1,
                    is_stale = 0,
                    stale_reason = NULL,
                    transcript_revision_id = ?,
                    confirmed_by = ?,
                    confirmed_markdown = ?,
                    confirmed_recommendation = ?,
                    confirmed_at = ?
                WHERE id = ?
                """,
                (active_rev, reviewer_id, confirmed_markdown, confirmed_recommendation, now, row["id"]),
            )

    # -------------------------------------------------------------
    # Final Report Revisions (Sealed & Checksummed)
    # -------------------------------------------------------------
    def save_report_revision(
        self,
        report_id: str,
        interview_id: str,
        revision_number: int,
        final_score_100: float | None,
        coverage_percentage: float,
        question_scores: dict[str, Any],
        summary_markdown: str,
        hiring_recommendation: str,
        confirmed_by: str,
        sha256_checksum: str,
        canonical_snapshot_json: str | None = None,
    ) -> None:
        now = utc_now_iso()
        # Guarantee globally unique ID per interview and revision number
        if not report_id.startswith(f"rep-{interview_id}"):
            report_id = f"rep-{interview_id}-rev-{revision_number}"

        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot save report revision: interview {interview_id} does not exist or is deleted")

            conn.execute(
                """
                INSERT INTO report_revisions (
                    id, interview_id, revision_number, final_score_100, coverage_percentage,
                    question_scores_json, summary_markdown, hiring_recommendation,
                    confirmed_by, sha256_checksum, canonical_snapshot_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interview_id, revision_number) DO UPDATE SET
                    final_score_100 = excluded.final_score_100,
                    coverage_percentage = excluded.coverage_percentage,
                    question_scores_json = excluded.question_scores_json,
                    summary_markdown = excluded.summary_markdown,
                    hiring_recommendation = excluded.hiring_recommendation,
                    confirmed_by = excluded.confirmed_by,
                    sha256_checksum = excluded.sha256_checksum,
                    canonical_snapshot_json = excluded.canonical_snapshot_json
                """,
                (
                    report_id,
                    interview_id,
                    revision_number,
                    final_score_100,
                    coverage_percentage,
                    json.dumps(question_scores, ensure_ascii=False),
                    summary_markdown,
                    hiring_recommendation,
                    confirmed_by,
                    sha256_checksum,
                    canonical_snapshot_json,
                    now,
                ),
            )

    def get_report_revisions(self, interview_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM report_revisions WHERE interview_id = ? ORDER BY revision_number ASC",
                (interview_id,),
            ).fetchall()
            res = []
            for r in rows:
                item = dict(r)
                item["question_scores"] = json.loads(item["question_scores_json"])
                if item.get("canonical_snapshot_json"):
                    item["canonical_snapshot"] = json.loads(item["canonical_snapshot_json"])
                res.append(item)
            return res

    def get_latest_report_revision(self, interview_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM report_revisions WHERE interview_id = ? ORDER BY revision_number DESC LIMIT 1",
                (interview_id,),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["question_scores"] = json.loads(item["question_scores_json"])
            if item.get("canonical_snapshot_json"):
                item["canonical_snapshot"] = json.loads(item["canonical_snapshot_json"])
            return item

    def finalize_interview(
        self,
        interview_id: str,
        summary_markdown: str,
        hiring_recommendation: str,
        confirmed_by: str,
        audio_limitations: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Atomically validates review state, calculates deterministic scoring,
        creates canonical snapshot with SHA-256 checksum, transitions status to FINALIZED,
        and records audit event in a single database transaction.
        """
        if not summary_markdown or not summary_markdown.strip():
            raise ValueError("summary_markdown must be provided and cannot be empty")
        if not hiring_recommendation or not hiring_recommendation.strip():
            raise ValueError("hiring_recommendation must be provided and cannot be empty")
        if not confirmed_by or not confirmed_by.strip():
            raise ValueError("confirmed_by must be provided and cannot be empty")

        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv_row = conn.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv_row or inv_row["status"] == "deleted":
                raise KeyError(f"Interview {interview_id} not found or deleted")
            inv = dict(inv_row)

            current_status = inv["status"]

            # Idempotence: if already finalized, check if this is an identical repeat
            if current_status == "finalized":
                latest_rep = conn.execute(
                    "SELECT * FROM report_revisions WHERE interview_id = ? ORDER BY revision_number DESC LIMIT 1",
                    (interview_id,),
                ).fetchone()
                if latest_rep:
                    rep_dict = dict(latest_rep)
                    existing_rec = rep_dict.get("hiring_recommendation")
                    if hiring_recommendation and existing_rec != hiring_recommendation:
                        raise RepositoryConflictError(
                            f"Interview is already finalized with recommendation '{existing_rec}'. "
                            f"Cannot re-finalize with '{hiring_recommendation}'"
                        )
                    snapshot = json.loads(rep_dict["canonical_snapshot_json"]) if rep_dict.get("canonical_snapshot_json") else None
                    return {
                        "status": "finalized",
                        "already_finalized": True,
                        "final_score_100": rep_dict["final_score_100"],
                        "coverage_percentage": rep_dict["coverage_percentage"],
                        "sha256_checksum": rep_dict["sha256_checksum"],
                        "report_id": rep_dict["id"],
                        "revision_number": rep_dict["revision_number"],
                        "snapshot": snapshot,
                    }

            if current_status != "review":
                raise InvalidStateTransitionError(
                    InterviewStatus(current_status),
                    InterviewStatus.FINALIZED,
                    f"Finalization is only permitted from REVIEW status (currently in '{current_status}')"
                )

            # Retrieve interview plan questions
            plan_row = conn.execute(
                "SELECT * FROM interview_plans WHERE interview_id = ? ORDER BY version DESC LIMIT 1",
                (interview_id,),
            ).fetchone()
            plan_payload = json.loads(plan_row["payload_json"]) if plan_row else {}
            questions = plan_payload.get("questions", [])

            # Retrieve human assessments
            ha_rows = conn.execute(
                "SELECT * FROM human_assessments WHERE interview_id = ?",
                (interview_id,),
            ).fetchall()
            ha_map: dict[str, dict[str, Any]] = {}
            for r in ha_rows:
                h_item = dict(r)
                h_item["scores"] = json.loads(h_item["scores_json"])
                ha_map[h_item["question_id"]] = h_item

            # Validate each planned question
            domain_questions: list[PlannedQuestion] = []
            for q in questions:
                q_id = q.get("id") or q.get("question_id")
                if not q_id:
                    continue

                if q_id not in ha_map:
                    raise ValueError(
                        f"Question '{q_id}' ('{q.get('title') or q.get('prompt', '')}') is unassessed. "
                        "All planned questions must have confirmed human evaluations or explicit exclusions."
                    )

                ha = ha_map[q_id]
                if ha.get("is_stale"):
                    stale_reason = ha.get("stale_reason") or "transcript revision changed"
                    raise RepositoryConflictError(
                        f"Human assessment for question '{q_id}' is stale: {stale_reason}. "
                        "Please re-confirm review against the latest transcript before finalizing."
                    )

                active_trans_rev = inv.get("active_transcript_revision_id") or "trans-rev-1"
                if ha.get("transcript_revision_id") and ha.get("transcript_revision_id") != active_trans_rev:
                    raise RepositoryConflictError(
                        f"Human assessment for question '{q_id}' was made on revision '{ha.get('transcript_revision_id')}', "
                        f"but active transcript revision is '{active_trans_rev}'. "
                        "Please re-confirm review against the latest transcript before finalizing."
                    )


                is_excluded = bool(ha.get("is_excluded"))
                if is_excluded:
                    ex_reason = ha.get("exclusion_reason")
                    if not ex_reason or not str(ex_reason).strip():
                        raise ValueError(
                            f"Question '{q_id}' is marked as excluded but lacks an explicit exclusion reason."
                        )
                else:
                    # Validate that all criteria have scores
                    eval_crit_ids = {sc["criterion_id"]: sc.get("score") for sc in ha.get("scores", [])}
                    for crit in q.get("criteria", []):
                        c_id = crit["id"]
                        if c_id not in eval_crit_ids or eval_crit_ids[c_id] is None:
                            raise ValueError(
                                f"Question '{q_id}' has unassessed criterion '{c_id}' ('{crit.get('title', '')}')."
                            )

                domain_criteria = [
                    RubricCriterion(
                        id=c["id"],
                        title=c.get("title", ""),
                        description=c.get("description", ""),
                        min_score=float(c.get("min_score", 1.0)),
                        max_score=float(c.get("max_score", 5.0)),
                        weight=float(c.get("weight", 1.0)),
                    )
                    for c in q.get("criteria", [])
                ]
                domain_questions.append(
                    PlannedQuestion(
                        id=q_id,
                        title=q.get("title", ""),
                        prompt=q.get("prompt") or q.get("text", ""),
                        weight=float(q.get("weight", 1.0)),
                        criteria=domain_criteria,
                    )
                )

            # Build domain assessments (only human assessments count!)
            domain_assessments: list[HumanQuestionAssessment] = []
            for ha in ha_map.values():
                crit_scores = [
                    HumanCriterionScore(
                        criterion_id=sc["criterion_id"],
                        score=float(sc["score"]) if sc.get("score") is not None else None,
                        explanation=sc.get("explanation"),
                    )
                    for sc in ha.get("scores", [])
                ]
                domain_assessments.append(
                    HumanQuestionAssessment(
                        question_id=ha["question_id"],
                        criteria_scores=crit_scores,
                        reviewer_notes=ha.get("reviewer_notes"),
                        reviewer_id=ha.get("reviewer_id") or confirmed_by,
                        is_manually_adjusted=bool(ha.get("is_manually_adjusted")),
                        is_excluded=bool(ha.get("is_excluded")),
                        exclusion_reason=ha.get("exclusion_reason"),
                    )
                )

            active_rubric_rev = inv.get("active_rubric_revision_id") or "rub-rev-1"
            active_trans_rev = inv.get("active_transcript_revision_id") or "trans-rev-1"
            rubric = RubricRevision(
                revision_id=active_rubric_rev,
                interview_id=interview_id,
                questions=domain_questions,
            )

            score_res = calculate_interview_score(rubric, domain_assessments)

            # Next revision number
            rev_row = conn.execute(
                "SELECT COUNT(*) FROM report_revisions WHERE interview_id = ?",
                (interview_id,),
            ).fetchone()
            rev_num = (rev_row[0] if rev_row else 0) + 1
            report_id = f"rep-{interview_id}-rev-{rev_num}"

            # Canonical question_scores for quick lookup
            question_scores_dict: dict[str, Any] = {}
            for ha in ha_map.values():
                q_id = ha["question_id"]
                is_ex = bool(ha.get("is_excluded"))
                scores_list = ha.get("scores", [])
                avg_score = None
                if not is_ex and scores_list:
                    valid_vals = [sc["score"] for sc in scores_list if sc.get("score") is not None]
                    if valid_vals:
                        avg_score = sum(valid_vals) / len(valid_vals)
                question_scores_dict[q_id] = {
                    "is_excluded": is_ex,
                    "exclusion_reason": ha.get("exclusion_reason"),
                    "score": avg_score,
                    "criteria_scores": scores_list,
                    "reviewer_notes": ha.get("reviewer_notes"),
                }

            fu_rows = conn.execute(
                """
                SELECT id, question_id, kind, question_text, purpose, asked_text,
                       rubric_revision_id, transcript_revision_id, decided_at
                FROM followup_suggestions
                WHERE interview_id = ? AND status = 'asked'
                ORDER BY decided_at ASC, created_at ASC
                """,
                (interview_id,),
            ).fetchall()
            asked_followups = [
                {
                    "id": r["id"],
                    "question_id": r["question_id"],
                    "kind": r["kind"],
                    "question_text": r["question_text"],
                    "purpose": r["purpose"],
                    "asked_text": r["asked_text"] or r["question_text"],
                    "rubric_revision_id": r["rubric_revision_id"],
                    "transcript_revision_id": r["transcript_revision_id"],
                    "decided_at": r["decided_at"],
                }
                for r in fu_rows
            ]

            snapshot_dict = {
                "interview_id": interview_id,
                "revision_number": rev_num,
                "candidate_name": inv.get("candidate_name"),
                "role": inv.get("role"),
                "title": inv.get("title"),
                "finalized_at": now,
                "confirmed_by": confirmed_by.strip(),
                "formula_version": "1.0-linear-midpoint",
                "input_revisions": {
                    "transcript_revision_id": active_trans_rev,
                    "rubric_revision_id": active_rubric_rev,
                },
                "scoring": {
                    "final_score_100": score_res.final_score_100,
                    "coverage_percentage": round(score_res.coverage_percentage, 2),
                    "total_planned_weight": score_res.total_planned_weight,
                    "evaluated_weight": score_res.evaluated_weight,
                },
                "questions": questions,
                "human_assessments": [
                    {
                        "question_id": ha["question_id"],
                        "scores": ha["scores"],
                        "reviewer_notes": ha.get("reviewer_notes"),
                        "reviewer_id": ha.get("reviewer_id") or confirmed_by,
                        "is_manually_adjusted": bool(ha.get("is_manually_adjusted")),
                        "is_excluded": bool(ha.get("is_excluded")),
                        "exclusion_reason": ha.get("exclusion_reason"),
                        "updated_at": ha.get("updated_at"),
                    }
                    for ha in ha_map.values()
                ],
                "executive_summary": {
                    "summary_markdown": summary_markdown.strip(),
                    "hiring_recommendation": hiring_recommendation.strip(),
                    "confirmed_by": confirmed_by.strip(),
                    "confirmed_at": now,
                },
                "audio_limitations": audio_limitations or [],
                "followup_questions": asked_followups,
            }
            canonical_json = json.dumps(snapshot_dict, sort_keys=True, ensure_ascii=False)
            sha256_checksum = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

            # Insert report revision
            conn.execute(
                """
                INSERT INTO report_revisions (
                    id, interview_id, revision_number, final_score_100, coverage_percentage,
                    question_scores_json, summary_markdown, hiring_recommendation,
                    confirmed_by, sha256_checksum, canonical_snapshot_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    interview_id,
                    rev_num,
                    score_res.final_score_100,
                    round(score_res.coverage_percentage, 2),
                    json.dumps(question_scores_dict, ensure_ascii=False),
                    summary_markdown.strip(),
                    hiring_recommendation.strip(),
                    confirmed_by.strip(),
                    sha256_checksum,
                    canonical_json,
                    now,
                ),
            )

            # Update executive summary proposal if exists
            conn.execute(
                """
                UPDATE summary_proposals
                SET is_confirmed = 1,
                    confirmed_by = ?,
                    confirmed_markdown = ?,
                    confirmed_recommendation = ?,
                    confirmed_at = ?
                WHERE interview_id = ?
                """,
                (confirmed_by.strip(), summary_markdown.strip(), hiring_recommendation.strip(), now, interview_id),
            )

            # Update interview status to finalized
            conn.execute(
                "UPDATE interviews SET status = 'finalized', updated_at = ? WHERE id = ?",
                (now, interview_id),
            )

            # Audit event
            conn.execute(
                """
                INSERT INTO audit_events (id, interview_id, event_type, payload_json, created_at)
                VALUES (?, ?, 'INTERVIEW_FINALIZED', ?, ?)
                """,
                (
                    f"audit-{sha256_checksum[:8]}",
                    interview_id,
                    json.dumps(
                        {
                            "report_id": report_id,
                            "sha256_checksum": sha256_checksum,
                            "confirmed_by": confirmed_by.strip(),
                            "final_score_100": score_res.final_score_100,
                            "hiring_recommendation": hiring_recommendation.strip(),
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

            return {
                "status": "finalized",
                "final_score_100": score_res.final_score_100,
                "coverage_percentage": round(score_res.coverage_percentage, 2),
                "sha256_checksum": sha256_checksum,
                "report_id": report_id,
                "revision_number": rev_num,
                "snapshot": snapshot_dict,
            }

    def reopen_revision(
        self,
        interview_id: str,
        reviewer_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """
        Opens a new revision cycle for an already finalized interview,
        preserving historical sealed report snapshots in report_revisions.
        """
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise KeyError(f"Interview {interview_id} not found or deleted")
            if inv["status"] != "finalized":
                raise InvalidStateTransitionError(
                    InterviewStatus(inv["status"]),
                    InterviewStatus.REVIEW,
                    f"Cannot reopen revision: interview status is '{inv['status']}', expected 'finalized'"
                )

            conn.execute(
                "UPDATE interviews SET status = 'review', updated_at = ? WHERE id = ?",
                (now, interview_id),
            )
            audit_id = f"audit-{hashlib.sha256((interview_id + now).encode('utf-8')).hexdigest()[:8]}"
            conn.execute(
                """
                INSERT INTO audit_events (id, interview_id, event_type, payload_json, created_at)
                VALUES (?, ?, 'REVISION_REOPENED', ?, ?)
                """,
                (
                    audit_id,
                    interview_id,
                    json.dumps({"reviewer_id": reviewer_id, "reason": reason}, ensure_ascii=False),
                    now,
                ),
            )
            return {"status": "review", "interview_id": interview_id, "reopened_at": now}

    # -------------------------------------------------------------
    # Durable Jobs
    # -------------------------------------------------------------
    def enqueue_job(
        self,
        job_id: str,
        job_type: str,
        interview_id: str,
        payload: dict[str, Any],
        max_attempts: int = 3,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot enqueue job: interview {interview_id} does not exist or is deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            if job_type == "BATCH_RETRANSCRIBE":
                active_batch = conn.execute(
                    """
                    SELECT id FROM jobs
                    WHERE interview_id = ? AND type = 'BATCH_RETRANSCRIBE' AND status IN ('PENDING', 'PROCESSING')
                    LIMIT 1
                    """,
                    (interview_id,),
                ).fetchone()
                if active_batch:
                    raise RepositoryConflictError(
                        f"A batch retranscription job ({active_batch['id']}) is already pending/processing for interview {interview_id}"
                    )

            conn.execute(
                """
                INSERT INTO jobs (
                    id, type, interview_id, payload_json, status, attempts, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)
                """,
                (job_id, job_type, interview_id, json.dumps(payload, ensure_ascii=False), max_attempts, now, now),
            )

    def claim_next_job(
        self,
        lock_duration_sec: int = 60,
        include_types: list[str] | tuple[str, ...] | None = None,
        exclude_types: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically leases next available pending or expired job with owner token."""
        now = utc_now_iso()
        owner_token = f"worker-{uuid.uuid4().hex}"
        with self.db.transaction() as conn:
            # 1. Terminal failure for expired PROCESSING jobs that exceeded max_attempts
            conn.execute(
                """
                UPDATE jobs
                SET status = 'FAILED',
                    error_message = 'Job lease expired and max attempts exceeded',
                    locked_until = NULL,
                    locked_by = NULL,
                    updated_at = ?
                WHERE status = 'PROCESSING' AND locked_until < ? AND attempts >= max_attempts
                """,
                (now, now),
            )

            # 2. Find eligible job: PENDING (whose retry backoff locked_until has passed)
            # or expired PROCESSING (where attempts < max_attempts)
            where_clauses = [
                "(((j.status = 'PENDING' AND (j.locked_until IS NULL OR j.locked_until <= ?)) "
                "OR (j.status = 'PROCESSING' AND j.locked_until < ? AND j.attempts < j.max_attempts)))"
            ]
            params: list[Any] = [now, now]

            if include_types:
                placeholders = ",".join("?" for _ in include_types)
                where_clauses.append(f"j.type IN ({placeholders})")
                params.extend(include_types)
            elif exclude_types:
                placeholders = ",".join("?" for _ in exclude_types)
                where_clauses.append(f"j.type NOT IN ({placeholders})")
                params.extend(exclude_types)

            where_sql = " AND ".join(where_clauses)
            query = f"""
                SELECT j.* FROM jobs j
                LEFT JOIN interviews i ON j.interview_id = i.id
                WHERE {where_sql}
                ORDER BY
                    CASE WHEN i.status = 'recording' THEN 0 ELSE 1 END ASC,
                    j.created_at ASC
                LIMIT 1
            """
            row = conn.execute(query, params).fetchone()
            if not row:
                return None

            job = dict(row)
            # Lock it with owner token
            lock_until_dt = datetime.fromtimestamp(datetime.now(UTC).timestamp() + lock_duration_sec, tz=UTC)
            lock_until_str = lock_until_dt.isoformat()
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'PROCESSING',
                    attempts = attempts + 1,
                    locked_until = ?,
                    locked_by = ?,
                    updated_at = ?,
                    started_at = ?
                WHERE id = ? AND (
                    (status = 'PENDING' AND (locked_until IS NULL OR locked_until <= ?))
                    OR (status = 'PROCESSING' AND locked_until < ? AND attempts < max_attempts)
                )
                """,
                (lock_until_str, owner_token, now, now, job["id"], now, now),
            )
            if cursor.rowcount == 0:
                # Concurrent race condition: another worker claimed it in between
                return None

            job["payload"] = json.loads(job["payload_json"])
            job["attempts"] += 1
            job["locked_by"] = owner_token
            job["started_at"] = now
            return job

    def claim_adjacent_transcribe_jobs(
        self,
        interview_id: str,
        track_id: str,
        last_end_ms: int | None = None,
        epoch: int | None = None,
        max_additional: int = 3,
        owner_token: str | None = None,
        lock_duration_sec: int = 60,
        tolerance_ms: int = 100,
    ) -> list[dict[str, Any]]:
        """Claims up to max_additional pending TRANSCRIBE_AUDIO jobs for the same interview, track, epoch, and consecutive timeline."""
        if max_additional <= 0:
            return []
        now = utc_now_iso()
        lock_until_dt = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() + lock_duration_sec, tz=UTC
        )
        lock_until_str = lock_until_dt.isoformat()
        results = []
        current_end_ms = last_end_ms

        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE interview_id = ? AND type = 'TRANSCRIBE_AUDIO' AND status = 'PENDING'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (interview_id, max_additional * 5),  # Fetch a reasonable window to find consecutive matching chunks
            ).fetchall()

            for r in rows:
                if len(results) >= max_additional:
                    break
                j = dict(r)
                p = json.loads(j["payload_json"])
                if p.get("track_id") != track_id:
                    continue
                if epoch is not None and p.get("epoch") is not None and p.get("epoch") != epoch:
                    # Epoch mismatch - break continuity
                    break

                chunk_start = p.get("start_ms", 0)
                chunk_end = p.get("end_ms", chunk_start)
                # Check continuity: chunk must start where previous ended (within tolerance)
                if current_end_ms is not None and abs(chunk_start - current_end_ms) > tolerance_ms:
                    # Timeline gap or jump detected, stop grouping
                    break

                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'PROCESSING',
                        attempts = attempts + 1,
                        locked_until = ?,
                        locked_by = ?,
                        updated_at = ?,
                        started_at = ?
                    WHERE id = ? AND status = 'PENDING'
                    """,
                    (lock_until_str, owner_token, now, now, j["id"]),
                )
                if cursor.rowcount > 0:
                    j["payload"] = p
                    j["attempts"] += 1
                    j["locked_by"] = owner_token
                    j["started_at"] = now
                    results.append(j)
                    current_end_ms = chunk_end
        return results

    def renew_job_lease(self, job_id: str, owner_token: str, extension_sec: int = 60) -> bool:
        """Extends the lease duration for an actively executing job if owner token matches."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            lock_until_dt = datetime.fromtimestamp(datetime.now(UTC).timestamp() + extension_sec, tz=UTC)
            lock_until_str = lock_until_dt.isoformat()
            cursor = conn.execute(
                """
                UPDATE jobs
                SET locked_until = ?, updated_at = ?
                WHERE id = ? AND status = 'PROCESSING' AND locked_by = ?
                """,
                (lock_until_str, now, job_id, owner_token),
            )
            return cursor.rowcount > 0

    def update_job_checkpoint(
        self,
        job_id: str,
        owner_token: str,
        checkpoint_data: dict[str, Any],
        extension_sec: int = 60,
    ) -> bool:
        """Atomically updates job payload checkpoint and extends lease if owner token matches."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM jobs WHERE id = ? AND status = 'PROCESSING' AND locked_by = ?",
                (job_id, owner_token),
            ).fetchone()
            if not row:
                return False
            payload = json.loads(row["payload_json"])
            checkpoint = payload.setdefault("checkpoint", {})
            for k, v in checkpoint_data.items():
                if isinstance(v, list) and isinstance(checkpoint.get(k), list):
                    existing_set = set(checkpoint[k])
                    for item in v:
                        if item not in existing_set:
                            checkpoint[k].append(item)
                else:
                    checkpoint[k] = v
            new_payload_json = json.dumps(payload, ensure_ascii=False)
            lock_until_dt = datetime.fromtimestamp(datetime.now(UTC).timestamp() + extension_sec, tz=UTC)
            cursor = conn.execute(
                """
                UPDATE jobs
                SET payload_json = ?, locked_until = ?, updated_at = ?
                WHERE id = ? AND status = 'PROCESSING' AND locked_by = ?
                """,
                (new_payload_json, lock_until_dt.isoformat(), now, job_id, owner_token),
            )
            return cursor.rowcount > 0

    def reclaim_expired_jobs(self, retry_delay_sec: int = 5) -> int:
        """Counts and resets expired PROCESSING jobs back to PENDING for retry, or FAILED if max attempts exceeded."""
        now = utc_now_iso()
        retry_until_dt = datetime.fromtimestamp(datetime.now(UTC).timestamp() + retry_delay_sec, tz=UTC)
        retry_until_str = retry_until_dt.isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'FAILED', error_message = 'Job lease expired and max attempts exceeded', locked_until = NULL, locked_by = NULL, updated_at = ?
                WHERE status = 'PROCESSING' AND locked_until < ? AND attempts >= max_attempts
                """,
                (now, now),
            )
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'PENDING', locked_until = ?, locked_by = NULL, updated_at = ?
                WHERE status = 'PROCESSING' AND locked_until < ? AND attempts < max_attempts
                """,
                (retry_until_str, now, now),
            )
            return cursor.rowcount

    def complete_job(self, job_id: str, owner_token: str | None = None) -> None:
        """Marks job as COMPLETED, verifying owner token if supplied."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            if owner_token is not None:
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'COMPLETED', updated_at = ?, completed_at = ?, locked_until = NULL, locked_by = NULL
                    WHERE id = ? AND status = 'PROCESSING' AND (locked_by = ? OR locked_by IS NULL)
                    """,
                    (now, now, job_id, owner_token),
                )
                if cursor.rowcount == 0:
                    raise RepositoryConflictError(f"Cannot complete job {job_id}: lease expired or owner token mismatch")
            else:
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'COMPLETED', updated_at = ?, completed_at = ?, locked_until = NULL, locked_by = NULL WHERE id = ?",
                    (now, now, job_id),
                )
                if cursor.rowcount == 0:
                    raise RepositoryConflictError(f"Cannot complete job {job_id}: job not found")

    def fail_job(
        self,
        job_id: str,
        error_message: str,
        owner_token: str | None = None,
        retry_delay_sec: int = 0,
        is_terminal: bool = False,
    ) -> None:
        """Marks job as FAILED or PENDING retry with durable delay, verifying owner token if supplied."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT attempts, max_attempts, status, locked_by FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                raise RepositoryConflictError(f"Cannot fail job {job_id}: job not found")

            if owner_token is not None:
                if row["status"] != "PROCESSING" or row["locked_by"] != owner_token:
                    raise RepositoryConflictError(
                        f"Cannot fail job {job_id}: status is {row['status']} or owner token mismatch"
                    )
            elif row["status"] in ("COMPLETED", "FAILED"):
                raise RepositoryConflictError(f"Cannot fail job {job_id}: job is already terminal ({row['status']})")

            if is_terminal or (row["attempts"] >= row["max_attempts"]):
                status = "FAILED"
                retry_until_str = None
                completed_at_str = now
            else:
                status = "PENDING"  # Re-enqueue for retry with backoff delay
                completed_at_str = None
                if retry_delay_sec > 0:
                    retry_until_dt = datetime.fromtimestamp(datetime.now(UTC).timestamp() + retry_delay_sec, tz=UTC)
                    retry_until_str = retry_until_dt.isoformat()
                else:
                    retry_until_str = None
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, locked_until = ?, locked_by = NULL, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, error_message, retry_until_str, now, completed_at_str, job_id),
            )

    def find_active_evaluate_job(
        self,
        interview_id: str,
        question_id: str,
        candidate_fingerprint: str | None = None,
        rubric_revision_id: str | None = None,
        transcript_revision_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Finds an active (PENDING or PROCESSING) EVALUATE_QUESTION job matching the given
        interview, question, revisions, and candidate speech fingerprint."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT id, type, status, attempts, max_attempts, payload_json, error_message,
                       locked_until, created_at, started_at
                FROM jobs
                WHERE interview_id = ? AND type = 'EVALUATE_QUESTION' AND status IN ('PENDING', 'PROCESSING')
                ORDER BY created_at DESC
                """,
                (interview_id,),
            ).fetchall()

            for r in rows:
                p: dict[str, Any] = {}
                if r["payload_json"]:
                    with contextlib.suppress(Exception):
                        p = json.loads(r["payload_json"])

                if p.get("question_id") != question_id:
                    continue
                if rubric_revision_id is not None and p.get("rubric_revision_id") != rubric_revision_id:
                    continue
                if transcript_revision_id is not None and p.get("transcript_revision_id") != transcript_revision_id:
                    continue
                if p.get("candidate_fingerprint") == candidate_fingerprint:
                    j = dict(r)
                    j["payload"] = p
                    return j
        return None

    def get_interview_jobs_status(
        self,
        interview_id: str,
        job_ids: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Returns job counts, telemetry metrics and active/requested job details for an interview."""
        now_dt = datetime.now(UTC)
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT type, status, COUNT(*) as cnt FROM jobs WHERE interview_id = ? GROUP BY type, status",
                (interview_id,),
            ).fetchall()
            status_counts: dict[str, int] = {}
            counts_by_type: dict[str, dict[str, int]] = {}
            for r in rows:
                j_type = r["type"]
                j_stat = r["status"]
                cnt = r["cnt"]
                status_counts[j_stat] = status_counts.get(j_stat, 0) + cnt
                if j_type not in counts_by_type:
                    counts_by_type[j_type] = {}
                counts_by_type[j_type][j_stat] = cnt

            pending_or_processing = (
                status_counts.get("PENDING", 0) + status_counts.get("PROCESSING", 0)
            )

            # Calculate oldest pending job age
            oldest_row = conn.execute(
                "SELECT created_at FROM jobs WHERE interview_id = ? AND status = 'PENDING' ORDER BY created_at ASC LIMIT 1",
                (interview_id,),
            ).fetchone()
            oldest_pending_age_sec: float | None = None
            if oldest_row and oldest_row["created_at"]:
                with contextlib.suppress(Exception):
                    c_dt = datetime.fromisoformat(oldest_row["created_at"])
                    oldest_pending_age_sec = max(0.0, round((now_dt - c_dt).total_seconds(), 2))

            # Fetch active or requested jobs
            if job_ids is not None:
                if not job_ids:
                    job_rows = []
                else:
                    placeholders = ",".join("?" for _ in job_ids)
                    job_rows = conn.execute(
                        f"""
                        SELECT id, type, status, attempts, max_attempts, payload_json, error_message,
                               locked_until, created_at, started_at, completed_at
                        FROM jobs
                        WHERE interview_id = ? AND id IN ({placeholders})
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (interview_id, *job_ids, limit),
                    ).fetchall()
            else:
                job_rows = conn.execute(
                    """
                    SELECT id, type, status, attempts, max_attempts, payload_json, error_message,
                           locked_until, created_at, started_at, completed_at
                    FROM jobs
                    WHERE interview_id = ? AND status IN ('PENDING', 'PROCESSING')
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (interview_id, limit),
                ).fetchall()

            active_jobs: list[dict[str, Any]] = []
            for r in job_rows:
                payload = {}
                if r["payload_json"]:
                    with contextlib.suppress(Exception):
                        payload = json.loads(r["payload_json"])

                q_id = payload.get("question_id")
                rubric_rev = payload.get("rubric_revision_id")
                transcript_rev = payload.get("transcript_revision_id")

                created_at_str = r["created_at"]
                started_at_str = r["started_at"]
                completed_at_str = r["completed_at"]

                queue_wait_sec: float | None = None
                if created_at_str:
                    with contextlib.suppress(Exception):
                        c_dt = datetime.fromisoformat(created_at_str)
                        ref_dt = datetime.fromisoformat(started_at_str) if started_at_str else now_dt
                        queue_wait_sec = max(0.0, round((ref_dt - c_dt).total_seconds(), 2))

                elapsed_sec: float | None = None
                if started_at_str:
                    with contextlib.suppress(Exception):
                        s_dt = datetime.fromisoformat(started_at_str)
                        end_dt = datetime.fromisoformat(completed_at_str) if completed_at_str else now_dt
                        elapsed_sec = max(0.0, round((end_dt - s_dt).total_seconds(), 2))

                active_jobs.append({
                    "id": r["id"],
                    "job_id": r["id"],
                    "type": r["type"],
                    "status": r["status"],
                    "attempts": r["attempts"],
                    "max_attempts": r["max_attempts"],
                    "created_at": created_at_str,
                    "started_at": started_at_str,
                    "completed_at": completed_at_str,
                    "locked_until": r["locked_until"],
                    "error_message": r["error_message"],
                    "question_id": q_id,
                    "rubric_revision_id": rubric_rev,
                    "transcript_revision_id": transcript_rev,
                    "queue_wait_sec": queue_wait_sec,
                    "elapsed_sec": elapsed_sec,
                })

            return {
                "interview_id": interview_id,
                "counts": status_counts,
                "counts_by_type": counts_by_type,
                "pending_or_processing": pending_or_processing,
                "is_pipeline_idle": pending_or_processing == 0,
                "oldest_pending_age_sec": oldest_pending_age_sec,
                "active_jobs": active_jobs,
            }

    # -------------------------------------------------------------
    # Audit Events
    # -------------------------------------------------------------
    def record_audit_event(
        self,
        event_id: str,
        interview_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (id, interview_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, interview_id, event_type, json.dumps(payload, ensure_ascii=False), now),
            )

    def get_audit_events(self, interview_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE interview_id = ? ORDER BY created_at ASC",
                (interview_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------------
    # Question Associations
    # -------------------------------------------------------------
    def save_association(
        self,
        assoc_id: str,
        interview_id: str,
        question_id: str,
        segment_id: str,
        confidence: float = 1.0,
        is_ambiguous: bool = False,
        is_manually_adjusted: bool = False,
        notes: str = "",
        revision_id: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            rev = revision_id
            if not rev:
                inv_row = conn.execute(
                    "SELECT active_transcript_revision_id FROM interviews WHERE id = ?",
                    (interview_id,),
                ).fetchone()
                active_rev = inv_row["active_transcript_revision_id"] if inv_row and inv_row["active_transcript_revision_id"] else "trans-rev-1"
                seg_row = conn.execute(
                    "SELECT revision_id FROM transcript_segments WHERE interview_id = ? AND id = ? AND revision_id = ?",
                    (interview_id, segment_id, active_rev),
                ).fetchone()
                if not seg_row:
                    seg_row = conn.execute(
                        "SELECT revision_id FROM transcript_segments WHERE interview_id = ? AND id = ?",
                        (interview_id, segment_id),
                    ).fetchone()
                rev = seg_row["revision_id"] if seg_row else active_rev

            existing_row = conn.execute(
                "SELECT id FROM question_associations WHERE interview_id = ? AND revision_id = ? AND segment_id = ?",
                (interview_id, rev, segment_id),
            ).fetchone()
            target_assoc_id = existing_row["id"] if existing_row else assoc_id

            conn.execute(
                """
                INSERT INTO question_associations (
                    id, interview_id, revision_id, question_id, segment_id, confidence,
                    is_ambiguous, is_manually_adjusted, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    revision_id = excluded.revision_id,
                    question_id = excluded.question_id,
                    confidence = excluded.confidence,
                    is_ambiguous = excluded.is_ambiguous,
                    is_manually_adjusted = excluded.is_manually_adjusted,
                    notes = excluded.notes
                """,
                (
                    target_assoc_id,
                    interview_id,
                    rev,
                    question_id,
                    segment_id,
                    confidence,
                    1 if is_ambiguous else 0,
                    1 if is_manually_adjusted else 0,
                    notes,
                    now,
                ),
            )

    def get_associations(self, interview_id: str, revision_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if revision_id is None:
                inv = conn.execute("SELECT active_transcript_revision_id FROM interviews WHERE id = ?", (interview_id,)).fetchone()
                rev = inv["active_transcript_revision_id"] if inv and inv["active_transcript_revision_id"] else "trans-rev-1"
            else:
                rev = revision_id
            rows = conn.execute(
                "SELECT * FROM question_associations WHERE interview_id = ? AND revision_id = ? ORDER BY created_at ASC",
                (interview_id, rev),
            ).fetchall()
            return [dict(r) for r in rows]

    def reassociate_segment(
        self,
        interview_id: str,
        segment_id: str,
        new_question_id: str,
        notes: str = "Manually re-linked by reviewer",
        expected_revision_id: str | None = None,
        revision_id: str | None = None,
    ) -> None:
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status, active_transcript_revision_id FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Interview {interview_id} not found or deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            active_rev = inv["active_transcript_revision_id"] or "trans-rev-1"
            if expected_revision_id and expected_revision_id != active_rev:
                raise RepositoryConflictError(
                    f"Revision conflict: active transcript revision is '{active_rev}', but expected was '{expected_revision_id}'"
                )

            target_rev = revision_id or active_rev
            seg = conn.execute(
                "SELECT id FROM transcript_segments WHERE interview_id = ? AND revision_id = ? AND id = ?",
                (interview_id, target_rev, segment_id),
            ).fetchone()
            if not seg:
                raise ValueError(f"Segment {segment_id} not found in revision {target_rev}")

            row = conn.execute(
                "SELECT id FROM question_associations WHERE interview_id = ? AND revision_id = ? AND segment_id = ?",
                (interview_id, target_rev, segment_id),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE question_associations
                    SET question_id = ?, is_ambiguous = 0, is_manually_adjusted = 1, notes = ?
                    WHERE id = ?
                    """,
                    (new_question_id, notes, row["id"]),
                )
            else:
                assoc_id = f"assoc-{uuid.uuid4().hex[:8]}"
                self.save_association(
                    assoc_id=assoc_id,
                    interview_id=interview_id,
                    question_id=new_question_id,
                    segment_id=segment_id,
                    confidence=1.0,
                    is_ambiguous=False,
                    is_manually_adjusted=True,
                    notes=notes,
                    revision_id=target_rev,
                )


    def save_audio_chunk(
        self,
        interview_id: str,
        track_id: str,
        capture_epoch: int,
        sequence: int,
        start_time_ms: int,
        end_time_ms: int,
        sample_rate: int,
        channels: int,
        sample_count: int,
        format_str: str,
        checksum_sha256: str,
        payload_bytes: bytes,
        spool_base_dir: Path | str,
    ) -> tuple[str, bool]:
        """
        Durable and idempotent storage of an audio chunk:
        1. Verifies interview status (rejects finalized with 409).
        2. Idempotent check: same key + same checksum -> idempotent_duplicate.
        3. Conflict check: same key + different checksum -> RepositoryConflictError.
        4. Atomic write to spool directory: <spool_base_dir>/<interview_id>/<track_id>/<seq>.chunk + .meta.json.
        5. Persists metadata record in audio_chunks table.
        6. Enqueues idempotent TRANSCRIBE_AUDIO job for the pipeline worker.
        """
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv:
                raise RepositoryNotFoundError(f"Interview {interview_id} not found")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            existing = conn.execute(
                """
                SELECT checksum_sha256 FROM audio_chunks
                WHERE interview_id = ? AND track_id = ? AND capture_epoch = ? AND sequence = ?
                """,
                (interview_id, track_id, capture_epoch, sequence),
            ).fetchone()

            if existing:
                if existing["checksum_sha256"] == checksum_sha256:
                    return ("idempotent_duplicate", False)
                raise RepositoryConflictError(
                    f"Conflict for chunk ({interview_id}, {track_id}, epoch={capture_epoch}, seq={sequence}): "
                    f"existing hash {existing['checksum_sha256']} differs from incoming {checksum_sha256}"
                )

            # Persist to disk atomically
            track_dir = Path(spool_base_dir) / interview_id / track_id
            track_dir.mkdir(parents=True, exist_ok=True)

            chunk_filename = f"{sequence:08d}.chunk"
            tmp_filename = f"{sequence:08d}.tmp"
            meta_filename = f"{sequence:08d}.meta.json"

            chunk_path = track_dir / chunk_filename
            tmp_path = track_dir / tmp_filename
            meta_path = track_dir / meta_filename

            tmp_path.write_bytes(payload_bytes)
            tmp_path.replace(chunk_path)

            meta_dict = {
                "interview_id": interview_id,
                "track_id": track_id,
                "capture_epoch": capture_epoch,
                "sequence": sequence,
                "start_time_ms": start_time_ms,
                "end_time_ms": end_time_ms,
                "sample_rate": sample_rate,
                "channels": channels,
                "sample_count": sample_count,
                "format": format_str,
                "checksum_sha256": checksum_sha256,
                "size_bytes": len(payload_bytes),
            }
            meta_path.write_text(json.dumps(meta_dict, indent=2, ensure_ascii=False), encoding="utf-8")

            conn.execute(
                """
                INSERT INTO audio_chunks (
                    interview_id, track_id, capture_epoch, sequence,
                    start_time_ms, end_time_ms, sample_rate, channels,
                    sample_count, format, checksum_sha256, size_bytes,
                    file_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interview_id,
                    track_id,
                    capture_epoch,
                    sequence,
                    start_time_ms,
                    end_time_ms,
                    sample_rate,
                    channels,
                    sample_count,
                    format_str,
                    checksum_sha256,
                    len(payload_bytes),
                    str(chunk_path),
                    now,
                ),
            )

            # Enqueue STT job idempotently
            job_id = f"stt-{interview_id}-{track_id}-{capture_epoch}-{sequence}"
            segment_id = f"seg-{interview_id}-{track_id}-{capture_epoch}-{sequence}"
            stt_payload = {
                "audio_hex": payload_bytes.hex(),
                "track_id": track_id,
                "capture_epoch": capture_epoch,
                "epoch": capture_epoch,
                "sequence": sequence,
                "start_ms": start_time_ms,
                "end_ms": end_time_ms,
                "sample_rate": sample_rate,
                "channels": channels,
                "segment_id": segment_id,
                "file_path": str(chunk_path),
            }

            conn.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    id, type, interview_id, payload_json, status, created_at, updated_at
                ) VALUES (?, 'TRANSCRIBE_AUDIO', ?, ?, 'PENDING', ?, ?)
                """,
                (job_id, interview_id, json.dumps(stt_payload, ensure_ascii=False), now, now),
            )

            return ("persisted", True)

    def get_audio_chunks(self, interview_id: str, track_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if track_id:
                rows = conn.execute(
                    "SELECT * FROM audio_chunks WHERE interview_id = ? AND track_id = ? ORDER BY sequence ASC",
                    (interview_id, track_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audio_chunks WHERE interview_id = ? ORDER BY track_id ASC, sequence ASC",
                    (interview_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------------
    # Speech Turn Assembly & Cursor
    # -------------------------------------------------------------
    def get_turn_assembly_cursor(
        self, interview_id: str, track_id: str, capture_epoch: int
    ) -> tuple[int, int]:
        """Returns (next_sequence, next_sample_offset) for the given track and epoch."""
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT next_sequence, next_sample_offset
                FROM transcript_assembly_state
                WHERE interview_id = ? AND track_id = ? AND capture_epoch = ?
                """,
                (interview_id, track_id, capture_epoch),
            ).fetchone()
            if row:
                return (row["next_sequence"], row["next_sample_offset"])
            return (0, 0)

    def get_audio_chunks_for_assembly(
        self,
        interview_id: str,
        track_id: str,
        capture_epoch: int,
        from_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        """Returns ordered audio chunks for speech turn assembly starting from sequence."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM audio_chunks
                WHERE interview_id = ? AND track_id = ? AND capture_epoch = ? AND sequence >= ?
                ORDER BY sequence ASC
                """,
                (interview_id, track_id, capture_epoch, from_sequence),
            ).fetchall()
            return [dict(r) for r in rows]

    def commit_turn_assembly_results(
        self,
        interview_id: str,
        track_id: str,
        capture_epoch: int,
        next_sequence: int,
        next_sample_offset: int,
        turns: list[AssembledTurn],
        base_time: datetime | None = None,
        expected_sequence: int | None = None,
        expected_sample_offset: int | None = None,
    ) -> list[str]:
        """
        Atomically commits updated assembly cursor and enqueues idempotent TRANSCRIBE_TURN jobs
        using CAS on expected cursor and strictly preventing cursor regression.
        """
        now = utc_now_iso()
        created_job_ids: list[str] = []

        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv:
                raise RepositoryNotFoundError(f"Interview {interview_id} not found")
            if inv["status"] in ("finalized", "deleted"):
                return []

            # 1. CAS & anti-regression check
            curr_row = conn.execute(
                """
                SELECT next_sequence, next_sample_offset FROM transcript_assembly_state
                WHERE interview_id = ? AND track_id = ? AND capture_epoch = ?
                """,
                (interview_id, track_id, capture_epoch),
            ).fetchone()

            if curr_row is not None:
                curr_seq = curr_row["next_sequence"]
                curr_off = curr_row["next_sample_offset"]

                # If caller specified expected cursor, enforce strict CAS match
                if (
                    expected_sequence is not None
                    and expected_sample_offset is not None
                    and (curr_seq != expected_sequence or curr_off != expected_sample_offset)
                ):
                    logger.warning(
                        "CAS mismatch in commit_turn_assembly_results for %s track %s epoch %d: "
                        "expected (%d, %d), found (%d, %d). Rejecting concurrent commit.",
                        interview_id, track_id, capture_epoch,
                        expected_sequence, expected_sample_offset, curr_seq, curr_off,
                    )
                    return []

                # Prevent cursor from moving backwards in all cases
                if (next_sequence < curr_seq) or (next_sequence == curr_seq and next_sample_offset < curr_off):
                    logger.warning(
                        "Cursor regression rejected for %s track %s epoch %d: "
                        "current (%d, %d), attempted (%d, %d)",
                        interview_id, track_id, capture_epoch, curr_seq, curr_off, next_sequence, next_sample_offset,
                    )
                    return []

            # 2. Update or insert cursor state
            conn.execute(
                """
                INSERT INTO transcript_assembly_state (
                    interview_id, track_id, capture_epoch, next_sequence, next_sample_offset, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(interview_id, track_id, capture_epoch)
                DO UPDATE SET
                    next_sequence = excluded.next_sequence,
                    next_sample_offset = excluded.next_sample_offset,
                    updated_at = excluded.updated_at
                """,
                (interview_id, track_id, capture_epoch, next_sequence, next_sample_offset, now),
            )

            # 3. Insert TRANSCRIBE_TURN jobs idempotently sorted chronologically
            turns_sorted = sorted(turns, key=lambda t: (t.start_ms, t.first_sequence))
            now_dt = base_time or datetime.now(UTC)
            for idx, turn in enumerate(turns_sorted):
                turn_now = (now_dt + timedelta(microseconds=idx * 1000)).isoformat()
                job_id = (
                    f"stt-turn-{interview_id}-{track_id}-{capture_epoch}-"
                    f"{turn.first_sequence}-{turn.first_sample_offset}-"
                    f"{turn.last_sequence}-{turn.last_sample_offset}"
                )
                segment_id = (
                    f"seg-{interview_id}-{track_id}-{capture_epoch}-"
                    f"{turn.first_sequence}-{turn.first_sample_offset}-"
                    f"{turn.last_sequence}-{turn.last_sample_offset}"
                )
                payload = {
                    "track_id": track_id,
                    "capture_epoch": capture_epoch,
                    "first_sequence": turn.first_sequence,
                    "first_sample_offset": turn.first_sample_offset,
                    "last_sequence": turn.last_sequence,
                    "last_sample_offset": turn.last_sample_offset,
                    "start_ms": turn.start_ms,
                    "end_ms": turn.end_ms,
                    "sample_rate": turn.sample_rate,
                    "channels": turn.channels,
                    "language": turn.language,
                    "segment_id": segment_id,
                }
                cursor_res = conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs (
                        id, type, interview_id, payload_json, status, created_at, updated_at
                    ) VALUES (?, 'TRANSCRIBE_TURN', ?, ?, 'PENDING', ?, ?)
                    """,
                    (job_id, interview_id, json.dumps(payload, ensure_ascii=False), turn_now, turn_now),
                )
                if cursor_res.rowcount > 0:
                    created_job_ids.append(job_id)

        return created_job_ids

    def get_turn_audio_pcm(
        self,
        interview_id: str,
        track_id: str,
        capture_epoch: int,
        first_sequence: int,
        first_sample_offset: int,
        last_sequence: int,
        last_sample_offset: int,
    ) -> bytes:
        """
        Reconstructs the exact PCM bytes for an assembled turn from stored audio chunks.
        Verifies all sequences, checksums, formats, and offsets.
        """
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM audio_chunks
                WHERE interview_id = ? AND track_id = ? AND capture_epoch = ?
                  AND sequence BETWEEN ? AND ?
                ORDER BY sequence ASC
                """,
                (interview_id, track_id, capture_epoch, first_sequence, last_sequence),
            ).fetchall()

        chunks = [dict(r) for r in rows]
        expected_seqs = list(range(first_sequence, last_sequence + 1))
        actual_seqs = [c["sequence"] for c in chunks]
        if actual_seqs != expected_seqs:
            missing = set(expected_seqs) - set(actual_seqs)
            raise ValueError(
                f"Missing audio chunk sequences for turn in interview {interview_id} "
                f"(track {track_id}, epoch {capture_epoch}): missing {sorted(missing)}"
            )

        pcm_parts: list[bytes] = []
        for c in chunks:
            fp = Path(c["file_path"])
            if not fp.exists():
                raise FileNotFoundError(f"Audio chunk file not found at {fp}")
            raw = fp.read_bytes()

            # Verify checksum
            computed_sha = hashlib.sha256(raw).hexdigest()
            if computed_sha != c["checksum_sha256"]:
                raise ValueError(
                    f"Checksum mismatch for chunk seq {c['sequence']}: "
                    f"expected {c['checksum_sha256']}, got {computed_sha}"
                )

            # Strip RIFF header if present
            raw_pcm = raw[44:] if raw.startswith(b"RIFF") and len(raw) >= 44 else raw

            seq = c["sequence"]
            start_samp = first_sample_offset if seq == first_sequence else 0
            end_samp = last_sample_offset if seq == last_sequence else (len(raw_pcm) // 2)

            start_byte = start_samp * 2
            end_byte = end_samp * 2
            pcm_parts.append(raw_pcm[start_byte:end_byte])

        return b"".join(pcm_parts)

    def flush_turn_assembly(
        self,
        interview_id: str,
        track_id: str | None = None,
        capture_epoch: int | None = None,
    ) -> list[str]:
        """
        Forces turn assembler to flush any open speech tail for the given interview, track, and epoch.
        Returns newly enqueued TRANSCRIBE_TURN job IDs.
        """
        assembler = TurnAssembler()
        created_jobs: list[str] = []

        with self.db.transaction() as conn:
            query = """
                SELECT track_id, capture_epoch, MIN(start_time_ms) as min_start
                FROM audio_chunks
                WHERE interview_id = ?
            """
            params: list[Any] = [interview_id]
            if track_id:
                query += " AND track_id = ?"
                params.append(track_id)
            if capture_epoch is not None:
                query += " AND capture_epoch = ?"
                params.append(capture_epoch)
            query += " GROUP BY track_id, capture_epoch ORDER BY min_start ASC"
            rows = conn.execute(query, params).fetchall()

        base_time = datetime.now(UTC)
        for track_idx, r in enumerate(rows):
            t_id = r["track_id"]
            ep = r["capture_epoch"]
            cursor_seq, cursor_off = self.get_turn_assembly_cursor(interview_id, t_id, ep)
            chunk_records = self.get_audio_chunks_for_assembly(interview_id, t_id, ep, from_sequence=cursor_seq)
            if not chunk_records:
                continue

            chunk_refs: list[AudioChunkRef] = []
            for cr in chunk_records:
                fp_str = cr.get("file_path")
                if fp_str and Path(fp_str).exists():
                    raw = Path(fp_str).read_bytes()
                    pcm = raw[44:] if raw.startswith(b"RIFF") and len(raw) >= 44 else raw
                    chunk_refs.append(
                        AudioChunkRef(
                            sequence=cr["sequence"],
                            start_time_ms=cr["start_time_ms"],
                            end_time_ms=cr["end_time_ms"],
                            sample_rate=cr.get("sample_rate", 16000),
                            channels=cr.get("channels", 1),
                            format=cr.get("format", "pcm_s16le"),
                            pcm_bytes=pcm,
                        )
                    )

            if not chunk_refs:
                continue

            out = assembler.assemble(
                chunks=chunk_refs,
                cursor=TurnAssemblyCursor(cursor_seq, cursor_off),
                is_flush=True,
                track_id=t_id,
                capture_epoch=ep,
            )

            if out.turns or out.next_sequence != cursor_seq or out.next_sample_offset != cursor_off:
                new_jobs = self.commit_turn_assembly_results(
                    interview_id=interview_id,
                    track_id=t_id,
                    capture_epoch=ep,
                    next_sequence=out.next_sequence,
                    next_sample_offset=out.next_sample_offset,
                    turns=out.turns,
                    base_time=base_time + timedelta(milliseconds=track_idx * 100),
                    expected_sequence=cursor_seq,
                    expected_sample_offset=cursor_off,
                )
                created_jobs.extend(new_jobs)

        return created_jobs

    # -------------------------------------------------------------
    # Job Templates
    # -------------------------------------------------------------
    def create_job_template(
        self,
        template_id: str,
        title: str,
        role: str,
        level: str = "Middle",
        description: str = "",
        questions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        questions_payload = questions or []
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO job_templates (
                    id, title, role, level, description, questions_json, version, is_archived, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                """,
                (
                    template_id,
                    title,
                    role,
                    level,
                    description,
                    json.dumps(questions_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_job_template(template_id)  # type: ignore[return-value]

    def get_job_template(self, template_id: str) -> dict[str, Any] | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM job_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            data["questions"] = json.loads(data["questions_json"])
            return data

    def list_job_templates(self, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if include_archived:
                rows = conn.execute(
                    "SELECT * FROM job_templates ORDER BY is_archived ASC, updated_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM job_templates WHERE is_archived = 0 ORDER BY updated_at DESC"
                ).fetchall()
            res = []
            for r in rows:
                item = dict(r)
                item["questions"] = json.loads(item["questions_json"])
                res.append(item)
            return res

    def update_job_template(
        self,
        template_id: str,
        title: str | None = None,
        role: str | None = None,
        level: str | None = None,
        description: str | None = None,
        questions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM job_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if not current:
                raise KeyError(f"Job template {template_id} not found")

            new_title = title if title is not None else current["title"]
            new_role = role if role is not None else current["role"]
            new_level = level if level is not None else current["level"]
            new_description = description if description is not None else current["description"]
            new_questions_json = json.dumps(questions, ensure_ascii=False) if questions is not None else current["questions_json"]
            new_version = current["version"] + 1

            conn.execute(
                """
                UPDATE job_templates
                SET title = ?, role = ?, level = ?, description = ?, questions_json = ?, version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_title,
                    new_role,
                    new_level,
                    new_description,
                    new_questions_json,
                    new_version,
                    now,
                    template_id,
                ),
            )
        return self.get_job_template(template_id)  # type: ignore[return-value]

    def duplicate_job_template(
        self,
        template_id: str,
        new_id: str | None = None,
        title_suffix: str = " (Копия)",
    ) -> dict[str, Any]:
        source = self.get_job_template(template_id)
        if not source:
            raise KeyError(f"Job template {template_id} not found")

        gen_id = new_id or f"tpl-{uuid.uuid4().hex[:8]}"
        new_title = f"{source['title']}{title_suffix}"
        return self.create_job_template(
            template_id=gen_id,
            title=new_title,
            role=source["role"],
            level=source.get("level", "Middle"),
            description=source.get("description", ""),
            questions=source.get("questions", []),
        )

    def archive_job_template(self, template_id: str, is_archived: bool = True) -> dict[str, Any]:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            current = conn.execute(
                "SELECT id FROM job_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if not current:
                raise KeyError(f"Job template {template_id} not found")

            conn.execute(
                "UPDATE job_templates SET is_archived = ?, updated_at = ? WHERE id = ?",
                (1 if is_archived else 0, now, template_id),
            )
        return self.get_job_template(template_id)  # type: ignore[return-value]

    def delete_job_template(self, template_id: str) -> bool:
        with self.db.transaction() as conn:
            ref_count = conn.execute(
                "SELECT COUNT(*) FROM interviews WHERE template_id = ?", (template_id,)
            ).fetchone()[0]
            if ref_count > 0:
                raise ValueError(
                    f"Нельзя удалить должность {template_id}: на неё ссылаются существующие интервью ({ref_count}). Используйте архивирование."
                )

            deleted = conn.execute(
                "DELETE FROM job_templates WHERE id = ?", (template_id,)
            ).rowcount
            return deleted > 0

    def copy_question_to_template(
        self,
        source_template_id: str,
        question_id: str,
        target_template_id: str,
    ) -> dict[str, Any]:
        source = self.get_job_template(source_template_id)
        if not source:
            raise KeyError(f"Source template {source_template_id} not found")
        target = self.get_job_template(target_template_id)
        if not target:
            raise KeyError(f"Target template {target_template_id} not found")

        matching_q = None
        for q in source.get("questions", []):
            if q.get("id") == question_id:
                matching_q = copy.deepcopy(q)
                break
        if not matching_q:
            raise KeyError(f"Question {question_id} not found in template {source_template_id}")

        target_questions = target.get("questions", [])
        target_q_ids = {q.get("id") for q in target_questions}
        if matching_q["id"] in target_q_ids:
            matching_q["id"] = f"{matching_q['id']}-copy-{uuid.uuid4().hex[:4]}"
        matching_q["order_index"] = len(target_questions)
        target_questions.append(matching_q)

        return self.update_job_template(
            template_id=target_template_id,
            questions=target_questions,
        )

    # -------------------------------------------------------------
    # Adaptive Follow-up and Guiding Questions
    # -------------------------------------------------------------

    def create_followup_request_and_job(
        self,
        interview_id: str,
        question_id: str,
        mode: str,
        trigger: str,
        candidate_fingerprint: str | None,
        context_hash: str,
        context_json: str,
        rubric_revision_id: str,
        transcript_revision_id: str,
        cooldown_sec: int = 30,
    ) -> tuple[dict[str, Any] | None, bool, str | None, float | None]:
        """
        Atomically creates followup_requests record and enqueues GENERATE_FOLLOWUPS job.
        Returns: (request_dict, is_new, wait_reason, cooldown_remaining_sec)
        """
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv_row = conn.execute("SELECT status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv_row:
                raise KeyError(f"Interview {interview_id} not found")

            inv_status = str(inv_row["status"]).lower()
            if inv_status in ("processing", "review", "finalized", "deleted"):
                raise RepositoryConflictError(f"Cannot generate followups in interview status '{inv_status}'")

            if trigger == "auto" and inv_status != "recording":
                return None, False, "not_recording", None

            if trigger == "manual" and inv_status not in ("recording", "paused"):
                raise RepositoryConflictError(f"Cannot generate followups in interview status '{inv_status}'")

            # Check if identical request exists
            existing_req = conn.execute(
                """
                SELECT * FROM followup_requests
                WHERE interview_id = ? AND question_id = ? AND mode = ? AND context_hash = ?
                """,
                (interview_id, question_id, mode, context_hash),
            ).fetchone()
            if existing_req:
                req_dict = dict(existing_req)
                return req_dict, False, None, None

            # Check if an active follow-up job is already in flight for this interview
            in_flight = conn.execute(
                """
                SELECT j.id, fr.id as req_id, fr.question_id, fr.mode
                FROM jobs j
                JOIN followup_requests fr ON j.id = fr.job_id
                WHERE j.interview_id = ? AND j.type = 'GENERATE_FOLLOWUPS'
                  AND j.status IN ('PENDING', 'PROCESSING')
                LIMIT 1
                """,
                (interview_id,),
            ).fetchone()
            if in_flight:
                active_req = conn.execute(
                    "SELECT * FROM followup_requests WHERE id = ?", (in_flight["req_id"],)
                ).fetchone()
                return dict(active_req) if active_req else None, False, "generating", None

            # Check auto cooldown
            if trigger == "auto":
                last_auto = conn.execute(
                    """
                    SELECT created_at FROM followup_requests
                    WHERE interview_id = ? AND trigger = 'auto'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (interview_id,),
                ).fetchone()
                if last_auto:
                    try:
                        last_dt = datetime.fromisoformat(last_auto["created_at"])
                        cur_dt = datetime.now(UTC)
                        elapsed = (cur_dt - last_dt).total_seconds()
                        if elapsed < cooldown_sec:
                            return None, False, "cooldown_active", round(cooldown_sec - elapsed, 1)
                    except Exception as e:
                        logger.debug("Failed to calculate cooldown diff: %s", e)

            # Insert new request and job (jobs first to satisfy FOREIGN KEY constraint)
            request_id = f"freq-{uuid.uuid4().hex[:12]}"
            job_id = f"job-fu-{uuid.uuid4().hex[:12]}"

            job_payload = {
                "request_id": request_id,
                "interview_id": interview_id,
                "question_id": question_id,
                "mode": mode,
                "context_hash": context_hash,
            }
            conn.execute(
                """
                INSERT INTO jobs (
                    id, type, interview_id, payload_json, status, attempts, max_attempts, created_at, updated_at
                ) VALUES (?, 'GENERATE_FOLLOWUPS', ?, ?, 'PENDING', 0, 2, ?, ?)
                """,
                (job_id, interview_id, json.dumps(job_payload, ensure_ascii=False), now, now),
            )

            conn.execute(
                """
                INSERT INTO followup_requests (
                    id, interview_id, question_id, rubric_revision_id, transcript_revision_id,
                    mode, trigger, candidate_fingerprint, context_hash, context_json,
                    job_id, outcome, prompt_version, schema_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'v1', 'v1', ?)
                """,
                (
                    request_id,
                    interview_id,
                    question_id,
                    rubric_revision_id,
                    transcript_revision_id,
                    mode,
                    trigger,
                    candidate_fingerprint,
                    context_hash,
                    context_json,
                    job_id,
                    now,
                ),
            )

            new_req = conn.execute(
                "SELECT * FROM followup_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(new_req), True, None, None

    def get_followup_state(
        self,
        interview_id: str,
        question_id: str,
        mode: str = "probe",
    ) -> dict[str, Any]:
        """
        Reads follow-up state for a question: latest request, suggestions, staleness, and history.
        """
        with self.db.transaction() as conn:
            inv = conn.execute(
                "SELECT status, active_rubric_revision_id, active_transcript_revision_id FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
            if not inv:
                raise KeyError(f"Interview {interview_id} not found")

            active_rubric = inv["active_rubric_revision_id"] or "rub-rev-1"
            active_trans = inv["active_transcript_revision_id"] or "trans-rev-1"
            inv_status = str(inv["status"]).lower()

            # Latest request for this question and mode
            req_row = conn.execute(
                """
                SELECT * FROM followup_requests
                WHERE interview_id = ? AND question_id = ? AND mode = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (interview_id, question_id, mode),
            ).fetchone()

            latest_req: dict[str, Any] | None = dict(req_row) if req_row else None
            suggestions: list[dict[str, Any]] = []
            has_new_answer = False

            if latest_req:
                # Add job info
                if latest_req.get("job_id"):
                    job_row = conn.execute(
                        "SELECT status, attempts, max_attempts, error_message FROM jobs WHERE id = ?",
                        (latest_req["job_id"],),
                    ).fetchone()
                    if job_row:
                        latest_req["job"] = dict(job_row)

                # Fetch suggestions
                sug_rows = conn.execute(
                    """
                    SELECT * FROM followup_suggestions
                    WHERE request_id = ?
                    ORDER BY ordinal ASC
                    """,
                    (latest_req["id"],),
                ).fetchall()

                # Check staleness
                is_stale = False
                stale_reason = None
                if latest_req["rubric_revision_id"] != active_rubric:
                    is_stale = True
                    stale_reason = "План интервью был обновлён"
                elif latest_req["transcript_revision_id"] != active_trans:
                    is_stale = True
                    stale_reason = "Стенограмма перетранскрибирована"

                for sr in sug_rows:
                    s_dict = dict(sr)
                    s_dict["criterion_ids"] = json.loads(s_dict.get("criterion_ids_json") or "[]")
                    s_dict["source_refs"] = json.loads(s_dict.get("source_refs_json") or "[]")

                    # Check segment content integrity for staleness if revisions matched
                    item_stale = is_stale
                    item_stale_reason = stale_reason
                    if not item_stale:
                        for ref in s_dict["source_refs"]:
                            seg_id = ref.get("segment_id")
                            seg_row = conn.execute(
                                """
                                SELECT text, speaker_role, track_id FROM transcript_segments
                                WHERE interview_id = ? AND revision_id = ? AND id = ?
                                """,
                                (interview_id, active_trans, seg_id),
                            ).fetchone()
                            if not seg_row:
                                item_stale = True
                                item_stale_reason = "Исходная реплика удалена"
                                break
                            # Check role
                            role = (seg_row["speaker_role"] or "unknown").lower()
                            track = str(seg_row["track_id"]).lower()
                            is_cand = (role != "interviewer") if track == "candidate" else (role == "candidate")
                            if not is_cand:
                                item_stale = True
                                item_stale_reason = "Роль спикера была изменена"
                                break

                    s_dict["is_stale"] = item_stale
                    s_dict["stale_reason"] = item_stale_reason

                    # Check if there are new candidate segments after the snapshot
                    s_dict["has_new_answer"] = False
                    suggestions.append(s_dict)

                # Check has_new_answer globally for this question
                new_segs = conn.execute(
                    """
                    SELECT COUNT(*) FROM transcript_segments
                    WHERE interview_id = ? AND revision_id = ? AND is_final = 1 AND created_at > ?
                    """,
                    (interview_id, active_trans, latest_req["created_at"]),
                ).fetchone()[0]
                if new_segs > 0:
                    has_new_answer = True
                    for s in suggestions:
                        s["has_new_answer"] = True

            # Fetch history of asked/dismissed suggestions across all requests for this question
            history_rows = conn.execute(
                """
                SELECT * FROM followup_suggestions
                WHERE interview_id = ? AND question_id = ? AND status IN ('asked', 'dismissed')
                ORDER BY decided_at ASC, created_at ASC
                """,
                (interview_id, question_id),
            ).fetchall()
            history: list[dict[str, Any]] = []
            for hr in history_rows:
                h_dict = dict(hr)
                h_dict["criterion_ids"] = json.loads(h_dict.get("criterion_ids_json") or "[]")
                h_dict["source_refs"] = json.loads(h_dict.get("source_refs_json") or "[]")
                history.append(h_dict)

            # Check cooldown
            cooldown_remaining: float | None = None
            last_auto = conn.execute(
                """
                SELECT created_at FROM followup_requests
                WHERE interview_id = ? AND trigger = 'auto'
                ORDER BY created_at DESC LIMIT 1
                """,
                (interview_id,),
            ).fetchone()
            if last_auto:
                try:
                    last_dt = datetime.fromisoformat(last_auto["created_at"])
                    elapsed = (datetime.now(UTC) - last_dt).total_seconds()
                    if elapsed < 30:
                        cooldown_remaining = round(30 - elapsed, 1)
                except Exception:
                    pass

            # Check in-flight job
            is_generating = False
            in_flight = conn.execute(
                """
                SELECT id FROM jobs
                WHERE interview_id = ? AND type = 'GENERATE_FOLLOWUPS'
                  AND status IN ('PENDING', 'PROCESSING')
                LIMIT 1
                """,
                (interview_id,),
            ).fetchone()
            if in_flight:
                is_generating = True

            can_generate = (
                inv_status in ("recording", "paused")
                and not is_generating
            )

            wait_reason = None
            if is_generating:
                wait_reason = "generating"
            elif cooldown_remaining is not None and cooldown_remaining > 0:
                wait_reason = "cooldown_active"

            return {
                "interview_id": interview_id,
                "question_id": question_id,
                "mode": mode,
                "active_rubric_revision_id": active_rubric,
                "active_transcript_revision_id": active_trans,
                "candidate_fingerprint": latest_req.get("candidate_fingerprint") if latest_req else None,
                "context_hash": latest_req.get("context_hash") if latest_req else None,
                "can_generate": can_generate,
                "wait_reason": wait_reason,
                "cooldown_remaining_sec": cooldown_remaining,
                "latest_request": latest_req,
                "suggestions": suggestions,
                "has_new_answer": has_new_answer,
                "history": history,
            }

    def save_followup_suggestions(
        self,
        job_id: str,
        request_id: str,
        owner_token: str,
        suggestions: list[dict[str, Any]],
        outcome: str,
        model_profile_id: str | None = None,
        provider_id: str | None = None,
        usage_tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """
        Atomically saves validated suggestions and updates request outcome, guarded by owner_token.
        """
        now = utc_now_iso()
        with self.db.transaction() as conn:
            job_row = conn.execute(
                "SELECT locked_by, interview_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not job_row or job_row["locked_by"] != owner_token:
                raise RepositoryConflictError(f"Job {job_id} lease expired or owner token mismatch")

            req_row = conn.execute(
                "SELECT * FROM followup_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if not req_row:
                raise KeyError(f"Followup request {request_id} not found")

            interview_id = req_row["interview_id"]
            question_id = req_row["question_id"]
            rubric_rev = req_row["rubric_revision_id"]
            trans_rev = req_row["transcript_revision_id"]

            # Save suggestions with stable IDs
            for idx, sug in enumerate(suggestions):
                sug_id = f"{request_id}-{idx}"
                existing_sug = conn.execute(
                    "SELECT status, asked_text, decided_at, decision_version FROM followup_suggestions WHERE id = ?",
                    (sug_id,),
                ).fetchone()

                crit_json = json.dumps(sug.get("criterion_ids") or [], ensure_ascii=False)
                source_json = json.dumps(
                    [
                        ref.model_dump() if hasattr(ref, "model_dump") else ref
                        for ref in (sug.get("source_refs") or [])
                    ],
                    ensure_ascii=False,
                )

                if existing_sug:
                    # Preserve human decisions
                    conn.execute(
                        """
                        UPDATE followup_suggestions
                        SET kind = ?, question_text = ?, purpose = ?, criterion_ids_json = ?, source_refs_json = ?
                        WHERE id = ?
                        """,
                        (
                            sug["kind"],
                            sug["question_text"].strip(),
                            sug["purpose"].strip(),
                            crit_json,
                            source_json,
                            sug_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO followup_suggestions (
                            id, request_id, interview_id, question_id, rubric_revision_id, transcript_revision_id,
                            kind, question_text, purpose, criterion_ids_json, source_refs_json,
                            status, ordinal, decision_version, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'suggested', ?, 1, ?)
                        """,
                        (
                            sug_id,
                            request_id,
                            interview_id,
                            question_id,
                            rubric_rev,
                            trans_rev,
                            sug["kind"],
                            sug["question_text"].strip(),
                            sug["purpose"].strip(),
                            crit_json,
                            source_json,
                            idx,
                            now,
                        ),
                    )

            conn.execute(
                """
                UPDATE followup_requests
                SET outcome = ?, model_profile_id = ?, provider_id = ?, usage_tokens = ?, latency_ms = ?, completed_at = ?
                WHERE id = ?
                """,
                (outcome, model_profile_id, provider_id, usage_tokens, latency_ms, now, request_id),
            )

    def update_suggestion_decision(
        self,
        interview_id: str,
        suggestion_id: str,
        status: str,
        asked_text: str | None,
        expected_decision_version: int,
        expected_rubric_revision_id: str | None = None,
        expected_transcript_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Updates interviewer decision ('suggested' | 'asked' | 'dismissed') on a card with OCC.
        """
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute(
                "SELECT status, active_rubric_revision_id, active_transcript_revision_id FROM interviews WHERE id = ?",
                (interview_id,),
            ).fetchone()
            if not inv:
                raise KeyError(f"Interview {interview_id} not found")

            if str(inv["status"]).lower() == "finalized":
                raise RepositoryConflictError("Interview is finalized and immutable")

            sug_row = conn.execute(
                "SELECT * FROM followup_suggestions WHERE id = ? AND interview_id = ?",
                (suggestion_id, interview_id),
            ).fetchone()
            if not sug_row:
                raise KeyError(f"Suggestion {suggestion_id} not found")

            # Check expected revisions if provided
            if expected_rubric_revision_id and inv["active_rubric_revision_id"] != expected_rubric_revision_id:
                raise RepositoryConflictError("Active rubric revision does not match expected revision")
            if expected_transcript_revision_id and inv["active_transcript_revision_id"] != expected_transcript_revision_id:
                raise RepositoryConflictError("Active transcript revision does not match expected revision")

            # Check OCC version
            current_version = sug_row["decision_version"]
            current_status = sug_row["status"]
            current_asked_text = sug_row["asked_text"] or ""
            target_asked_text = (asked_text or "").strip()

            if current_version != expected_decision_version:
                # Check idempotency
                if current_status == status and current_asked_text == target_asked_text:
                    res = dict(sug_row)
                    res["criterion_ids"] = json.loads(res.get("criterion_ids_json") or "[]")
                    res["source_refs"] = json.loads(res.get("source_refs_json") or "[]")
                    return res
                raise RepositoryConflictError(
                    f"Decision version mismatch for suggestion {suggestion_id}: expected {expected_decision_version}, got {current_version}"
                )

            # Do not permit marking stale suggestion as asked
            if status == "asked" and (
                sug_row["rubric_revision_id"] != inv["active_rubric_revision_id"]
                or sug_row["transcript_revision_id"] != inv["active_transcript_revision_id"]
            ):
                raise RepositoryConflictError("Cannot mark stale suggestion as asked")

            new_version = current_version + 1
            final_asked = target_asked_text if target_asked_text else None
            decided_at = now if status in ("asked", "dismissed") else None

            conn.execute(
                """
                UPDATE followup_suggestions
                SET status = ?, asked_text = ?, decided_at = ?, decision_version = ?
                WHERE id = ? AND decision_version = ?
                """,
                (status, final_asked, decided_at, new_version, suggestion_id, current_version),
            )

            # Record audit event
            event_id = f"audit-{uuid.uuid4().hex[:8]}"
            conn.execute(
                """
                INSERT INTO audit_events (id, interview_id, event_type, payload_json, created_at)
                VALUES (?, ?, 'FOLLOWUP_DECISION_UPDATED', ?, ?)
                """,
                (
                    event_id,
                    interview_id,
                    json.dumps(
                        {
                            "suggestion_id": suggestion_id,
                            "question_id": sug_row["question_id"],
                            "status": status,
                            "asked_text": final_asked,
                            "decision_version": new_version,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

            updated = conn.execute(
                "SELECT * FROM followup_suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
            res = dict(updated)
            res["criterion_ids"] = json.loads(res.get("criterion_ids_json") or "[]")
            res["source_refs"] = json.loads(res.get("source_refs_json") or "[]")
            return res

    def retry_followup_request(
        self,
        interview_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """
        Retries a failed follow-up request, resetting attempts and job status to PENDING.
        """
        now = utc_now_iso()
        with self.db.transaction() as conn:
            req_row = conn.execute(
                "SELECT * FROM followup_requests WHERE id = ? AND interview_id = ?",
                (request_id, interview_id),
            ).fetchone()
            if not req_row:
                raise KeyError(f"Followup request {request_id} not found")

            inv = conn.execute(
                "SELECT status FROM interviews WHERE id = ?", (interview_id,)
            ).fetchone()
            if not inv:
                raise KeyError(f"Interview {interview_id} not found")
            if str(inv["status"]).lower() not in ("recording", "paused"):
                raise RepositoryConflictError(f"Cannot retry followups in interview status '{inv['status']}'")

            job_id = req_row["job_id"]
            if not job_id:
                raise RepositoryConflictError(f"No job associated with request {request_id}")

            job_row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not job_row:
                raise KeyError(f"Job {job_id} not found")

            if job_row["status"] == "COMPLETED" and req_row["outcome"] in ("ready", "no_suggestions"):
                raise RepositoryConflictError("Cannot retry successfully completed request")

            conn.execute(
                """
                UPDATE jobs
                SET status = 'PENDING', attempts = 0, locked_until = NULL, locked_by = NULL, error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
            conn.execute(
                """
                UPDATE followup_requests
                SET outcome = NULL, error_code = NULL, completed_at = NULL
                WHERE id = ?
                """,
                (request_id,),
            )

            updated_req = conn.execute(
                "SELECT * FROM followup_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(updated_req)

    def get_asked_followup_history(self, interview_id: str) -> list[dict[str, Any]]:
        """Returns all follow-up questions marked as asked for canonical snapshot and report."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT id, request_id, question_id, rubric_revision_id, transcript_revision_id,
                       kind, question_text, purpose, asked_text, decided_at, created_at
                FROM followup_suggestions
                WHERE interview_id = ? AND status = 'asked'
                ORDER BY decided_at ASC, created_at ASC
                """,
                (interview_id,),
            ).fetchall()
            return [dict(r) for r in rows]


