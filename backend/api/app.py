"""
FastAPI entry point for Nebula backend.
Provides endpoints for health check, scoring, proposal validation, and lifecycle.
"""
import hashlib
import json
import uuid
import os
from typing import Any
from dotenv import load_dotenv

# Ensure environment variables (.env) are loaded
load_dotenv()
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.core.audio_health import AudioHealthMonitor, ChannelMetrics
from backend.core.evidence_validator import (
    validate_proposal,
)
from backend.core.matcher import QuestionMatcher
from backend.core.revisions import TranscriptDiffEngine
from backend.core.scoring import (
    ScoringError,
    calculate_interview_score,
)
from backend.core.state_machine import (
    InvalidStateTransitionError,
    transition_status,
)
from backend.db.database import get_db
from backend.db.repository import Repository, RepositoryConflictError
from contracts.audio import AudioChunkMetadata
from contracts.domain import (

    AssessmentProposal,
    HumanCriterionScore,
    HumanQuestionAssessment,
    InterviewStatus,
    PlannedQuestion,
    RubricCriterion,
    RubricRevision,
    TranscriptRevision,
)

import re
from pathlib import Path

app = FastAPI(
    title="Nebula Backend API",
    version="0.1.0",
    description="Core API for Nebula AI Interview Copilot",
)

