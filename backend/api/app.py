"""
FastAPI entry point for Nebula backend.
Provides endpoints for health check, scoring, proposal validation, and lifecycle.
"""
import uuid
from typing import Any
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend.core.audio_health import AudioHealthMonitor, ChannelMetrics
from backend.core.evidence_validator import (
    validate_proposal,
)
from backend.core.matcher import QuestionMatcher
from backend.core.scoring import (
    ScoringError,
    calculate_interview_score,
)
from backend.core.state_machine import (
    InvalidStateTransitionError,
    transition_status,
)
from backend.db.database import get_db
from backend.db.repository import Repository
from contracts.domain import (
    AssessmentProposal,
    HumanQuestionAssessment,
    InterviewStatus,
    RubricRevision,
    TranscriptRevision,
)

app = FastAPI(
    title="Nebula Backend API",
    version="0.1.0",
    description="Core API for Nebula AI Interview Copilot",
)


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


class AddSegmentRequest(BaseModel):
    id: str
    track_id: str
    start_time_ms: int
    end_time_ms: int
    text: str
    is_final: bool = True


class ApproveAssessmentRequest(BaseModel):
    reviewed_scores: list[dict[str, Any]] | None = None
    reviewer_notes: str | None = None


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
    current_status: InterviewStatus
    target_status: InterviewStatus
    consent_confirmed_at: str | None = None
    consent_version: str | None = None


class ReassociateSegmentRequest(BaseModel):
    new_question_id: str
    notes: str = "Manually reassociated by reviewer"


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
    inv = repo.create_interview(
        interview_id=payload.id,
        title=payload.title,
        candidate_name=payload.candidate_name,
        role=payload.role,
        status=InterviewStatus.DRAFT,
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


@app.get("/api/v1/interviews/{interview_id}")
async def get_interview_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    plan = repo.get_latest_plan(interview_id)
    segments = repo.get_transcript_segments(interview_id)
    proposals = repo.get_assessment_proposals(interview_id)

    return {
        "interview": inv,
        "plan": plan["payload"] if plan else None,
        "transcript_segments": segments,
        "assessment_proposals": proposals,
    }


@app.post("/api/v1/interviews/{interview_id}/status")
async def update_status_endpoint(
    interview_id: str,
    payload: TransitionStateRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

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


@app.post("/api/v1/interviews/{interview_id}/segments")
async def add_segment_endpoint(
    interview_id: str,
    payload: AddSegmentRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    repo.add_transcript_segment(
        segment_id=payload.id,
        interview_id=interview_id,
        track_id=payload.track_id,
        start_time_ms=payload.start_time_ms,
        end_time_ms=payload.end_time_ms,
        text=payload.text,
        is_final=payload.is_final,
    )
    return {"status": "ok", "segment_id": payload.id}


@app.post("/api/v1/interviews/{interview_id}/jobs/enqueue")
async def enqueue_job_endpoint(
    interview_id: str,
    payload: EnqueueJobRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    repo.enqueue_job(
        job_id=payload.id,
        job_type=payload.type,
        interview_id=interview_id,
        payload=payload.payload,
        max_attempts=payload.max_attempts,
    )
    return {"status": "enqueued", "job_id": payload.id}


@app.post("/api/v1/interviews/{interview_id}/assessments/{proposal_id}/approve")
async def approve_assessment_endpoint(
    interview_id: str,
    proposal_id: str,
    payload: ApproveAssessmentRequest,
    repo: Repository = Depends(get_repository),
):
    repo.approve_assessment(
        proposal_id=proposal_id,
        reviewed_scores=payload.reviewed_scores,
        reviewer_notes=payload.reviewer_notes,
    )
    repo.record_audit_event(
        event_id=f"audit-{uuid.uuid4().hex[:8]}",
        interview_id=interview_id,
        event_type="ASSESSMENT_APPROVED",
        payload={"proposal_id": proposal_id, "reviewer_notes": payload.reviewer_notes},
    )
    return {"status": "approved", "proposal_id": proposal_id}


@app.get("/api/v1/interviews/{interview_id}/export")
async def export_interview_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    plan = repo.get_latest_plan(interview_id)
    segments = repo.get_transcript_segments(interview_id)
    proposals = repo.get_assessment_proposals(interview_id)
    audit_events = repo.get_audit_events(interview_id)

    total_score = 0.0
    count = 0
    decisions = []
    for p in proposals:
        score_val = None
        if p.get("reviewed_scores") and len(p["reviewed_scores"]) > 0:
            score_val = p["reviewed_scores"][0].get("score")
        elif p.get("scores") and len(p["scores"]) > 0:
            score_val = p["scores"][0].get("score")

        if score_val is not None:
            total_score += float(score_val)
            count += 1
            decisions.append({
                "proposal_id": p["id"],
                "question_id": p["question_id"],
                "score": score_val,
                "is_approved": bool(p.get("is_approved")),
                "reviewer_notes": p.get("reviewer_notes"),
            })

    final_score_100 = round((total_score / (count * 5.0)) * 100.0, 2) if count > 0 else 0.0

    return {
        "interview_id": inv["id"],
        "title": inv["title"],
        "candidate_name": inv["candidate_name"],
        "role": inv["role"],
        "status": inv["status"],
        "plan": plan["payload"] if plan else None,
        "transcript_segments": segments,
        "assessment_proposals": proposals,
        "decisions": decisions,
        "final_score_100": final_score_100,
        "audit_trail": audit_events,
    }


@app.get("/api/v1/interviews/{interview_id}/associations")
async def get_associations_endpoint(
    interview_id: str,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    assocs = repo.get_associations(interview_id)
    if not assocs:
        plan = repo.get_latest_plan(interview_id)
        segments = repo.get_transcript_segments(interview_id)
        if plan and segments:
            questions = plan.get("payload", {}).get("questions", [])
            matcher = QuestionMatcher()
            results = matcher.associate_segments(questions, segments)
            for r in results:
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
