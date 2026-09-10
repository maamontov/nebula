"""
Repository Layer for Nebula Domain Entities and Durable Jobs.
Provides safe database operations with JSON serialization and timestamps.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

from backend.core.scoring import calculate_interview_score
from backend.core.state_machine import InvalidStateTransitionError
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
    pass


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
        revision_id: str = "trans-rev-1",
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot add segment: interview {interview_id} does not exist or is deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

            conn.execute(
                """
                INSERT INTO transcript_segments (
                    id, interview_id, track_id, start_time_ms, end_time_ms, text, is_final, revision_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interview_id, revision_id, id) DO UPDATE SET
                    track_id = excluded.track_id,
                    start_time_ms = excluded.start_time_ms,
                    end_time_ms = excluded.end_time_ms,
                    text = excluded.text,
                    is_final = excluded.is_final
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

    def set_active_transcript_revision(self, interview_id: str, revision_id: str) -> None:
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Interview {interview_id} not found or deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")
            conn.execute(
                "UPDATE interviews SET active_transcript_revision_id = ? WHERE id = ?",
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
    ) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot save proposal: interview {interview_id} does not exist or is deleted")

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
            # Invalidate dependent confirmed summary if interview transcript changed
            conn.execute(
                """
                UPDATE summary_proposals
                SET is_confirmed = 0
                WHERE interview_id = ?
                """,
                (interview_id,),
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
            inv = conn.execute("SELECT id, status FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            if not inv or inv["status"] == "deleted":
                raise ValueError(f"Cannot confirm summary: interview {interview_id} does not exist or is deleted")
            if inv["status"] == "finalized":
                raise RepositoryConflictError(f"Interview {interview_id} is finalized and immutable")

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

            conn.execute(
                """
                INSERT INTO jobs (
                    id, type, interview_id, payload_json, status, attempts, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)
                """,
                (job_id, job_type, interview_id, json.dumps(payload, ensure_ascii=False), max_attempts, now, now),
            )

    def claim_next_job(self, lock_duration_sec: int = 60) -> dict[str, Any] | None:
        """Atomically leases next available pending or expired job with owner token."""
        now = utc_now_iso()
        owner_token = f"worker-{uuid.uuid4().hex}"
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
            # Lock it with owner token
            lock_until_dt = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + lock_duration_sec, tz=timezone.utc)
            lock_until_str = lock_until_dt.isoformat()
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'PROCESSING',
                    attempts = attempts + 1,
                    locked_until = ?,
                    locked_by = ?,
                    updated_at = ?
                WHERE id = ? AND (status = 'PENDING' OR (status = 'PROCESSING' AND locked_until < ?))
                """,
                (lock_until_str, owner_token, now, job["id"], now),
            )
            if cursor.rowcount == 0:
                # Concurrent race condition: another worker claimed it in between
                return None

            job["payload"] = json.loads(job["payload_json"])
            job["attempts"] += 1
            job["locked_by"] = owner_token
            return job

    def renew_job_lease(self, job_id: str, owner_token: str, extension_sec: int = 60) -> bool:
        """Extends the lease duration for an actively executing job if owner token matches."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            lock_until_dt = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + extension_sec, tz=timezone.utc)
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

    def reclaim_expired_jobs(self) -> int:
        """Counts and resets expired PROCESSING jobs back to PENDING for retry."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'PENDING', locked_until = NULL, locked_by = NULL, updated_at = ?
                WHERE status = 'PROCESSING' AND locked_until < ? AND attempts < max_attempts
                """,
                (now, now),
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
                    SET status = 'COMPLETED', updated_at = ?, locked_until = NULL, locked_by = NULL
                    WHERE id = ? AND status = 'PROCESSING' AND (locked_by = ? OR locked_by IS NULL)
                    """,
                    (now, job_id, owner_token),
                )
                if cursor.rowcount == 0:
                    raise RepositoryConflictError(f"Cannot complete job {job_id}: lease expired or owner token mismatch")
            else:
                cursor = conn.execute(
                    "UPDATE jobs SET status = 'COMPLETED', updated_at = ?, locked_until = NULL, locked_by = NULL WHERE id = ?",
                    (now, job_id),
                )
                if cursor.rowcount == 0:
                    raise RepositoryConflictError(f"Cannot complete job {job_id}: job not found")

    def fail_job(self, job_id: str, error_message: str, owner_token: str | None = None) -> None:
        """Marks job as FAILED or PENDING retry, verifying owner token if supplied."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT attempts, max_attempts, status, locked_by FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row and owner_token is not None and row["locked_by"] and row["locked_by"] != owner_token:
                raise RepositoryConflictError(f"Cannot fail job {job_id}: owner token mismatch")
            if row and row["attempts"] >= row["max_attempts"]:
                status = "FAILED"
            else:
                status = "PENDING"  # Re-enqueue for retry
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, locked_until = NULL, locked_by = NULL, updated_at = ?
                WHERE id = ?
                """,
                (status, error_message, now, job_id),
            )

    def get_interview_jobs_status(self, interview_id: str) -> dict[str, Any]:
        """Returns job counts and status summary for an interview."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM jobs WHERE interview_id = ? GROUP BY status",
                (interview_id,),
            ).fetchall()
            status_counts = {r["status"]: r["cnt"] for r in rows}
            pending_or_processing = (
                status_counts.get("PENDING", 0) + status_counts.get("PROCESSING", 0)
            )
            return {
                "interview_id": interview_id,
                "counts": status_counts,
                "pending_or_processing": pending_or_processing,
                "is_pipeline_idle": pending_or_processing == 0,
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


