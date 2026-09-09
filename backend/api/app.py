"""
FastAPI entry point for Nebula backend.
Provides endpoints for health check, scoring, proposal validation, and lifecycle.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from backend.core.evidence_validator import (
    validate_proposal,
)
from backend.core.scoring import (
    ScoringError,
    calculate_interview_score,
)
from backend.core.state_machine import (
    InvalidStateTransitionError,
    transition_status,
)
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


class CalculateScoreRequest(BaseModel):
    rubric: RubricRevision
    assessments: list[HumanQuestionAssessment]


class ValidateProposalRequest(BaseModel):
    proposal: AssessmentProposal
    transcript: TranscriptRevision


class TransitionStateRequest(BaseModel):
    current_status: InterviewStatus
    target_status: InterviewStatus


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": "0.1.0"}


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
