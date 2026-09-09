"""
Repository Layer for Nebula Domain Entities and Durable Jobs.
Provides safe database operations with JSON serialization and timestamps.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from backend.db.database import Database
from contracts.domain import InterviewStatus


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        status: InterviewStatus = InterviewStatus.DRAFT,
        consent_confirmed_at: str | None = None,
        consent_version: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO interviews (
                    id, title, candidate_name, role, status,
                    consent_confirmed_at, consent_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interview_id,
                    title,
                    candidate_name,
                    role,
                    status.value,
                    consent_confirmed_at,
                    consent_version,
                    now,
                    now,
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

    def list_interviews(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM interviews ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_interview_status(
        self,
        interview_id: str,
        new_status: InterviewStatus,
        consent_confirmed_at: str | None = None,
        consent_version: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            if consent_confirmed_at:
                conn.execute(
                    """
                    UPDATE interviews
                    SET status = ?, consent_confirmed_at = ?, consent_version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status.value, consent_confirmed_at, consent_version, now, interview_id),
                )
            else:
                conn.execute(
                    "UPDATE interviews SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status.value, now, interview_id),
                )

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
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO transcript_segments (
                    id, interview_id, track_id, start_time_ms, end_time_ms, text, is_final, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment_id,
                    interview_id,
                    track_id,
                    start_time_ms,
                    end_time_ms,
                    text,
                    1 if is_final else 0,
                    now,
                ),
            )

    def get_transcript_segments(self, interview_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM transcript_segments
                WHERE interview_id = ?
                ORDER BY start_time_ms ASC
                """,
                (interview_id,),
            ).fetchall()
            return [dict(r) for r in rows]

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
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO assessment_proposals (
                    id, interview_id, question_id, model_profile_id,
                    scores_json, critical_errors_json, is_approved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    proposal_id,
                    interview_id,
                    question_id,
                    model_profile_id,
                    json.dumps(scores, ensure_ascii=False),
                    json.dumps(critical_errors or [], ensure_ascii=False),
                    now,
                ),
            )

    def get_assessment_proposals(self, interview_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
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
                if item["reviewed_scores_json"]:
                    item["reviewed_scores"] = json.loads(item["reviewed_scores_json"])
                res.append(item)
            return res

    def approve_assessment(
        self,
        proposal_id: str,
        reviewed_scores: list[dict[str, Any]] | None = None,
        reviewer_notes: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
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
            conn.execute(
                """
                INSERT INTO jobs (
                    id, type, interview_id, payload_json, status, attempts, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)
                """,
                (job_id, job_type, interview_id, json.dumps(payload, ensure_ascii=False), max_attempts, now, now),
            )

    def claim_next_job(self, lock_duration_sec: int = 60) -> dict[str, Any] | None:
        """Atomically leases next available pending or expired job."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            # Find eligible job
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'PENDING' OR (status = 'PROCESSING' AND locked_until < ?)
                ORDER BY created_at ASC LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None

            job = dict(row)
            # Lock it
            lock_until_dt = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + lock_duration_sec, tz=timezone.utc)
            lock_until_str = lock_until_dt.isoformat()
            conn.execute(
                """
                UPDATE jobs
                SET status = 'PROCESSING',
                    attempts = attempts + 1,
                    locked_until = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (lock_until_str, now, job["id"]),
            )
            job["payload"] = json.loads(job["payload_json"])
            job["attempts"] += 1
            return job

    def complete_job(self, job_id: str) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'COMPLETED', updated_at = ? WHERE id = ?",
                (now, job_id),
            )

    def fail_job(self, job_id: str, error_message: str) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row and row["attempts"] >= row["max_attempts"]:
                status = "FAILED"
            else:
                status = "PENDING"  # Re-enqueue for retry
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, locked_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (status, error_message, now, job_id),
            )

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
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO question_associations (
                    id, interview_id, question_id, segment_id, confidence,
                    is_ambiguous, is_manually_adjusted, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    question_id = excluded.question_id,
                    confidence = excluded.confidence,
                    is_ambiguous = excluded.is_ambiguous,
                    is_manually_adjusted = excluded.is_manually_adjusted,
                    notes = excluded.notes
                """,
                (
                    assoc_id,
                    interview_id,
                    question_id,
                    segment_id,
                    confidence,
                    1 if is_ambiguous else 0,
                    1 if is_manually_adjusted else 0,
                    notes,
                    now,
                ),
            )

    def get_associations(self, interview_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM question_associations WHERE interview_id = ? ORDER BY created_at ASC",
                (interview_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def reassociate_segment(
        self,
        interview_id: str,
        segment_id: str,
        new_question_id: str,
        notes: str = "Manually re-linked by reviewer",
    ) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM question_associations WHERE interview_id = ? AND segment_id = ?",
                (interview_id, segment_id),
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
                )

