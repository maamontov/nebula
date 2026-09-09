"""
FastAPI entry point for Nebula backend.
Provides endpoints for health check, scoring, proposal validation, and lifecycle.
"""
import hashlib
import json
import uuid
import os
from typing import Any
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

# Enable CORS for desktop UI (localhost:1420, tauri://localhost, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


class BatchRetranscribeRequest(BaseModel):
    new_revision_id: str = "trans-rev-2"
    old_revision_id: str = "trans-rev-1"
    segments: list[dict[str, Any]] = Field(default_factory=list)


class ReviewAssessmentWithRevisionRequest(BaseModel):
    expected_transcript_revision: str
    scores: list[dict[str, Any]]
    reviewer_notes: str | None = None
    reviewer_id: str | None = "interviewer-1"
    is_manually_adjusted: bool = True


class ConfirmSummaryRequest(BaseModel):
    reviewer_id: str = "interviewer-1"
    confirmed_markdown: str
    confirmed_recommendation: str


class FinalizeReportRequest(BaseModel):
    confirmed_by: str = "interviewer-1"
    summary_markdown: str | None = None
    hiring_recommendation: str | None = None


class CreateBackupRequest(BaseModel):
    target_path: str


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


@app.delete("/api/v1/interviews/{interview_id}")
async def delete_interview_endpoint(
    interview_id: str,
    spool_dir: str = os.getenv("NEBULA_SPOOL_DIR", "./spool"),
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    deleted = repo.delete_interview(interview_id=interview_id, spool_dir=spool_dir)
    return {"status": "deleted", "interview_id": interview_id, "success": deleted}


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

    latest_report = repo.get_latest_report_revision(interview_id)
    summary_prop = repo.get_summary_proposal(interview_id)

    return {
        "interview_id": inv["id"],
        "title": inv["title"],
        "candidate_name": inv["candidate_name"],
        "role": inv["role"],
        "status": inv["status"],
        "is_draft": latest_report is None,
        "plan": plan["payload"] if plan else None,
        "transcript_segments": segments,
        "assessment_proposals": proposals,
        "decisions": decisions,
        "final_score_100": latest_report["final_score_100"] if latest_report else final_score_100,
        "report_revision": latest_report,
        "sha256_checksum": latest_report["sha256_checksum"] if latest_report else None,
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

    job_id = f"job-batch-{uuid.uuid4().hex[:8]}"
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

    # Conflict check: verify expected transcript revision against latest revision
    revs = repo.get_transcript_revisions(interview_id)
    latest_rev = revs[-1]["id"] if revs else "trans-rev-1"

    if payload.expected_transcript_revision != latest_rev:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Revision conflict: active transcript revision is '{latest_rev}', "
                f"but review was submitted for '{payload.expected_transcript_revision}'. "
                "Please refresh and review the updated transcript before submitting."
            ),
        )

    # Save human assessment (immutable record of human decision)
    assessment_id = f"pass-{uuid.uuid4().hex[:8]}"
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
    )

    repo.record_audit_event(
        event_id=f"audit-{uuid.uuid4().hex[:8]}",
        interview_id=interview_id,
        event_type="HUMAN_ASSESSMENT_CONFIRMED",
        payload={
            "assessment_id": assessment_id,
            "question_id": question_id,
            "transcript_revision_id": payload.expected_transcript_revision,
            "is_manually_adjusted": payload.is_manually_adjusted,
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


@app.post("/api/v1/interviews/{interview_id}/report/finalize")
async def finalize_report_endpoint(
    interview_id: str,
    payload: FinalizeReportRequest,
    repo: Repository = Depends(get_repository),
):
    inv = repo.get_interview(interview_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Interview not found")

    plan = repo.get_latest_plan(interview_id)
    questions = plan.get("payload", {}).get("questions", []) if plan else []
    human_assessments = repo.get_human_assessments(interview_id)
    proposals = repo.get_assessment_proposals(interview_id)

    decisions_map: dict[str, Any] = {}
    for p in proposals:
        score_val = p["scores"][0].get("score") if p.get("scores") else None
        decisions_map[p["question_id"]] = {"score": score_val, "is_human": False}
    for h in human_assessments:
        score_val = h["scores"][0].get("score") if h.get("scores") else None
        decisions_map[h["question_id"]] = {"score": score_val, "is_human": True, "notes": h.get("reviewer_notes")}

    total_weighted_score = 0.0
    total_weight = 0.0
    question_scores = {}

    for q in questions:
        q_id = q["question_id"]
        q_weight = float(q.get("weight", 1.0))
        total_weight += q_weight
        dec = decisions_map.get(q_id)
        if dec and dec.get("score") is not None:
            norm_score = float(dec["score"]) / 5.0
            total_weighted_score += norm_score * q_weight
            question_scores[q_id] = {
                "score": dec["score"],
                "normalized": round(norm_score, 4),
                "is_human_override": dec.get("is_human", False),
            }
        else:
            question_scores[q_id] = {"score": None, "normalized": 0.0, "is_human_override": False}

    final_score_100 = round((total_weighted_score / total_weight) * 100.0, 2) if total_weight > 0 else 0.0
    coverage_pct = round((len([v for v in question_scores.values() if v["score"] is not None]) / len(questions)) * 100.0, 1) if questions else 0.0

    summary_prop = repo.get_summary_proposal(interview_id)
    summary_md = payload.summary_markdown or (summary_prop.get("confirmed_markdown") if summary_prop else "") or "Резюме утверждено."
    rec = payload.hiring_recommendation or (summary_prop.get("confirmed_recommendation") if summary_prop else "HIRE")

    canonical_dict = {
        "interview_id": interview_id,
        "candidate": inv.get("candidate_name"),
        "role": inv.get("role"),
        "final_score_100": final_score_100,
        "coverage_percentage": coverage_pct,
        "question_scores": question_scores,
        "summary": summary_md,
        "recommendation": rec,
        "confirmed_by": payload.confirmed_by,
    }
    canonical_json = json.dumps(canonical_dict, sort_keys=True, ensure_ascii=False)
    sha256_checksum = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    revs = repo.get_report_revisions(interview_id)
    next_rev_num = len(revs) + 1
    report_id = f"rep-rev-{next_rev_num}"

    repo.save_report_revision(
        report_id=report_id,
        interview_id=interview_id,
        revision_number=next_rev_num,
        final_score_100=final_score_100,
        coverage_percentage=coverage_pct,
        question_scores=question_scores,
        summary_markdown=summary_md,
        hiring_recommendation=rec,
        confirmed_by=payload.confirmed_by,
        sha256_checksum=sha256_checksum,
    )

    repo.update_interview_status(interview_id, InterviewStatus.FINALIZED)
    repo.record_audit_event(
        event_id=f"audit-{uuid.uuid4().hex[:8]}",
        interview_id=interview_id,
        event_type="REPORT_FINALIZED_AND_SEALED",
        payload={
            "report_id": report_id,
            "revision_number": next_rev_num,
            "final_score_100": final_score_100,
            "sha256_checksum": sha256_checksum,
            "confirmed_by": payload.confirmed_by,
        },
    )

    return {
        "status": "finalized",
        "report_id": report_id,
        "revision_number": next_rev_num,
        "final_score_100": final_score_100,
        "coverage_percentage": coverage_pct,
        "sha256_checksum": sha256_checksum,
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


# -------------------------------------------------------------
# System & Reliability Maintenance
# -------------------------------------------------------------
@app.post("/api/v1/system/backup")
async def backup_database_endpoint(
    payload: CreateBackupRequest,
    repo: Repository = Depends(get_repository),
):
    try:
        target = repo.db.backup(payload.target_path)
        return {"status": "ok", "target_path": target}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")


@app.get("/api/v1/system/integrity")
async def check_database_integrity_endpoint(
    repo: Repository = Depends(get_repository),
):
    is_ok = repo.db.verify_integrity()
    return {"status": "ok" if is_ok else "corrupted", "integrity_ok": is_ok}

