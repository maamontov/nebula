"""
FastAPI entry point for Nebula backend.
Provides endpoints for health check, scoring, proposal validation, and lifecycle.
"""
import contextlib
import hashlib
import json
import math
import os
import urllib.parse
import uuid
from threading import Lock
from typing import Any

from dotenv import load_dotenv

# Ensure environment variables (.env) are loaded
load_dotenv()
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("nebula.api")

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.adapters.llm import OpenAICompatibleAdapter
from backend.adapters.stt import OpenAICompatibleSTTAdapter
from backend.core.ai_settings import resolve_ai_settings, to_model_profile
from backend.core.audio_health import AudioHealthMonitor, ChannelMetrics
from backend.core.audio_utils import pcm_s16le_to_wav_bytes
from backend.core.credential_store import CredentialStore
from backend.core.evidence_validator import (
    validate_proposal,
)
from backend.core.followup_generator import (
    build_followup_context,
    compute_candidate_fingerprint,
    is_candidate_segment,
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
from contracts.audio import AudioChunkMetadata, TrackManifest
from contracts.domain import (
    AssessmentProposal,
    HumanCriterionScore,
    HumanQuestionAssessment,
    InterviewPlan,
    InterviewStatus,
    PlannedQuestion,
    RubricCriterion,
    RubricRevision,
    TranscriptRevision,
)
from contracts.followups import (
    FollowUpMode,
    FollowUpsStateResponse,
    FollowUpTrigger,
    GenerateFollowUpsRequest,
    PatchFollowUpSuggestionRequest,
)
from contracts.provider import ProviderProfile, STTProfile, STTProtocol
from contracts.settings import (
    AiSettingsResponse,
    ApiKeyAction,
    AuthMode,
    TestAiSettingsRequest,
    TestAiSettingsResponse,
    TestTarget,
    UpdateAiSettingsRequest,
)

app = FastAPI(
    title="Nebula Backend API",
    version="0.1.0",
    description="Core API for Nebula AI Interview Copilot",
)

_ai_settings_update_lock = Lock()

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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
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
    template_id: str | None = None
    template_version: int | None = None


class UpdateInterviewDraftRequest(BaseModel):
    title: str | None = None
    candidate_name: str | None = None
    role: str | None = None
    plan: dict[str, Any] | None = None
    capture_mode: str | None = None
    template_id: str | None = None
    template_version: int | None = None


class CreateJobTemplateRequest(BaseModel):
    id: str | None = None
    title: str
    role: str
    level: str = "Middle"
    description: str = ""
    questions: list[dict[str, Any]] = Field(default_factory=list)


class UpdateJobTemplateRequest(BaseModel):
    title: str | None = None
    role: str | None = None
    level: str | None = None
    description: str | None = None
    questions: list[dict[str, Any]] | None = None


class DuplicateJobTemplateRequest(BaseModel):
    new_id: str | None = None
    title_suffix: str = " (Копия)"


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
    expected_revision_id: str | None = None


class SplitSegmentRequest(BaseModel):
    split_time_ms: int
    text_part1: str
    text_part2: str
    role_part1: str = "interviewer"
    role_part2: str = "candidate"
    expected_revision_id: str | None = None



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
    expected_revision_id: str | None = None



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
    manifests: list[TrackManifest] = Field(default_factory=list)


@app.get("/healthz")

async def healthz():
    return {"status": "ok", "version": "0.1.0"}


def validate_job_template_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated = []
    seen_ids = set()
    for idx, q_data in enumerate(questions):
        try:
            pq = PlannedQuestion.model_validate(q_data)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid question structure at index {idx}: {e}")
        if pq.id in seen_ids:
            raise HTTPException(status_code=422, detail=f"Duplicate question ID '{pq.id}' at index {idx}")
        seen_ids.add(pq.id)
        validated.append(pq.model_dump())
    return validated


# -------------------------------------------------------------
# Job Templates Management
# -------------------------------------------------------------
@app.post("/api/v1/job-templates")
async def create_job_template_endpoint(
    payload: CreateJobTemplateRequest,
    repo: Repository = Depends(get_repository),
):
    tpl_id = payload.id or f"tpl-{uuid.uuid4().hex[:8]}"
    validate_safe_id(tpl_id, "template_id")
    validated_questions = validate_job_template_questions(payload.questions)
    return repo.create_job_template(
        template_id=tpl_id,
        title=payload.title.strip(),
        role=payload.role.strip(),
        level=payload.level.strip(),
        description=payload.description.strip(),
        questions=validated_questions,
    )


@app.get("/api/v1/job-templates")
async def list_job_templates_endpoint(
    include_archived: bool = False,
    repo: Repository = Depends(get_repository),
):
    return repo.list_job_templates(include_archived=include_archived)


@app.get("/api/v1/job-templates/{template_id}")
async def get_job_template_endpoint(
    template_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(template_id, "template_id")
    tpl = repo.get_job_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail=f"Job template {template_id} not found")
    return tpl


@app.put("/api/v1/job-templates/{template_id}")
async def update_job_template_endpoint(
    template_id: str,
    payload: UpdateJobTemplateRequest,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(template_id, "template_id")
    validated_questions = None
    if payload.questions is not None:
        validated_questions = validate_job_template_questions(payload.questions)
    try:
        return repo.update_job_template(
            template_id=template_id,
            title=payload.title.strip() if payload.title is not None else None,
            role=payload.role.strip() if payload.role is not None else None,
            level=payload.level.strip() if payload.level is not None else None,
            description=payload.description.strip() if payload.description is not None else None,
            questions=validated_questions,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/v1/job-templates/{template_id}/duplicate")
async def duplicate_job_template_endpoint(
    template_id: str,
    payload: DuplicateJobTemplateRequest = DuplicateJobTemplateRequest(),
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(template_id, "template_id")
    try:
        return repo.duplicate_job_template(
            template_id=template_id,
            new_id=payload.new_id,
            title_suffix=payload.title_suffix,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/v1/job-templates/{template_id}/archive")
async def archive_job_template_endpoint(
    template_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(template_id, "template_id")
    try:
        return repo.archive_job_template(template_id=template_id, is_archived=True)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/v1/job-templates/{template_id}/unarchive")
async def unarchive_job_template_endpoint(
    template_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(template_id, "template_id")
    try:
        return repo.archive_job_template(template_id=template_id, is_archived=False)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/api/v1/job-templates/{template_id}")
async def delete_job_template_endpoint(
    template_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(template_id, "template_id")
    try:
        deleted = repo.delete_job_template(template_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Job template {template_id} not found")
        return {"status": "deleted", "template_id": template_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/job-templates/{template_id}/questions/{question_id}/copy-to/{target_template_id}")
async def copy_question_to_template_endpoint(
    template_id: str,
    question_id: str,
    target_template_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(template_id, "source_template_id")
    validate_safe_id(target_template_id, "target_template_id")
    try:
        return repo.copy_question_to_template(
            source_template_id=template_id,
            question_id=question_id,
            target_template_id=target_template_id,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
        template_id=payload.template_id,
        template_version=payload.template_version,
    )
    plan_to_save = payload.plan
    if not plan_to_save and payload.template_id:
        tpl = repo.get_job_template(payload.template_id)
        if tpl:
            plan_to_save = {
                "id": f"plan-{payload.id}",
                "title": tpl["title"],
                "role": tpl["role"],
                "candidate_name": payload.candidate_name,
                "questions": tpl["questions"],
            }
    if plan_to_save:
        if isinstance(plan_to_save, dict) and not plan_to_save.get("candidate_name"):
            plan_to_save["candidate_name"] = payload.candidate_name
        repo.save_plan(f"plan-{payload.id}", payload.id, plan_to_save, version=1)
    repo.record_audit_event(
        event_id=f"audit-{uuid.uuid4().hex[:8]}",
        interview_id=payload.id,
        event_type="INTERVIEW_CREATED",
        payload={"title": payload.title, "candidate": payload.candidate_name, "role": payload.role, "template_id": payload.template_id},
    )
    return inv


@app.put("/api/v1/interviews/{interview_id}")
async def update_interview_endpoint(
    interview_id: str,
    payload: UpdateInterviewDraftRequest,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    if inv["status"] not in ("draft", "ready"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot edit interview in status '{inv['status']}', expected 'draft' or 'ready'",
        )

    if payload.capture_mode is not None and payload.capture_mode not in ("single_source", "dual_source"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid capture_mode '{payload.capture_mode}', expected 'single_source' or 'dual_source'",
        )

    try:
        repo.update_interview_draft(
            interview_id=interview_id,
            title=payload.title,
            candidate_name=payload.candidate_name,
            role=payload.role,
            template_id=payload.template_id,
            template_version=payload.template_version,
            capture_mode=payload.capture_mode,
        )
    except RepositoryConflictError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err

    if payload.plan is not None:
        latest_plan = repo.get_latest_plan(interview_id)
        next_ver = (latest_plan["version"] + 1) if latest_plan else 1
        plan_dict = dict(payload.plan) if isinstance(payload.plan, dict) else payload.plan
        if isinstance(plan_dict, dict) and not plan_dict.get("candidate_name"):
            plan_dict["candidate_name"] = payload.candidate_name or inv.get("candidate_name")
        repo.save_plan(
            plan_id=f"plan-{interview_id}-v{next_ver}",
            interview_id=interview_id,
            payload=plan_dict,
            version=next_ver,
        )

    repo.record_audit_event(
        event_id=f"audit-{uuid.uuid4().hex[:8]}",
        interview_id=interview_id,
        event_type="INTERVIEW_DRAFT_UPDATED",
        payload={
            "title": payload.title,
            "candidate": payload.candidate_name,
            "role": payload.role,
            "capture_mode": payload.capture_mode,
        },
    )
    return repo.get_interview(interview_id)


@app.get("/api/v1/interviews")
async def list_interviews_endpoint(
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
    role: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    repo: Repository = Depends(get_repository),
):
    items = repo.list_interviews(
        limit=limit,
        offset=offset,
        search=search,
        role=role,
        status=status,
        from_date=from_date,
        to_date=to_date,
    )
    total = repo.count_interviews(
        search=search,
        role=role,
        status=status,
        from_date=from_date,
        to_date=to_date,
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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
        "asked_followups": repo.get_asked_followup_history(interview_id),
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
        raise HTTPException(status_code=500, detail=f"Interview physical cleanup failed: {exc!s}")

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
            with contextlib.suppress(Exception):
                expected_tracks = json.loads(inv["expected_tracks_json"])
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

    # If manifests exist and no chunks missing, ensure open assembly tails are flushed
    if manifests_found and not missing_chunks:
        with contextlib.suppress(Exception):
            repo.flush_turn_assembly(interview_id)
        with repo.db.transaction() as conn:
            job_rows = conn.execute(
                "SELECT id, type, status FROM jobs WHERE interview_id = ?",
                (interview_id,),
            ).fetchall()

    stt_relevant = [r for r in job_rows if r["type"] in ("TRANSCRIBE_AUDIO", "TRANSCRIBE_TURN")]
    pending_jobs = [r for r in stt_relevant if r["status"] in ("PENDING", "PROCESSING")]
    failed_jobs = [r for r in stt_relevant if r["status"] == "FAILED"]
    completed_jobs = [r for r in stt_relevant if r["status"] == "COMPLETED"]

    assembly_completed = [r for r in job_rows if r["type"] == "TRANSCRIBE_AUDIO" and r["status"] == "COMPLETED"]

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
                    "details": f"{len(failed_jobs)} audio transcription / assembly jobs failed.",
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
                    "details": f"STT transcription in progress ({len(completed_jobs)}/{len(stt_relevant)} completed, {len(pending_jobs)} pending).",
                    "missing_chunks": {},
                    "stt_jobs": {"completed": len(completed_jobs), "pending": len(pending_jobs), "failed": len(failed_jobs)},
                    "manifests": manifests_found,
                },
            )

        if total_expected_chunks > 0 and len(assembly_completed) < len(db_chunks):
            return (
                False,
                {
                    "is_ready": False,
                    "state": "STT_IN_PROGRESS",
                    "details": f"Awaiting STT job execution for {len(db_chunks) - len(assembly_completed)} chunks.",
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
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    expected_tracks = ["candidate", "interviewer"]
    if inv.get("expected_tracks_json"):
        with contextlib.suppress(Exception):
            expected_tracks = json.loads(inv["expected_tracks_json"])
    elif inv.get("capture_mode") == "single_source":
        expected_tracks = ["shared"]

    base_spool = Path(spool_dir).resolve()
    interview_dir = (base_spool / interview_id).resolve()
    try:
        interview_dir.relative_to(base_spool)
    except (ValueError, RuntimeError):
        raise HTTPException(status_code=400, detail="Spool path traversal detected")

    if (base_spool / interview_id).is_symlink():
        raise HTTPException(status_code=400, detail="Symlinks in spool are forbidden")

    if payload and payload.manifests:
        for manifest in payload.manifests:
            if manifest.interview_id != interview_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"Manifest interview_id '{manifest.interview_id}' does not match URL '{interview_id}'",
                )
            track_val = manifest.track_id.value if hasattr(manifest.track_id, "value") else str(manifest.track_id)
            if track_val not in expected_tracks:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unexpected track '{track_val}' for interview {interview_id}. Expected: {expected_tracks}",
                )
            if manifest.total_chunks < 0 or manifest.total_duration_ms < 0 or manifest.total_samples < 0 or manifest.dropped_samples < 0:
                raise HTTPException(status_code=422, detail="Manifest counters cannot be negative")

            track_path = base_spool / interview_id / track_val
            if track_path.is_symlink():
                raise HTTPException(status_code=400, detail="Symlinks in spool are forbidden")
            try:
                track_path.resolve().relative_to(base_spool)
            except (ValueError, RuntimeError):
                raise HTTPException(status_code=400, detail="Spool path traversal detected")

        # Atomic writes of all manifests
        interview_dir.mkdir(parents=True, exist_ok=True)
        for manifest in payload.manifests:
            track_val = manifest.track_id.value if hasattr(manifest.track_id, "value") else str(manifest.track_id)
            track_path = interview_dir / track_val
            if track_path.is_symlink():
                raise HTTPException(status_code=400, detail="Symlinks in spool are forbidden")
            track_path.mkdir(parents=True, exist_ok=True)
            manifest_path = track_path / "manifest.json"
            tmp_path = track_path / f"manifest.json.tmp.{uuid.uuid4().hex}"
            data = manifest.model_dump_json(indent=2)
            tmp_path.write_text(data, encoding="utf-8")
            os.replace(tmp_path, manifest_path)

    # Flush open speech turns in the turn assembler on interview stop
    with contextlib.suppress(Exception):
        repo.flush_turn_assembly(interview_id)

    if current_status != new_status:
        repo.update_interview_status(interview_id, new_status)
        repo.record_audit_event(
            event_id=f"audit-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            event_type="INTERVIEW_STOPPED",
            payload={"from": current_status.value, "to": new_status.value},
        )

    return {"status": new_status.value}



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
            expected_revision_id=payload.expected_revision_id,
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
            expected_revision_id=payload.expected_revision_id,
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

    if payload.type == "GENERATE_FOLLOWUPS":
        raise HTTPException(
            status_code=400,
            detail="GENERATE_FOLLOWUPS must be requested via dedicated /followups/generate endpoint",
        )

    if payload.type == "EVALUATE_QUESTION":
        q_id = payload.payload.get("question_id")
        plan = repo.get_latest_plan(interview_id)
        if plan and "payload" in plan and "questions" in plan["payload"]:
            known_q_ids = {q["id"] for q in plan["payload"]["questions"]}
            if q_id not in known_q_ids:
                raise HTTPException(status_code=400, detail=f"Question '{q_id}' not found in interview plan")
        # Fix active revisions on server and strip arbitrary candidate_text
        rubric_rev = payload.payload.get("rubric_revision_id") or inv.get("active_rubric_revision_id") or "rub-rev-1"
        transcript_rev = payload.payload.get("transcript_revision_id") or inv.get("active_transcript_revision_id") or "trans-rev-1"
        payload.payload["rubric_revision_id"] = rubric_rev
        payload.payload["transcript_revision_id"] = transcript_rev
        payload.payload.pop("candidate_text", None)

        all_segments = repo.get_transcript_segments(interview_id, revision_id=transcript_rev)
        assocs = repo.get_associations(interview_id, revision_id=transcript_rev)

        associated_seg_ids = {a["segment_id"] for a in assocs if a.get("question_id") == q_id}
        candidate_segments = [
            s for s in all_segments
            if s["id"] in associated_seg_ids and is_candidate_segment(s)
        ]

        if not candidate_segments and plan and "payload" in plan and all_segments:
            questions = plan["payload"].get("questions", [])
            if questions:
                matcher = QuestionMatcher()
                match_results = matcher.associate_segments(questions, all_segments, existing_associations=assocs)
                matched_ids = {r.segment_id for r in match_results if r.question_id == q_id}
                candidate_segments = [s for s in all_segments if s["id"] in matched_ids and is_candidate_segment(s)]

        candidate_fp = compute_candidate_fingerprint(candidate_segments, rubric_rev, transcript_rev)
        payload.payload["candidate_fingerprint"] = candidate_fp

        existing_job = repo.find_active_evaluate_job(
            interview_id=interview_id,
            question_id=q_id,
            candidate_fingerprint=candidate_fp,
            rubric_revision_id=rubric_rev,
            transcript_revision_id=transcript_rev,
        )
        if existing_job:
            return {
                "status": "already_queued",
                "job_id": existing_job["id"],
                "existing": True,
            }

    try:
        repo.enqueue_job(
            job_id=payload.id,
            job_type=payload.type,
            interview_id=interview_id,
            payload=payload.payload,
            max_attempts=payload.max_attempts,
        )
        return {"status": "enqueued", "job_id": payload.id, "existing": False}
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))



@app.get("/api/v1/interviews/{interview_id}/jobs/status")
async def get_jobs_status_endpoint(
    interview_id: str,
    job_ids: list[str] | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    if job_ids:
        for jid in job_ids:
            validate_safe_id(jid, "job_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    return repo.get_interview_jobs_status(interview_id, job_ids=job_ids, limit=limit)



# ---------------------------------------------------------------------------
# Adaptive Follow-up and Guiding Questions Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/v1/interviews/{interview_id}/followups", response_model=FollowUpsStateResponse)
async def get_followups_endpoint(
    interview_id: str,
    question_id: str,
    mode: FollowUpMode = FollowUpMode.PROBE,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    validate_safe_id(question_id, "question_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    plan = repo.get_latest_plan(interview_id)
    plan_payload = plan["payload"] if plan else None
    if not plan_payload or "questions" not in plan_payload:
        raise HTTPException(status_code=400, detail="Interview plan not available")

    q_ids = {q.get("id") for q in plan_payload["questions"]}
    if question_id not in q_ids:
        raise HTTPException(status_code=400, detail=f"Question '{question_id}' not found in plan")

    active_trans_rev = inv.get("active_transcript_revision_id") or "trans-rev-1"
    active_rub_rev = inv.get("active_rubric_revision_id") or "rub-rev-1"
    segments = repo.get_transcript_segments(interview_id, revision_id=active_trans_rev)
    associations = repo.get_associations(interview_id, revision_id=active_trans_rev)
    history = repo.get_asked_followup_history(interview_id)

    fu_context = build_followup_context(
        interview_id=interview_id,
        question_id=question_id,
        mode=mode,
        rubric_questions=plan_payload["questions"],
        transcript_segments=segments,
        existing_associations=associations,
        decisions_history=history,
        role_title=inv.get("role") or "",
        active_rubric_revision_id=active_rub_rev,
        active_transcript_revision_id=active_trans_rev,
    )

    state = repo.get_followup_state(
        interview_id=interview_id,
        question_id=question_id,
        mode=mode.value if hasattr(mode, "value") else str(mode),
    )

    # A guide hint exists precisely for a candidate who is stuck, so it must stay available
    # without a candidate answer. Probe suggestions still need something to probe, and an
    # oversized context still blocks both modes.
    # Наводящий вопрос нужен именно тогда, когда кандидат затрудняется, поэтому доступен и без
    # ответа; уточняющие вопросы по-прежнему требуют ответа, а переросший контекст блокирует оба.
    is_guide = mode == FollowUpMode.GUIDE
    if not fu_context.candidate_segments and not is_guide:
        state["can_generate"] = False
        has_shared_unknown = any(
            str(s.get("track_id", "")).lower() == "shared" and (s.get("speaker_role") or "unknown").lower() == "unknown"
            for s in segments
        )
        if has_shared_unknown:
            state["wait_reason"] = "needs_role_assignment"
        elif any(is_candidate_segment(s) for s in segments):
            state["wait_reason"] = "needs_association"
        else:
            state["wait_reason"] = "waiting_for_candidate"
    elif fu_context.is_context_too_large:
        state["can_generate"] = False
        state["wait_reason"] = "context_too_large"

    state["candidate_fingerprint"] = fu_context.candidate_fingerprint
    state["context_hash"] = fu_context.context_hash

    latest_req = state.get("latest_request")
    state["active_request_id"] = latest_req.get("id") if latest_req else None
    job_info = latest_req.get("job") if latest_req else None
    is_job_failed = bool(job_info and job_info.get("status") == "FAILED")
    is_req_failed = bool(latest_req and latest_req.get("outcome") in ("failed", "error"))

    if state.get("wait_reason") == "generating":
        state["status"] = "processing"
    elif is_req_failed or is_job_failed:
        state["status"] = "error"
    else:
        state["status"] = "idle"

    error_msg = None
    if is_req_failed or is_job_failed:
        err_code = (latest_req.get("error_code") if latest_req else None) or ""
        raw_job_err = (job_info.get("error_message") if job_info else None) or ""
        combined = f"{err_code} {raw_job_err}".lower()
        if "auth" in combined:
            error_msg = "Ошибка авторизации провайдера модели"
        elif "rate" in combined or "429" in combined:
            error_msg = "Превышен лимит запросов к модели (rate limit)"
        elif "timeout" in combined:
            error_msg = "Превышено время ожидания ответа модели"
        elif "validation" in combined:
            error_msg = "Ответ модели не соответствует требуемому формату"
        else:
            error_msg = "Сбой генерации подсказок"
    state["error_message"] = error_msg

    cd = state.get("cooldown_remaining_sec") or 0.0
    state["cooldown_seconds_remaining"] = int(math.ceil(cd))

    return state


@app.post("/api/v1/interviews/{interview_id}/followups/generate")
async def generate_followups_endpoint(
    interview_id: str,
    payload: GenerateFollowUpsRequest,
    response: Response,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    validate_safe_id(payload.question_id, "question_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    status_str = str(inv["status"]).lower()
    if status_str in ("finalized", "deleted"):
        raise HTTPException(status_code=409, detail=f"Interview is {status_str} and immutable")
    if status_str in ("processing", "review"):
        raise HTTPException(status_code=409, detail=f"Cannot generate followups in status '{status_str}'")

    if payload.trigger == FollowUpTrigger.AUTO and status_str != "recording":
        raise HTTPException(status_code=409, detail="Auto follow-ups only permitted in recording status")
    if payload.trigger == FollowUpTrigger.MANUAL and status_str not in ("recording", "paused"):
        raise HTTPException(status_code=409, detail="Manual follow-ups only permitted in recording or paused status")

    active_trans_rev = inv.get("active_transcript_revision_id") or "trans-rev-1"
    active_rub_rev = inv.get("active_rubric_revision_id") or "rub-rev-1"

    if payload.expected_rubric_revision_id and payload.expected_rubric_revision_id != active_rub_rev:
        raise HTTPException(status_code=409, detail="Rubric revision conflict")
    if payload.expected_transcript_revision_id and payload.expected_transcript_revision_id != active_trans_rev:
        raise HTTPException(status_code=409, detail="Transcript revision conflict")

    plan = repo.get_latest_plan(interview_id)
    plan_payload = plan["payload"] if plan else None
    if not plan_payload or "questions" not in plan_payload:
        raise HTTPException(status_code=400, detail="Interview plan not available")

    q_ids = {q.get("id") for q in plan_payload["questions"]}
    if payload.question_id not in q_ids:
        raise HTTPException(status_code=400, detail=f"Question '{payload.question_id}' not found in plan")

    segments = repo.get_transcript_segments(interview_id, revision_id=active_trans_rev)
    associations = repo.get_associations(interview_id, revision_id=active_trans_rev)
    history = repo.get_asked_followup_history(interview_id)

    fu_context = build_followup_context(
        interview_id=interview_id,
        question_id=payload.question_id,
        mode=payload.mode,
        rubric_questions=plan_payload["questions"],
        transcript_segments=segments,
        existing_associations=associations,
        decisions_history=history,
        role_title=inv.get("role") or "",
        active_rubric_revision_id=active_rub_rev,
        active_transcript_revision_id=active_trans_rev,
    )

    if fu_context.is_context_too_large:
        raise HTTPException(status_code=400, detail="Candidate response context is too large for follow-up generation")

    # Guide may run before the candidate answers; probe may not.
    # Наводящий вопрос допустим до ответа кандидата, уточняющий — нет.
    if not fu_context.candidate_segments and payload.mode != FollowUpMode.GUIDE:
        has_shared_unknown = any(
            str(s.get("track_id", "")).lower() == "shared" and (s.get("speaker_role") or "unknown").lower() == "unknown"
            for s in segments
        )
        reason = "needs_role_assignment" if has_shared_unknown else "waiting_for_candidate"
        return {"status": "waiting", "wait_reason": reason, "can_generate": False}

    try:
        req_dict, is_new, wait_reason, cooldown_remaining = repo.create_followup_request_and_job(
            interview_id=interview_id,
            question_id=payload.question_id,
            mode=payload.mode.value if hasattr(payload.mode, "value") else str(payload.mode),
            trigger=payload.trigger.value if hasattr(payload.trigger, "value") else str(payload.trigger),
            candidate_fingerprint=fu_context.candidate_fingerprint,
            context_hash=fu_context.context_hash,
            context_json=fu_context.context_json,
            rubric_revision_id=fu_context.rubric_revision_id,
            transcript_revision_id=fu_context.transcript_revision_id,
            cooldown_sec=30,
        )
    except RepositoryConflictError as err:
        raise HTTPException(status_code=409, detail=str(err))

    if wait_reason == "cooldown_active":
        return {
            "status": "cooldown_active",
            "wait_reason": "cooldown_active",
            "cooldown_remaining_sec": cooldown_remaining,
        }

    if wait_reason == "generating":
        return {
            "status": "generating",
            "wait_reason": "generating",
            "request": req_dict,
        }

    mode_str = payload.mode.value if hasattr(payload.mode, "value") else str(payload.mode)
    if is_new:
        response.status_code = 202
        return {
            "status": "enqueued",
            "request_id": req_dict["id"],
            "job_id": req_dict["job_id"],
            "mode": mode_str,
            "is_cached": False,
        }
    return {
        "status": "cached",
        "request_id": req_dict["id"],
        "job_id": req_dict.get("job_id"),
        "mode": mode_str,
        "is_cached": True,
        "outcome": req_dict.get("outcome"),
    }


@app.patch("/api/v1/interviews/{interview_id}/followups/{suggestion_id}")
async def patch_followup_suggestion_endpoint(
    interview_id: str,
    suggestion_id: str,
    payload: PatchFollowUpSuggestionRequest,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    validate_safe_id(suggestion_id, "suggestion_id")
    try:
        updated = repo.update_suggestion_decision(
            interview_id=interview_id,
            suggestion_id=suggestion_id,
            status=payload.status.value if hasattr(payload.status, "value") else str(payload.status),
            asked_text=payload.asked_text,
            expected_decision_version=payload.expected_decision_version,
            expected_rubric_revision_id=payload.expected_rubric_revision_id,
            expected_transcript_revision_id=payload.expected_transcript_revision_id,
        )
        return updated
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err))
    except RepositoryConflictError as err:
        raise HTTPException(status_code=409, detail=str(err))


@app.post("/api/v1/interviews/{interview_id}/followups/requests/{request_id}/retry")
async def retry_followup_request_endpoint(
    interview_id: str,
    request_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    validate_safe_id(request_id, "request_id")
    try:
        retried = repo.retry_followup_request(interview_id=interview_id, request_id=request_id)
        return {
            "status": "retried",
            "request_id": retried["id"],
            "job_id": retried.get("job_id"),
            "request": retried,
        }
    except KeyError as err:
        raise HTTPException(status_code=404, detail=str(err))
    except RepositoryConflictError as err:
        raise HTTPException(status_code=409, detail=str(err))



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


@app.get("/api/v1/interviews/{interview_id}/revisions/reports")
async def list_report_revisions_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    return repo.get_report_revisions(interview_id)


@app.get("/api/v1/interviews/{interview_id}/plan/readiness")
async def get_interview_plan_readiness_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    validate_safe_id(interview_id, "interview_id")
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")
    plan = repo.get_latest_plan(interview_id)
    if not plan or not plan.get("payload"):
        return {"is_ready": False, "errors": ["План собеседования отсутствует или пуст"]}
    try:
        InterviewPlan.model_validate(plan["payload"])
        return {"is_ready": True, "errors": []}
    except Exception as e:
        return {"is_ready": False, "errors": [str(e)]}


@app.get("/api/v1/interviews/{interview_id}/export")
async def export_interview_endpoint(
    interview_id: str,
    revision_number: int | None = None,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    if revision_number is not None:
        revisions = repo.get_report_revisions(interview_id)
        rev = next((r for r in revisions if r.get("revision_number") == revision_number), None)
        if not rev:
            raise HTTPException(status_code=404, detail=f"Report revision {revision_number} not found")
        snapshot = rev.get("canonical_snapshot") or {}
        return {
            "interview_id": inv["id"],
            "title": inv["title"],
            "candidate_name": inv["candidate_name"],
            "role": inv["role"],
            "status": "FINALIZED",
            "is_draft": False,
            "revision_number": revision_number,
            "final_score_100": rev["final_score_100"],
            "coverage_percentage": rev["coverage_percentage"],
            "sha256_checksum": rev["sha256_checksum"],
            "snapshot": snapshot,
            "report_revision": rev,
            "summary": snapshot.get("executive_summary") or rev.get("summary_markdown"),
            "human_assessments": snapshot.get("human_assessments"),
            "followup_questions": snapshot.get("followup_questions", []),
            "audit_trail": repo.get_audit_events(interview_id),
        }

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
            "followup_questions": snapshot.get("followup_questions", []),
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
        "followup_questions": repo.get_asked_followup_history(interview_id),
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

    active_rev = inv.get("active_transcript_revision_id") or "trans-rev-1"
    old_rev = payload.old_revision_id or active_rev
    if payload.old_revision_id and payload.old_revision_id != active_rev:
        raise HTTPException(
            status_code=409,
            detail=f"Active transcript revision is '{active_rev}', but request specified '{payload.old_revision_id}'",
        )

    job_id = f"job-batch-{uuid.uuid4().hex[:8]}"
    try:
        repo.enqueue_job(
            job_id=job_id,
            job_type="BATCH_RETRANSCRIBE",
            interview_id=interview_id,
            payload={
                "new_revision_id": payload.new_revision_id,
                "old_revision_id": old_rev,
                "segments": payload.segments,
                "provenance": payload.provenance,
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
            expected_transcript_revision=payload.expected_transcript_revision,
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

    try:
        repo.reassociate_segment(
            interview_id=interview_id,
            segment_id=segment_id,
            new_question_id=payload.new_question_id,
            notes=payload.notes,
            expected_revision_id=payload.expected_revision_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RepositoryConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))

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
        raise HTTPException(status_code=500, detail=f"Backup failed: {e!s}")


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


@app.get("/api/v1/system/models")
async def get_system_models_endpoint(
    repo: Repository = Depends(get_repository),
):
    """
    Returns the provider and model profiles that live work actually uses.
    Uses the effective AI settings resolver.
    """
    cred_store = CredentialStore()
    settings_resp, runtime_config = resolve_ai_settings(repo, cred_store)
    return {
        "llm": {
            "provider_id": runtime_config.llm_provider.id,
            "provider_name": runtime_config.llm_provider.name,
            "model_profile_id": runtime_config.llm_primary.id,
            "upstream_model_id": runtime_config.llm_primary.upstream_model_id,
            "fallback_models": [m.upstream_model_id for m in runtime_config.llm_fallbacks],
        },
        "stt": {
            "provider_id": runtime_config.stt_provider.id,
            "provider_name": runtime_config.stt_provider.name,
            "model_profile_id": runtime_config.stt_profile.id,
            "upstream_model_id": runtime_config.stt_profile.model_id,
        },
        "settings_revision": runtime_config.revision,
        "settings_source": settings_resp.source,
    }


# -------------------------------------------------------------
# AI Provider & Model Settings Endpoints
# -------------------------------------------------------------


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlparse(url)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port


def _validate_preserved_credential_origin(
    *,
    label: str,
    old_url: str,
    new_url: str,
    action: ApiKeyAction,
    credential_configured: bool,
    credential_source: str,
) -> None:
    if (
        action == ApiKeyAction.PRESERVE
        and credential_configured
        and credential_source != "process_environment"
        and _url_origin(old_url) != _url_origin(new_url)
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{label} provider origin changed; choose replace or clear for its API key "
                "instead of preserving the credential from the previous origin"
            ),
        )


def _probe_api_key(
    *,
    candidate_key: str | None,
    auth_mode: AuthMode,
    candidate_url: str,
    active_url: str,
    active_key: str | None,
) -> str | None:
    if auth_mode == AuthMode.NONE:
        return None
    if candidate_key:
        return candidate_key
    if _url_origin(candidate_url) == _url_origin(active_url) and active_key:
        return active_key
    raise HTTPException(
        status_code=422,
        detail="An API key is required to test a Bearer-authenticated provider at a different origin",
    )


def _safe_probe_error(exc: Exception) -> str:
    """Returns a useful category without exposing untrusted upstream response content."""
    error_code = exc.__class__.__name__
    if "Authentication" in error_code:
        return "Authentication failed"
    if "RateLimit" in error_code:
        return "Provider rate limit exceeded"
    if "ContextLength" in error_code:
        return "Model context limit exceeded"
    if "InvalidResponse" in error_code:
        return "Provider returned an invalid structured response"
    if "Transient" in error_code:
        return "Provider is temporarily unavailable"
    return "Probe request failed"


@app.get("/api/v1/settings/ai", response_model=AiSettingsResponse)
async def get_ai_settings_endpoint(
    repo: Repository = Depends(get_repository),
) -> AiSettingsResponse:
    """
    Returns the effective AI settings for STT and LLM.
    Secrets are NEVER returned in the response.
    """
    cred_store = CredentialStore()
    response, _ = resolve_ai_settings(repo, cred_store)
    return response


@app.put("/api/v1/settings/ai", response_model=AiSettingsResponse)
async def update_ai_settings_endpoint(
    req: UpdateAiSettingsRequest,
    repo: Repository = Depends(get_repository),
) -> AiSettingsResponse:
    """
    Updates the AI settings with OCC revision check and blockers validation.
    Stores non-secret configs in SQLite and writes versioned credentials snapshot to disk.
    """
    cred_store = CredentialStore()
    next_revision = req.expected_revision + 1

    # Snapshot publication and DB activation must be serialized in this process.
    # The snapshot store additionally refuses to replace an existing revision,
    # protecting the active credential file from stale or cross-process writers.
    with _ai_settings_update_lock:
        current, _ = resolve_ai_settings(repo, cred_store)
        if current.revision != req.expected_revision:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"AI settings conflict: expected revision {req.expected_revision}, "
                    f"but current revision is {current.revision}"
                ),
            )
        if current.update_blocker:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot update AI settings: active work present ({current.update_blocker})",
            )

        _validate_preserved_credential_origin(
            label="STT",
            old_url=current.transcription.endpoint_url,
            new_url=req.transcription.endpoint_url,
            action=req.stt_api_key.action,
            credential_configured=current.stt_credentials.configured,
            credential_source=current.stt_credentials.source,
        )
        _validate_preserved_credential_origin(
            label="LLM",
            old_url=current.text_analysis.base_url,
            new_url=req.text_analysis.base_url,
            action=req.llm_api_key.action,
            credential_configured=current.llm_credentials.configured,
            credential_source=current.llm_credentials.source,
        )

        snapshot_created = False
        try:
            stt_val = req.stt_api_key.value.get_secret_value() if req.stt_api_key.value else None
            llm_val = req.llm_api_key.value.get_secret_value() if req.llm_api_key.value else None
            cred_store.prepare_snapshot(
                expected_revision=req.expected_revision,
                stt_action=req.stt_api_key.action,
                stt_value=stt_val,
                llm_action=req.llm_api_key.action,
                llm_value=llm_val,
            )
            snapshot_created = True
        except FileExistsError:
            raise HTTPException(
                status_code=409,
                detail=f"Credentials snapshot for revision {next_revision} already exists",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        try:
            repo.update_ai_settings(
                expected_revision=req.expected_revision,
                transcription_config=req.transcription.model_dump(mode="json"),
                text_analysis_config=req.text_analysis.model_dump(mode="json"),
            )
        except RepositoryConflictError as exc:
            if snapshot_created:
                cred_store.cleanup_snapshot(next_revision)
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception:
            if snapshot_created:
                cred_store.cleanup_snapshot(next_revision)
            logger.exception("Failed to update AI settings in DB")
            raise HTTPException(status_code=500, detail="Failed to persist AI settings")

    # Prune old snapshot files (keeps current and previous)
    cred_store.prune_old_snapshots(next_revision)

    response, _ = resolve_ai_settings(repo, cred_store)
    return response


@app.post("/api/v1/settings/ai/test", response_model=TestAiSettingsResponse)
async def test_ai_settings_endpoint(
    req: TestAiSettingsRequest,
    repo: Repository = Depends(get_repository),
) -> TestAiSettingsResponse:
    """
    Tests connectivity and configuration for STT or LLM without persisting settings.
    Uses candidate config and optional candidate API key.
    """
    cred_store = CredentialStore()
    t0 = time.perf_counter()

    if req.target == TestTarget.STT:
        if not req.transcription:
            raise HTTPException(
                status_code=422,
                detail="transcription settings must be provided when target is 'stt'",
            )
        t_cfg = req.transcription
        active, runtime_config = resolve_ai_settings(repo, cred_store)
        candidate_key = req.api_key.get_secret_value() if req.api_key else None
        api_key = _probe_api_key(
            candidate_key=candidate_key,
            auth_mode=t_cfg.auth_mode,
            candidate_url=t_cfg.endpoint_url,
            active_url=active.transcription.endpoint_url,
            active_key=runtime_config.stt_credentials.api_key,
        )

        stt_profile = STTProfile(
            id=f"test-stt-{t_cfg.provider_id}",
            name=t_cfg.provider_name,
            endpoint_url=t_cfg.endpoint_url,
            protocol=STTProtocol.BATCH,
            model_id=t_cfg.model_id,
            timeout_seconds=t_cfg.timeout_seconds,
        )
        # Synthetic WAV: 1 second of 16kHz mono 16-bit PCM silence (32000 bytes)
        silence_pcm = b"\x00" * 32000
        wav_bytes = pcm_s16le_to_wav_bytes(silence_pcm, sample_rate=16000, channels=1)

        adapter = OpenAICompatibleSTTAdapter(
            profile=stt_profile,
            api_key=api_key,
            auth_mode=t_cfg.auth_mode,
        )

        try:
            res = await adapter.transcribe_audio(
                wav_bytes,
                filename="test_probe.wav",
                content_type="audio/wav",
                language=t_cfg.language,
                max_retries=1,
                timeout_seconds=min(15.0, t_cfg.timeout_seconds),
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            actual_model = getattr(res, "model_id", t_cfg.model_id)
            return TestAiSettingsResponse(
                success=True,
                target=TestTarget.STT,
                provider_id=t_cfg.provider_id,
                model_id=actual_model,
                latency_ms=latency_ms,
                message="STT probe successful (synthetic audio accepted; microphone/quality not verified)",
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            err_code = exc.__class__.__name__
            clean_msg = _safe_probe_error(exc)
            return TestAiSettingsResponse(
                success=False,
                target=TestTarget.STT,
                provider_id=t_cfg.provider_id,
                model_id=t_cfg.model_id,
                latency_ms=latency_ms,
                error_code=err_code,
                message=f"STT probe failed: {clean_msg}",
            )

    elif req.target == TestTarget.LLM:
        if not req.text_analysis:
            raise HTTPException(
                status_code=422,
                detail="text_analysis settings must be provided when target is 'llm'",
            )
        llm_cfg = req.text_analysis
        active, runtime_config = resolve_ai_settings(repo, cred_store)
        candidate_key = req.api_key.get_secret_value() if req.api_key else None
        api_key = _probe_api_key(
            candidate_key=candidate_key,
            auth_mode=llm_cfg.auth_mode,
            candidate_url=llm_cfg.base_url,
            active_url=active.text_analysis.base_url,
            active_key=runtime_config.llm_credentials.api_key,
        )

        provider = ProviderProfile(
            id=llm_cfg.provider_id,
            name=llm_cfg.provider_name,
            base_url=llm_cfg.base_url,
            api_key_env="NEBULA_LLM_API_KEY",
            timeout_seconds=min(15.0, llm_cfg.timeout_seconds),
            max_concurrency=llm_cfg.max_concurrency,
        )
        model = to_model_profile(llm_cfg.provider_id, llm_cfg.primary_model)

        adapter = OpenAICompatibleAdapter(
            provider=provider,
            model=model,
            api_key=api_key,
            auth_mode=llm_cfg.auth_mode,
        )

        messages = [
            {"role": "system", "content": "Respond with a JSON object: {\"status\": \"ok\"}"},
            {"role": "user", "content": "Test connectivity."},
        ]
        json_schema = {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        }

        try:
            await adapter.execute_request(
                messages=messages,
                json_schema=json_schema,
                schema_name="probe_test",
                max_retries=1,
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)
            actual_model = getattr(adapter.model, "upstream_model_id", llm_cfg.primary_model.model_id)
            return TestAiSettingsResponse(
                success=True,
                target=TestTarget.LLM,
                provider_id=llm_cfg.provider_id,
                model_id=actual_model,
                latency_ms=latency_ms,
                message="LLM probe successful (connectivity and structured output verified)",
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            err_code = exc.__class__.__name__
            clean_msg = _safe_probe_error(exc)
            return TestAiSettingsResponse(
                success=False,
                target=TestTarget.LLM,
                provider_id=llm_cfg.provider_id,
                model_id=llm_cfg.primary_model.model_id,
                latency_ms=latency_ms,
                error_code=err_code,
                message=f"LLM probe failed: {clean_msg}",
            )
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported target: {req.target}")
