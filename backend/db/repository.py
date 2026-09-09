"""
Repository Layer for Nebula Domain Entities and Durable Jobs.
Provides safe database operations with JSON serialization and timestamps.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
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

    def delete_interview(self, interview_id: str, spool_dir: str | Path | None = None) -> bool:
        """
        Permanently deletes an interview and cascades across all SQLite WAL tables.
        Cancels associated jobs and removes local audio spool files.
        """
        with self.db.transaction() as conn:
            row = conn.execute("SELECT id FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not row:
                return False

            # Delete jobs
            conn.execute("DELETE FROM jobs WHERE interview_id = ?", (interview_id,))
            # Delete audit events
            conn.execute("DELETE FROM audit_events WHERE interview_id = ?", (interview_id,))
            # Delete interview (triggers cascading delete for all child tables)
            conn.execute("DELETE FROM interviews WHERE id = ?", (interview_id,))

        # Physical audio spool deletion
        if spool_dir:
            spool_path = Path(spool_dir) / interview_id
            if spool_path.exists() and spool_path.is_dir():
                shutil.rmtree(spool_path, ignore_errors=True)

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
        revision_id: str = "trans-rev-1",
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO transcript_segments (
                    id, interview_id, track_id, start_time_ms, end_time_ms, text, is_final, revision_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment_id,
                    interview_id,
                    track_id,
                    start_time_ms,
                    end_time_ms,
                    text,
                    1 if is_final else 0,
                    revision_id,
                    now,
                ),
            )

    def get_transcript_segments(self, interview_id: str, revision_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            if revision_id:
                rows = conn.execute(
                    """
                    SELECT * FROM transcript_segments
                    WHERE interview_id = ? AND revision_id = ?
                    ORDER BY start_time_ms ASC
                    """,
                    (interview_id, revision_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM transcript_segments
                    WHERE interview_id = ?
                    ORDER BY start_time_ms ASC
                    """,
                    (interview_id,),
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
            conn.execute(
                """
                INSERT INTO transcript_revisions (id, interview_id, revision_number, is_batch_final, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (revision_id, interview_id, revision_number, 1 if is_batch_final else 0, now),
            )

    def get_transcript_revisions(self, interview_id: str) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM transcript_revisions WHERE interview_id = ? ORDER BY revision_number ASC",
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
        rubric_revision_id: str = "rub-rev-1",
        transcript_revision_id: str = "trans-rev-1",
        is_stale: bool = False,
        stale_reason: str | None = None,
        is_manually_adjusted: bool = False,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO assessment_proposals (
                    id, interview_id, question_id, rubric_revision_id, transcript_revision_id,
                    model_profile_id, scores_json, critical_errors_json, is_approved,
                    is_stale, stale_reason, is_manually_adjusted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    rubric_revision_id = excluded.rubric_revision_id,
                    transcript_revision_id = excluded.transcript_revision_id,
                    model_profile_id = excluded.model_profile_id,
                    scores_json = excluded.scores_json,
                    critical_errors_json = excluded.critical_errors_json,
                    is_stale = excluded.is_stale,
                    stale_reason = excluded.stale_reason,
                    is_manually_adjusted = excluded.is_manually_adjusted
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
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO human_assessments (
                    id, interview_id, question_id, rubric_revision_id, transcript_revision_id,
                    reviewer_id, scores_json, reviewer_notes, is_manually_adjusted,
                    is_stale, stale_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    rubric_revision_id = excluded.rubric_revision_id,
                    transcript_revision_id = excluded.transcript_revision_id,
                    reviewer_id = excluded.reviewer_id,
                    scores_json = excluded.scores_json,
                    reviewer_notes = excluded.reviewer_notes,
                    is_manually_adjusted = excluded.is_manually_adjusted,
                    is_stale = excluded.is_stale,
                    stale_reason = excluded.stale_reason,
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
                    now,
                    now,
                ),
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
    def save_summary_proposal(
        self,
        proposal_id: str,
        interview_id: str,
        model_profile_id: str,
        summary_data: dict[str, Any],
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO summary_proposals (
                    id, interview_id, model_profile_id, summary_data_json, is_confirmed, created_at
                ) VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(id) DO UPDATE SET
                    model_profile_id = excluded.model_profile_id,
                    summary_data_json = excluded.summary_data_json
                """,
                (
                    proposal_id,
                    interview_id,
                    model_profile_id,
                    json.dumps(summary_data, ensure_ascii=False),
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
            return item

    def confirm_summary(
        self,
        interview_id: str,
        reviewer_id: str,
        confirmed_markdown: str,
        confirmed_recommendation: str,
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
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
                (reviewer_id, confirmed_markdown, confirmed_recommendation, now, interview_id),
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
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO report_revisions (
                    id, interview_id, revision_number, final_score_100, coverage_percentage,
                    question_scores_json, summary_markdown, hiring_recommendation,
                    confirmed_by, sha256_checksum, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            return item

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

    def reclaim_expired_jobs(self) -> int:
        """Counts and resets expired PROCESSING jobs back to PENDING for retry."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'PENDING', locked_until = NULL, updated_at = ?
                WHERE status = 'PROCESSING' AND locked_until < ? AND attempts < max_attempts
                """,
                (now, now),
            )
            return cursor.rowcount

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