# Allowed origins for Tauri desktop and local development loopback
# Разрешенные origins для десктопа Tauri и локальной разработки на loopback
ALLOWED_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
    "https://tauri.localhost",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def validate_safe_id(identifier: str, field_name: str = "id") -> str:
    """
    Validates that an identifier contains only safe alphanumeric/hyphen/underscore characters.
    Проверяет, что идентификатор содержит только безопасные символы и не позволяет path traversal.
    """
    if not identifier or not SAFE_IDENTIFIER_PATTERN.match(identifier):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}: must contain only alphanumeric characters, underscores, or hyphens (1-128 chars).",
        )
    return identifier


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def get_trusted_spool_dir() -> Path:
    spool_path = os.getenv("NEBULA_BACKEND_SPOOL_DIR") or os.getenv("NEBULA_SPOOL_DIR")
    if not spool_path:
        spool_path = PROJECT_ROOT / "data" / "spool_backend"
    path = Path(spool_path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_trusted_backup_dir() -> Path:
    backup_path = os.getenv("NEBULA_BACKUP_DIR")
    if not backup_path:
        backup_path = PROJECT_ROOT / "data" / "backups"
    path = Path(backup_path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_repository() -> Repository:
    db = get_db()
    db.init_schema()
    return Repository(db)


class CreateInterviewRequest(BaseModel):
    id: str
    title: str
    candidate_name: str
    role: str
    plan: dict[str, Any] | None = None
    capture_mode: str = "dual_source"


class AddSegmentRequest(BaseModel):
    id: str
    track_id: str
    start_time_ms: int
    end_time_ms: int
    text: str
    is_final: bool = True
    speaker_role: str = "unknown"
    parent_segment_id: str | None = None


class UpdateSpeakerRoleRequest(BaseModel):
    speaker_role: str


class SplitSegmentRequest(BaseModel):
    split_time_ms: int
    text_part1: str
    text_part2: str
    role_part1: str = "interviewer"
    role_part2: str = "candidate"


class ApproveAssessmentRequest(BaseModel):
    reviewed_scores: list[dict[str, Any]] | None = None
    reviewer_notes: str | None = None
    question_id: str | None = None


class EnqueueJobRequest(BaseModel):
    id: str
    type: str
    payload: dict[str, Any]
    max_attempts: int = 3


class CalculateScoreRequest(BaseModel):
    rubric: RubricRevision
    assessments: list[HumanQuestionAssessment]


class ValidateProposalRequest(BaseModel):
    proposal: AssessmentProposal
    transcript: TranscriptRevision


class TransitionStateRequest(BaseModel):
    current_status: InterviewStatus | None = None
    target_status: InterviewStatus
    consent_confirmed_at: str | None = None
    consent_version: str | None = None


class ReassociateSegmentRequest(BaseModel):
    new_question_id: str
    notes: str = "Manually reassociated by reviewer"


class BatchRetranscribeRequest(BaseModel):
    new_revision_id: str = "trans-rev-2"
    old_revision_id: str = "trans-rev-1"
    segments: list[dict[str, Any]] | None = None
    provenance: str = "batch_stt"


class ReviewAssessmentWithRevisionRequest(BaseModel):
    expected_transcript_revision: str
    scores: list[dict[str, Any]] = Field(default_factory=list)
    reviewer_notes: str | None = None
    reviewer_id: str | None = None
    is_manually_adjusted: bool = True
    is_excluded: bool = False
    exclusion_reason: str | None = None


class ConfirmSummaryRequest(BaseModel):
    reviewer_id: str
    confirmed_markdown: str
    confirmed_recommendation: str
    expected_transcript_revision: str | None = None


class FinalizeReportRequest(BaseModel):
    confirmed_by: str
    summary_markdown: str
    hiring_recommendation: str
    audio_limitations: list[str] | None = None
    expected_transcript_revision: str | None = None


class ReopenRevisionRequest(BaseModel):
    reviewer_id: str
    reason: str


class CreateBackupRequest(BaseModel):
    target_path: str | None = None


class IngestAudioChunkRequest(BaseModel):
    metadata: AudioChunkMetadata
    payload_hex: str


class StopInterviewRequest(BaseModel):
    manifests: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/healthz")

async def healthz():
    return {"status": "ok", "version": "0.1.0"}


# -------------------------------------------------------------
# Interview Management
# -------------------------------------------------------------
@app.post("/api/v1/interviews")
async def create_interview_endpoint(
    payload: CreateInterviewRequest,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(payload.id, "interview_id")
    inv = repo.create_interview(
        interview_id=payload.id,
        title=payload.title,
        candidate_name=payload.candidate_name,
        role=payload.role,
        status=InterviewStatus.DRAFT,
        capture_mode=payload.capture_mode,
    )
    if payload.plan:
        repo.save_plan(f"plan-{payload.id}", payload.id, payload.plan, version=1)
    repo.record_audit_event(
        event_id=f"audit-{uuid.uuid4().hex[:8]}",
        interview_id=payload.id,
        event_type="INTERVIEW_CREATED",
        payload={"title": payload.title, "candidate": payload.candidate_name, "role": payload.role},
    )
    return inv


@app.get("/api/v1/interviews")
async def list_interviews_endpoint(
    limit: int = 50,
    repo: Repository = Depends(get_repository),
):
    return repo.list_interviews(limit=limit)


def compute_interview_scoring(
    inv: dict[str, Any],
    plan_payload: dict[str, Any] | None,
    human_assessments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not plan_payload or not plan_payload.get("questions"):
        return None
    try:
        domain_questions = []
        for q_dict in plan_payload.get("questions", []):
            domain_criteria = []
            for c_dict in q_dict.get("criteria", []):
                domain_criteria.append(
                    RubricCriterion(
                        id=c_dict["id"],
                        title=c_dict.get("title", ""),
                        description=c_dict.get("description", ""),
                        min_score=float(c_dict.get("min_score", 1.0)),
                        max_score=float(c_dict.get("max_score", 5.0)),
                        weight=float(c_dict.get("weight", 1.0)),
                    )
                )
            domain_questions.append(
                PlannedQuestion(
                    id=q_dict.get("id") or q_dict.get("question_id", "q"),
                    title=q_dict.get("title", ""),
                    prompt=q_dict.get("prompt") or q_dict.get("text", ""),
                    weight=float(q_dict.get("weight", 1.0)),
                    criteria=domain_criteria,
                )
            )
        rubric = RubricRevision(
            revision_id=inv.get("active_rubric_revision_id") or "rub-rev-1",
            interview_id=inv["id"],
            questions=domain_questions,
        )

        domain_assessments = []
        for ha in human_assessments:
            if ha.get("is_stale"):
                continue  # Stale human assessments do not contribute to active score
            crit_scores = []
            for sc in ha.get("scores", []):
                crit_scores.append(
                    HumanCriterionScore(
                        criterion_id=sc["criterion_id"],
                        score=float(sc["score"]) if sc.get("score") is not None else None,
                        explanation=sc.get("explanation"),
                    )
                )
            domain_assessments.append(
                HumanQuestionAssessment(
                    question_id=ha["question_id"],
                    criteria_scores=crit_scores,
                    reviewer_notes=ha.get("reviewer_notes"),
                    reviewer_id=ha.get("reviewer_id"),
                    is_manually_adjusted=bool(ha.get("is_manually_adjusted")),
                    is_excluded=bool(ha.get("is_excluded")),
                    exclusion_reason=ha.get("exclusion_reason"),
                )
            )

        res = calculate_interview_score(rubric, domain_assessments)
        return {
            "final_score_100": res.final_score_100,
            "coverage_percentage": round(res.coverage_percentage, 2),
            "total_planned_weight": res.total_planned_weight,
            "evaluated_weight": res.evaluated_weight,
        }
    except Exception as e:
        return {"final_score_100": None, "coverage_percentage": 0.0, "error": str(e)}


@app.get("/api/v1/interviews/{interview_id}")
async def get_interview_endpoint(
    interview_id: str,
    revision_id: str | None = None,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    target_rev = revision_id or inv.get("active_transcript_revision_id") or "trans-rev-1"
    plan = repo.get_latest_plan(interview_id)
    segments = repo.get_transcript_segments(interview_id, revision_id=target_rev)
    if not segments and not revision_id:
        all_segs = repo.get_transcript_segments(interview_id)
        if all_segs:
            segments = all_segs
            actual_rev = all_segs[0].get("revision_id")
            if actual_rev and actual_rev != target_rev:
                try:
                    repo.set_active_transcript_revision(interview_id, actual_rev)
                    inv["active_transcript_revision_id"] = actual_rev
                    target_rev = actual_rev
                except Exception:
                    pass
    proposals = repo.get_assessment_proposals(interview_id)
    human_assessments = repo.get_human_assessments(interview_id)
    plan_payload = plan["payload"] if plan else None
    scoring = compute_interview_scoring(inv, plan_payload, human_assessments)

    return {
        "interview": inv,
        "plan": plan_payload,
        "transcript_segments": segments,
        "assessment_proposals": proposals,
        "human_assessments": human_assessments,
        "scoring": scoring,
    }


@app.delete("/api/v1/interviews/{interview_id}")
async def delete_interview_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    spool_dir = get_trusted_spool_dir()
    try:
        deleted = repo.delete_interview(interview_id=interview_id, spool_dir=spool_dir)
        if not deleted:
            raise HTTPException(status_code=500, detail="Failed to delete interview records")
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Interview physical cleanup failed: {str(exc)}")

    return {"status": "deleted", "interview_id": interview_id, "success": True}


def check_interview_readiness(
    interview_id: str,
    repo: Repository,
    spool_dir: Path,
) -> tuple[bool, dict[str, Any]]:
    """
    Evaluates server-side pipeline readiness gate for transitioning to REVIEW:
    1. Inspects track manifests in spool_dir / interview_id.
    2. Verifies that all expected chunk sequences from manifests are ingested into audio_chunks.
    3. Verifies that all STT transcription jobs for this interview are completed.
    4. Detects failed STT jobs or missing chunks and provides granular diagnostics.
    """
    interview_path = spool_dir / interview_id
    manifests_found: dict[str, dict[str, Any]] = {}

    inv = repo.get_interview(interview_id)
    expected_tracks = ["candidate", "interviewer"]
    if inv:
        if inv.get("expected_tracks_json"):
            try:
                expected_tracks = json.loads(inv["expected_tracks_json"])
            except Exception:
                pass
        elif inv.get("capture_mode") == "single_source":
            expected_tracks = ["shared"]

    if interview_path.exists():
        for track in expected_tracks:
            m_path = interview_path / track / "manifest.json"
            if m_path.exists():
                try:
                    with open(m_path, "r", encoding="utf-8") as f:
                        manifests_found[track] = json.load(f)
                except Exception:
                    pass

    # Check database records
    db_chunks = repo.get_audio_chunks(interview_id)
    chunks_by_track: dict[str, set[int]] = {}
    for c in db_chunks:
        t_id = str(c.get("track_id", "")).lower()
        if "candidate" in t_id:
            key = "candidate"
        elif "interviewer" in t_id:
            key = "interviewer"
        elif "shared" in t_id:
            key = "shared"
        else:
            key = t_id
        chunks_by_track.setdefault(key, set()).add(c["sequence"])

    # Query jobs
    with repo.db.transaction() as conn:
        job_rows = conn.execute(
            "SELECT id, type, status FROM jobs WHERE interview_id = ?",
            (interview_id,),
        ).fetchall()

    stt_jobs = [r for r in job_rows if r["type"] == "TRANSCRIBE_AUDIO"]
    pending_jobs = [r for r in stt_jobs if r["status"] in ("PENDING", "PROCESSING")]
    failed_jobs = [r for r in stt_jobs if r["status"] == "FAILED"]
    completed_jobs = [r for r in stt_jobs if r["status"] == "COMPLETED"]

    missing_chunks: dict[str, list[int]] = {}
    total_expected_chunks = 0

    for track, m in manifests_found.items():
        total_c = m.get("total_chunks", 0)
        total_expected_chunks += total_c
        ingested = chunks_by_track.get(track, set())
        expected = set(range(total_c))
        diff = sorted(list(expected - ingested))
        if diff:
            missing_chunks[track] = diff

    # If manifests exist:
    if manifests_found:
        if missing_chunks:
            return (
                False,
                {
                    "is_ready": False,
                    "state": "AWAITING_CHUNKS",
                    "details": f"Missing chunks for tracks: {', '.join(f'{t}: {len(seqs)} chunks' for t, seqs in missing_chunks.items())}",
                    "missing_chunks": missing_chunks,
                    "stt_jobs": {"completed": len(completed_jobs), "pending": len(pending_jobs), "failed": len(failed_jobs)},
                    "manifests": manifests_found,
                },
            )

        if failed_jobs:
            return (
                False,
                {
                    "is_ready": False,
                    "state": "STT_FAILED",
                    "details": f"{len(failed_jobs)} audio transcription jobs failed.",
                    "missing_chunks": {},
                    "stt_jobs": {"completed": len(completed_jobs), "pending": len(pending_jobs), "failed": len(failed_jobs)},
                    "manifests": manifests_found,
                },
            )

        if pending_jobs:
            return (
                False,
                {
                    "is_ready": False,
                    "state": "STT_IN_PROGRESS",
                    "details": f"STT transcription in progress ({len(completed_jobs)}/{len(stt_jobs)} completed, {len(pending_jobs)} pending).",
                    "missing_chunks": {},
                    "stt_jobs": {"completed": len(completed_jobs), "pending": len(pending_jobs), "failed": len(failed_jobs)},
                    "manifests": manifests_found,
                },
            )

        if total_expected_chunks > 0 and len(completed_jobs) < len(db_chunks):
            return (
                False,
                {
                    "is_ready": False,
                    "state": "STT_IN_PROGRESS",
                    "details": f"Awaiting STT job execution for {len(db_chunks) - len(completed_jobs)} chunks.",
                    "missing_chunks": {},
                    "stt_jobs": {"completed": len(completed_jobs), "pending": len(pending_jobs), "failed": len(failed_jobs)},
                    "manifests": manifests_found,
                },
            )

        return (
            True,
            {
                "is_ready": True,
                "state": "READY_FOR_REVIEW",
                "details": "All manifests sealed, chunks ingested and transcribed.",
                "missing_chunks": {},
                "stt_jobs": {"completed": len(completed_jobs), "pending": 0, "failed": 0},
                "manifests": manifests_found,
            },
        )

    # If no manifests found on disk:
    # If chunks were ingested into database, an unsealed session cannot transition to review
    if db_chunks:
        return (
            False,
            {
                "is_ready": False,
                "state": "AWAITING_MANIFEST",
                "details": f"Found {len(db_chunks)} audio chunks in database, but track manifests have not been sealed yet.",
                "missing_chunks": {},
                "stt_jobs": {"completed": len(completed_jobs), "pending": len(pending_jobs), "failed": len(failed_jobs)},
                "manifests": {},
            },
        )

    # If any pending jobs exist (e.g. batch transcription or other async processing):
    if pending_jobs:
        return (
            False,
            {
                "is_ready": False,
                "state": "PROCESSING",
                "details": f"{len(pending_jobs)} jobs are still in progress.",
                "missing_chunks": {},
                "stt_jobs": {"completed": len(completed_jobs), "pending": len(pending_jobs), "failed": len(failed_jobs)},
                "manifests": {},
            },
        )

    # Synthetic or text-only interviews (e.g. in unit test fixtures):
    return (
        True,
        {
            "is_ready": True,
            "state": "READY_FOR_REVIEW",
            "details": "No pending audio processing.",
            "missing_chunks": {},
            "stt_jobs": {"completed": len(completed_jobs), "pending": 0, "failed": 0},
            "manifests": {},
        },
    )


@app.get("/api/v1/interviews/{interview_id}/readiness")
async def get_interview_readiness_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
    spool_dir: Path = Depends(get_trusted_spool_dir),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    _is_ready, report = check_interview_readiness(interview_id, repo, spool_dir)
    return report


@app.post("/api/v1/interviews/{interview_id}/status")
async def update_status_endpoint(
    interview_id: str,
    payload: TransitionStateRequest,
    repo: Repository = Depends(get_repository),
    spool_dir: Path = Depends(get_trusted_spool_dir),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    if payload.target_status == InterviewStatus.REVIEW:
        is_ready, report = check_interview_readiness(interview_id, repo, spool_dir)
        if not is_ready:
            raise HTTPException(
                status_code=409,
                detail=f"Interview readiness gate failed: {report['details']} (state: {report['state']})",
            )

    try:
        current_status = InterviewStatus(inv["status"])
        new_status = transition_status(current_status, payload.target_status)
        repo.update_interview_status(
            interview_id=interview_id,
            new_status=new_status,
            consent_confirmed_at=payload.consent_confirmed_at,
            consent_version=payload.consent_version,
        )
        repo.record_audit_event(
            event_id=f"audit-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            event_type="STATUS_TRANSITIONED",
            payload={"from": current_status.value, "to": new_status.value},
        )
        return {"status": new_status.value}
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/audio/chunks")
async def ingest_audio_chunk_endpoint(
    interview_id: str,
    payload: IngestAudioChunkRequest,
    repo: Repository = Depends(get_repository),
    spool_dir: Path = Depends(get_trusted_spool_dir),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    status_str = inv["status"]
    if status_str == InterviewStatus.FINALIZED.value:
        raise HTTPException(status_code=409, detail="Cannot ingest audio chunks into finalized interview")
    if status_str not in (
        InterviewStatus.RECORDING.value,
        InterviewStatus.PAUSED.value,
        InterviewStatus.PROCESSING.value,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot ingest audio chunks in status '{status_str}'; must be recording, paused, or processing",
        )

    try:
        payload_bytes = bytes.fromhex(payload.payload_hex)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid payload_hex: not valid hexadecimal string")

    # Validate size
    if len(payload_bytes) != payload.metadata.size_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"Payload byte size mismatch: expected {payload.metadata.size_bytes}, got {len(payload_bytes)}",
        )

    # Validate SHA-256
    computed_hash = hashlib.sha256(payload_bytes).hexdigest()
    if computed_hash.lower() != payload.metadata.checksum_sha256.lower():
        raise HTTPException(
            status_code=422,
            detail=f"Payload checksum mismatch: declared {payload.metadata.checksum_sha256}, computed {computed_hash}",
        )

    try:
        track_val = payload.metadata.track_id.value if hasattr(payload.metadata.track_id, "value") else str(payload.metadata.track_id)
        fmt_val = payload.metadata.format.value if hasattr(payload.metadata.format, "value") else str(payload.metadata.format)
        res_status, _is_new = repo.save_audio_chunk(
            interview_id=interview_id,
            track_id=track_val,
            capture_epoch=payload.metadata.capture_epoch,
            sequence=payload.metadata.sequence,
            start_time_ms=payload.metadata.start_time_ms,
            end_time_ms=payload.metadata.end_time_ms,
            sample_rate=payload.metadata.sample_rate,
            channels=payload.metadata.channels,
            sample_count=payload.metadata.sample_count,
            format_str=fmt_val,
            checksum_sha256=computed_hash,
            payload_bytes=payload_bytes,
            spool_base_dir=spool_dir,
        )
        return {
            "status": res_status,
            "interview_id": interview_id,
            "track_id": track_val,
            "sequence": payload.metadata.sequence,
        }
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/pause")
async def pause_interview_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    current_status = InterviewStatus(inv["status"])
    try:
        new_status = transition_status(current_status, InterviewStatus.PAUSED)
        repo.update_interview_status(interview_id, new_status)
        repo.record_audit_event(
            event_id=f"audit-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            event_type="INTERVIEW_PAUSED",
            payload={"from": current_status.value, "to": new_status.value},
        )
        return {"status": new_status.value}
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/resume")
async def resume_interview_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    current_status = InterviewStatus(inv["status"])
    try:
        new_status = transition_status(current_status, InterviewStatus.RECORDING)
        repo.update_interview_status(interview_id, new_status)
        repo.record_audit_event(
            event_id=f"audit-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            event_type="INTERVIEW_RESUMED",
            payload={"from": current_status.value, "to": new_status.value},
        )
        return {"status": new_status.value}
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/stop")
async def stop_interview_endpoint(
    interview_id: str,
    payload: StopInterviewRequest | None = None,
    repo: Repository = Depends(get_repository),
    spool_dir: Path = Depends(get_trusted_spool_dir),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    current_status = InterviewStatus(inv["status"])
    try:
        new_status = transition_status(current_status, InterviewStatus.PROCESSING)
        repo.update_interview_status(interview_id, new_status)

        if payload and payload.manifests:
            for manifest_dict in payload.manifests:
                track = manifest_dict.get("track_id", "unknown")
                track_dir = Path(spool_dir) / interview_id / track
                track_dir.mkdir(parents=True, exist_ok=True)
                manifest_path = track_dir / "manifest.json"
                manifest_path.write_text(json.dumps(manifest_dict, indent=2, ensure_ascii=False), encoding="utf-8")

        repo.record_audit_event(
            event_id=f"audit-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            event_type="INTERVIEW_STOPPED",
            payload={"from": current_status.value, "to": new_status.value},
        )
        return {"status": new_status.value}
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/segments")

async def add_segment_endpoint(
    interview_id: str,
    payload: AddSegmentRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    if InterviewStatus(inv["status"]) == InterviewStatus.FINALIZED:
        raise HTTPException(status_code=409, detail="Interview is finalized and immutable")

    try:
        repo.add_transcript_segment(
            segment_id=payload.id,
            interview_id=interview_id,
            track_id=payload.track_id,
            start_time_ms=payload.start_time_ms,
            end_time_ms=payload.end_time_ms,
            text=payload.text,
            is_final=payload.is_final,
            speaker_role=payload.speaker_role,
            parent_segment_id=payload.parent_segment_id,
        )
        return {"status": "ok", "segment_id": payload.id}
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/segments/{segment_id}/speaker-role")
async def update_segment_speaker_role_endpoint(
    interview_id: str,
    segment_id: str,
    payload: UpdateSpeakerRoleRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    if InterviewStatus(inv["status"]) == InterviewStatus.FINALIZED:
        raise HTTPException(status_code=409, detail="Interview is finalized and immutable")

    role = payload.speaker_role.lower()
    if role not in ("candidate", "interviewer", "unknown"):
        raise HTTPException(status_code=422, detail="Invalid speaker_role. Must be 'candidate', 'interviewer', or 'unknown'")

    try:
        updated = repo.update_segment_speaker_role(
            interview_id=interview_id,
            segment_id=segment_id,
            speaker_role=role,
        )
        return {"status": "ok", "segment": updated}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/segments/{segment_id}/split")
async def split_segment_endpoint(
    interview_id: str,
    segment_id: str,
    payload: SplitSegmentRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    if InterviewStatus(inv["status"]) == InterviewStatus.FINALIZED:
        raise HTTPException(status_code=409, detail="Interview is finalized and immutable")

    try:
        seg1, seg2 = repo.split_transcript_segment(
            interview_id=interview_id,
            segment_id=segment_id,
            split_time_ms=payload.split_time_ms,
            text_part1=payload.text_part1,
            text_part2=payload.text_part2,
            role_part1=payload.role_part1,
            role_part2=payload.role_part2,
        )
        return {"status": "ok", "segment_part1": seg1, "segment_part2": seg2}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/jobs/enqueue")
async def enqueue_job_endpoint(
    interview_id: str,
    payload: EnqueueJobRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    if InterviewStatus(inv["status"]) == InterviewStatus.FINALIZED:
        raise HTTPException(status_code=409, detail="Interview is finalized and immutable")

    try:
        repo.enqueue_job(
            job_id=payload.id,
            job_type=payload.type,
            interview_id=interview_id,
            payload=payload.payload,
            max_attempts=payload.max_attempts,
        )
        return {"status": "enqueued", "job_id": payload.id}
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/v1/interviews/{interview_id}/jobs/status")
async def get_jobs_status_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    return repo.get_interview_jobs_status(interview_id)



@app.post("/api/v1/interviews/{interview_id}/assessments/{proposal_id}/approve")
async def approve_assessment_endpoint(
    interview_id: str,
    proposal_id: str,
    payload: ApproveAssessmentRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    if InterviewStatus(inv["status"]) == InterviewStatus.FINALIZED:
        raise HTTPException(status_code=409, detail="Interview is finalized and immutable")

    revs = repo.get_transcript_revisions(interview_id)
    latest_rev = revs[-1]["id"] if revs else (inv.get("active_transcript_revision_id") or "trans-rev-1")

    prop = repo.get_assessment_proposal(proposal_id)
    if prop and prop.get("is_rejected"):
        errs = prop.get("validation_errors") or prop.get("critical_errors") or []
        raise HTTPException(
            status_code=409,
            detail=f"Proposal was rejected due to evidence validation errors: {errs}. It cannot be approved automatically. Submit a manual review via /review endpoint instead.",
        )

    question_id = payload.question_id or (prop["question_id"] if prop else "q-default")

    assessment_id = f"ha-{interview_id}-{question_id}"
    repo.save_human_assessment(
        assessment_id=assessment_id,
        interview_id=interview_id,
        question_id=question_id,
        rubric_revision_id=inv.get("active_rubric_revision_id") or "rub-rev-1",
        transcript_revision_id=latest_rev,
        scores=payload.reviewed_scores or [],
        reviewer_notes=payload.reviewer_notes or "Оценка подтверждена экспертом",
        is_manually_adjusted=True,
    )
    repo.record_audit_event(
        event_id=f"audit-{uuid.uuid4().hex[:8]}",
        interview_id=interview_id,
        event_type="ASSESSMENT_APPROVED",
        payload={"proposal_id": proposal_id, "question_id": question_id, "reviewer_notes": payload.reviewer_notes},
    )
    return {"status": "approved", "proposal_id": proposal_id, "question_id": question_id}


@app.get("/api/v1/interviews/{interview_id}/export")
async def export_interview_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    is_finalized = inv["status"] == "finalized"
    latest_report = repo.get_latest_report_revision(interview_id)

    if is_finalized and latest_report and latest_report.get("canonical_snapshot"):
        snapshot = latest_report["canonical_snapshot"]
        return {
            "interview_id": inv["id"],
            "title": inv["title"],
            "candidate_name": inv["candidate_name"],
            "role": inv["role"],
            "status": "FINALIZED",
            "is_draft": False,
            "final_score_100": latest_report["final_score_100"],
            "coverage_percentage": latest_report["coverage_percentage"],
            "sha256_checksum": latest_report["sha256_checksum"],
            "snapshot": snapshot,
            "report_revision": latest_report,
            "summary": snapshot.get("executive_summary"),
            "human_assessments": snapshot.get("human_assessments"),
            "audit_trail": repo.get_audit_events(interview_id),
        }

    plan = repo.get_latest_plan(interview_id)
    segments = repo.get_transcript_segments(interview_id)
    proposals = repo.get_assessment_proposals(interview_id)
    human_assessments = repo.get_human_assessments(interview_id)
    audit_events = repo.get_audit_events(interview_id)
    plan_payload = plan["payload"] if plan else None

    # Calculate deterministic scoring
    scoring = compute_interview_scoring(inv, plan_payload, human_assessments)
    calc_final_score = scoring["final_score_100"] if scoring else None

    summary_prop = repo.get_summary_proposal(interview_id)

    decisions = []
    for ha in human_assessments:
        decisions.append({
            "question_id": ha["question_id"],
            "scores": ha.get("scores", []),
            "reviewer_notes": ha.get("reviewer_notes"),
            "reviewer_id": ha.get("reviewer_id"),
            "is_manually_adjusted": bool(ha.get("is_manually_adjusted")),
            "is_excluded": bool(ha.get("is_excluded")),
            "exclusion_reason": ha.get("exclusion_reason"),
            "updated_at": ha.get("updated_at"),
        })

    final_score = latest_report["final_score_100"] if latest_report else calc_final_score

    return {
        "interview_id": inv["id"],
        "title": inv["title"],
        "candidate_name": inv["candidate_name"],
        "role": inv["role"],
        "status": inv["status"].upper(),
        "is_draft": True,
        "plan": plan_payload,
        "transcript_segments": segments,
        "assessment_proposals": proposals,
        "human_assessments": human_assessments,
        "scoring": scoring,
        "decisions": decisions,
        "final_score_100": final_score,
        "report_revision": latest_report,
        "sha256_checksum": None,
        "summary": summary_prop.get("summary_data") if summary_prop else None,
        "audit_trail": audit_events,
    }


@app.post("/api/v1/interviews/{interview_id}/batch-retranscribe")
async def batch_retranscribe_endpoint(
    interview_id: str,
    payload: BatchRetranscribeRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    if InterviewStatus(inv["status"]) == InterviewStatus.FINALIZED:
        raise HTTPException(status_code=409, detail="Interview is finalized and immutable")

    job_id = f"job-batch-{uuid.uuid4().hex[:8]}"
    try:
        repo.enqueue_job(
            job_id=job_id,
            job_type="BATCH_RETRANSCRIBE",
            interview_id=interview_id,
            payload={
                "new_revision_id": payload.new_revision_id,
                "old_revision_id": payload.old_revision_id,
                "segments": payload.segments,
            },
        )
        return {"status": "enqueued", "job_id": job_id, "new_revision_id": payload.new_revision_id}
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/v1/interviews/{interview_id}/revisions/transcript/diff")
async def get_transcript_diff_endpoint(
    interview_id: str,
    from_rev: str = "trans-rev-1",
    to_rev: str = "trans-rev-2",
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    old_segs = repo.get_transcript_segments(interview_id, revision_id=from_rev)
    new_segs = repo.get_transcript_segments(interview_id, revision_id=to_rev)

    diff_engine = TranscriptDiffEngine()
    segment_diffs = diff_engine.compare_revisions(old_segs, new_segs)

    # Question-level diff evaluation
    assocs = repo.get_associations(interview_id)
    existing_proposals = repo.get_assessment_proposals(interview_id)

    questions_map: dict[str, list[str]] = {}
    for a in assocs:
        q_id = a.get("question_id")
        s_id = a.get("segment_id")
        if q_id and s_id:
            questions_map.setdefault(q_id, []).append(s_id)

    question_reports = []
    for q_id, s_ids in questions_map.items():
        existing_evidence = []
        for p in existing_proposals:
            if p["question_id"] == q_id:
                for sc in p.get("scores", []):
                    existing_evidence.extend(sc.get("evidence", []))

        rep = diff_engine.evaluate_question_diff(
            question_id=q_id,
            question_segment_ids=s_ids,
            segment_diffs=segment_diffs,
            existing_evidence=existing_evidence,
            new_segments=new_segs,
        )
        question_reports.append({
            "question_id": rep.question_id,
            "is_modified": rep.is_modified,
            "diff_ratio": round(rep.diff_ratio, 3),
            "stale_reason": rep.stale_reason,
            "broken_evidence_count": len(rep.broken_evidence),
            "changed_segments_count": len(rep.changed_segments),
        })

    return {
        "interview_id": interview_id,
        "from_revision": from_rev,
        "to_revision": to_rev,
        "total_segment_diffs": len(segment_diffs),
        "question_reports": question_reports,
    }


@app.get("/api/v1/interviews/{interview_id}/revisions/transcript")
async def get_transcript_revisions_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    revs = repo.get_transcript_revisions(interview_id)
    active_rev = inv.get("active_transcript_revision_id") or "trans-rev-1"
    return {
        "interview_id": interview_id,
        "active_revision_id": active_rev,
        "revisions": revs,
    }


@app.get("/api/v1/interviews/{interview_id}/revisions/transcript/{revision_id}/segments")
async def get_revision_segments_endpoint(
    interview_id: str,
    revision_id: str,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    segments = repo.get_transcript_segments(interview_id, revision_id=revision_id)
    return {
        "interview_id": interview_id,
        "revision_id": revision_id,
        "segments": segments,
    }


@app.post("/api/v1/interviews/{interview_id}/assessments/{question_id}/review")
async def review_assessment_endpoint(
    interview_id: str,
    question_id: str,
    payload: ReviewAssessmentWithRevisionRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    if InterviewStatus(inv["status"]) == InterviewStatus.FINALIZED:
        raise HTTPException(status_code=409, detail="Interview is finalized and immutable")

    # Conflict check: verify expected transcript revision against active revision
    revs = repo.get_transcript_revisions(interview_id)
    active_rev = inv.get("active_transcript_revision_id") or (revs[-1]["id"] if revs else "trans-rev-1")

    if payload.expected_transcript_revision != active_rev:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Revision conflict: active transcript revision is '{active_rev}', "
                f"but review was submitted for '{payload.expected_transcript_revision}'. "
                "Please refresh and review the updated transcript before submitting."
            ),
        )

    # Save human assessment (immutable record of human decision)
    assessment_id = f"pass-{uuid.uuid4().hex[:8]}"
    try:
        repo.save_human_assessment(
            assessment_id=assessment_id,
            interview_id=interview_id,
            question_id=question_id,
            rubric_revision_id="rub-rev-1",
            transcript_revision_id=payload.expected_transcript_revision,
            scores=payload.scores,
            reviewer_notes=payload.reviewer_notes,
            reviewer_id=payload.reviewer_id,
            is_manually_adjusted=payload.is_manually_adjusted,
            is_stale=False,
            stale_reason=None,
            is_excluded=payload.is_excluded,
            exclusion_reason=payload.exclusion_reason,
        )
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))

    repo.record_audit_event(
        event_id=f"audit-{uuid.uuid4().hex[:8]}",
        interview_id=interview_id,
        event_type="HUMAN_ASSESSMENT_CONFIRMED",
        payload={
            "assessment_id": assessment_id,
            "question_id": question_id,
            "transcript_revision_id": payload.expected_transcript_revision,
            "is_manually_adjusted": payload.is_manually_adjusted,
            "is_excluded": payload.is_excluded,
            "reviewer_notes": payload.reviewer_notes,
        },
    )

    return {
        "status": "confirmed",
        "assessment_id": assessment_id,
        "question_id": question_id,
        "transcript_revision_id": payload.expected_transcript_revision,
    }


@app.get("/api/v1/interviews/{interview_id}/summary")
async def get_summary_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    summary_prop = repo.get_summary_proposal(interview_id)
    if not summary_prop:
        return {"interview_id": interview_id, "has_summary": False, "summary": None}

    return {
        "interview_id": interview_id,
        "has_summary": True,
        "is_confirmed": bool(summary_prop.get("is_confirmed")),
        "model_profile_id": summary_prop.get("model_profile_id"),
        "summary": summary_prop.get("summary_data"),
        "confirmed_markdown": summary_prop.get("confirmed_markdown"),
        "confirmed_recommendation": summary_prop.get("confirmed_recommendation"),
    }


@app.post("/api/v1/interviews/{interview_id}/summary/confirm")
async def confirm_summary_endpoint(
    interview_id: str,
    payload: ConfirmSummaryRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    if InterviewStatus(inv["status"]) == InterviewStatus.FINALIZED:
        raise HTTPException(status_code=409, detail="Interview is finalized and immutable")

    # Validate optimistic concurrency against active transcript revision
    active_rev = inv.get("active_transcript_revision_id") or "trans-rev-1"
    if payload.expected_transcript_revision and payload.expected_transcript_revision != active_rev:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Revision conflict: active transcript revision is '{active_rev}', "
                f"but summary confirmation was submitted for '{payload.expected_transcript_revision}'. "
                "Please review the updated transcript before confirming summary."
            ),
        )

    try:
        repo.confirm_summary(
            interview_id=interview_id,
            reviewer_id=payload.reviewer_id,
            confirmed_markdown=payload.confirmed_markdown,
            confirmed_recommendation=payload.confirmed_recommendation,
        )

        repo.record_audit_event(
            event_id=f"audit-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            event_type="EXECUTIVE_SUMMARY_CONFIRMED",
            payload={
                "confirmed_by": payload.reviewer_id,
                "recommendation": payload.confirmed_recommendation,
            },
        )

        return {"status": "summary_confirmed", "interview_id": interview_id}
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/report/finalize")
async def finalize_report_endpoint(
    interview_id: str,
    payload: FinalizeReportRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    active_rev = inv.get("active_transcript_revision_id") or "trans-rev-1"
    if payload.expected_transcript_revision and payload.expected_transcript_revision != active_rev:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Revision conflict: active transcript revision is '{active_rev}', "
                f"but finalization was submitted for '{payload.expected_transcript_revision}'. "
                "Please review the updated transcript before finalizing."
            ),
        )

    try:
        return repo.finalize_interview(
            interview_id=interview_id,
            confirmed_by=payload.confirmed_by,
            summary_markdown=payload.summary_markdown,
            hiring_recommendation=payload.hiring_recommendation,
            audio_limitations=payload.audio_limitations,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/interviews/{interview_id}/revisions/reopen")
async def reopen_revision_endpoint(
    interview_id: str,
    payload: ReopenRevisionRequest,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    try:
        return repo.reopen_revision(
            interview_id=interview_id,
            reviewer_id=payload.reviewer_id,
            reason=payload.reason,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/interviews/{interview_id}/associations")
async def get_associations_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    assocs = repo.get_associations(interview_id)
    plan = repo.get_latest_plan(interview_id)
    segments = repo.get_transcript_segments(interview_id)
    if plan and segments:
        associated_segment_ids = {a["segment_id"] for a in assocs}
        unassociated = [s for s in segments if s["id"] not in associated_segment_ids]
        if unassociated:
            questions = plan.get("payload", {}).get("questions", [])
            matcher = QuestionMatcher()
            results = matcher.associate_segments(questions, segments, existing_associations=assocs)
            for r in results:
                if r.segment_id not in associated_segment_ids:
                    assoc_id = f"assoc-{uuid.uuid4().hex[:8]}"
                    repo.save_association(
                        assoc_id=assoc_id,
                        interview_id=interview_id,
                        question_id=r.question_id,
                        segment_id=r.segment_id,
                        confidence=r.confidence,
                        is_ambiguous=r.is_ambiguous,
                        is_manually_adjusted=False,
                        notes=r.notes,
                    )
            assocs = repo.get_associations(interview_id)

    return {"interview_id": interview_id, "associations": assocs}


@app.post("/api/v1/interviews/{interview_id}/segments/{segment_id}/associate")
async def reassociate_segment_endpoint(
    interview_id: str,
    segment_id: str,
    payload: ReassociateSegmentRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    repo.reassociate_segment(
        interview_id=interview_id,
        segment_id=segment_id,
        new_question_id=payload.new_question_id,
        notes=payload.notes,
    )
    repo.record_audit_event(
        event_id=f"audit-{uuid.uuid4().hex[:8]}",
        interview_id=interview_id,
        event_type="SEGMENT_REASSOCIATED",
        payload={
            "segment_id": segment_id,
            "new_question_id": payload.new_question_id,
            "notes": payload.notes,
        },
    )
    return {"status": "reassociated", "segment_id": segment_id, "question_id": payload.new_question_id}


@app.get("/api/v1/interviews/{interview_id}/health")
async def get_interview_health_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    monitor = AudioHealthMonitor()
    report = monitor.evaluate_health(
        interviewer=ChannelMetrics(rms=0.08, peak=0.35, silence_duration_ms=0, drift_ms=0),
        candidate=ChannelMetrics(rms=0.12, peak=0.45, silence_duration_ms=0, drift_ms=4),
    )
    return {
        "interview_id": interview_id,
        "is_healthy": report.is_healthy,
        "interviewer": {
            "status": report.interviewer_status.value,
            "warnings": report.interviewer_warnings,
        },
        "candidate": {
            "status": report.candidate_status.value,
            "warnings": report.candidate_warnings,
        },
    }



# -------------------------------------------------------------
# Scoring and Validation Utilities
# -------------------------------------------------------------
@app.post("/api/v1/scoring/calculate", response_model=None)
async def calculate_score_endpoint(payload: CalculateScoreRequest):
    try:
        res = calculate_interview_score(payload.rubric, payload.assessments)
        return {
            "final_score_100": res.final_score_100,
            "coverage_percentage": res.coverage_percentage,
            "total_planned_weight": res.total_planned_weight,
            "evaluated_weight": res.evaluated_weight,
            "questions": {
                qid: {
                    "question_id": qr.question_id,
                    "normalized_score": qr.normalized_score,
                    "is_excluded": qr.is_excluded,
                    "active_weight": qr.active_weight,
                    "criterion_normalized_scores": qr.criterion_normalized_scores,
                }
                for qid, qr in res.question_results.items()
            },
        }
    except ScoringError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/proposals/validate", response_model=None)
async def validate_proposal_endpoint(payload: ValidateProposalRequest):
    res = validate_proposal(payload.proposal, payload.transcript)
    return {
        "is_valid": res.is_valid,
        "requires_human_review": res.requires_human_review,
        "errors": res.errors,
        "warnings": res.warnings,
    }


@app.post("/api/v1/lifecycle/transition", response_model=None)
async def transition_state_endpoint(payload: TransitionStateRequest):
    try:
        new_status = transition_status(payload.current_status, payload.target_status)
        return {"current_status": new_status.value}
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))


# -------------------------------------------------------------
# System & Reliability Maintenance
# -------------------------------------------------------------
@app.post("/api/v1/system/backup")
async def backup_database_endpoint(
    payload: CreateBackupRequest | None = None,
    repo: Repository = Depends(get_repository),
):
    try:
        backup_dir = get_trusted_backup_dir()
        filename = "backup.db"
        if payload and payload.target_path:
            raw_path = payload.target_path.strip()
            if ".." in raw_path or "/" in raw_path or "\\" in raw_path:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid backup filename: path separators and '..' are forbidden; backups must reside in trusted directory.",
                )
            filename = raw_path

        target = repo.db.backup(filename, trusted_backup_dir=backup_dir)
        return {"status": "ok", "target_path": target}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")


@app.get("/api/v1/system/integrity")
async def check_database_integrity_endpoint(
    repo: Repository = Depends(get_repository),
):
    is_ok = repo.db.verify_integrity()
    return {"status": "ok" if is_ok else "corrupted", "integrity_ok": is_ok}


@app.get("/api/v1/system/config")
async def get_system_config_endpoint(
    spool_dir: Path = Depends(get_trusted_spool_dir),
    backup_dir: Path = Depends(get_trusted_backup_dir),
):
    """
    Returns canonical server storage paths and configuration.
    Возвращает канонические пути хранения и конфигурацию сервера.
    """
    capture_spool = os.getenv("NEBULA_CAPTURE_SPOOL_DIR") or str(PROJECT_ROOT / "data" / "spool_capture")
    data_dir = os.getenv("NEBULA_DATA_DIR") or str(PROJECT_ROOT / "data")
    db_path = os.getenv("NEBULA_DB_PATH") or str(PROJECT_ROOT / "data" / "nebula.db")
    return {
        "data_dir": str(Path(data_dir).resolve()),
        "backend_spool_dir": str(spool_dir.resolve()),
        "capture_spool_dir": str(Path(capture_spool).resolve()),
        "backup_dir": str(backup_dir.resolve()),
        "db_path": str(Path(db_path).resolve()),
    }

