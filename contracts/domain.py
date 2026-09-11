from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field

from contracts.audio import TrackType


class InterviewStatus(str, Enum):
    """
    Lifecycle status of an interview session.
    State transitions:
    DRAFT -> READY -> RECORDING <-> PAUSED -> PROCESSING -> REVIEW -> FINALIZED.
    Any state -> DELETED (tombstone, forbids further background work).
    """
    DRAFT = "draft"
    READY = "ready"
    RECORDING = "recording"
    PAUSED = "paused"
    PROCESSING = "processing"
    REVIEW = "review"
    FINALIZED = "finalized"
    DELETED = "deleted"


from typing import Any

from pydantic import model_validator


class RubricCriterion(BaseModel):
    """Criterion within an interview rubric."""
    id: str = Field(..., description="Unique criterion ID (e.g. 'crit-arch-scale')")
    title: str = Field(..., min_length=1)
    description: str = Field(default="")
    min_score: float = Field(default=1.0, description="Minimum possible score on this scale")
    max_score: float = Field(default=5.0, description="Maximum possible score on this scale")
    weight: float = Field(default=1.0, ge=0.0, description="Relative weight within the question")
    levels_description: dict[int, str] = Field(
        default_factory=dict,
        description="Optional human-readable descriptions of score levels (e.g. {1: 'No understanding', 5: 'Expert'})"
    )


class PlannedQuestion(BaseModel):
    """
    Question planned in the interview blueprint.
    Standardized domain format supporting both id/question_id and title/prompt/text.
    Канонический формат вопроса с поддержкой синонимов id/question_id и title/prompt/text.
    """
    id: str = Field(..., description="Unique question ID (e.g. 'q-system-design')")
    title: str = Field(default="", description="Short question title")
    prompt: str = Field(default="", description="Guide / wording for interviewer")
    text: str | None = Field(default=None, description="Alternative/legacy text field")
    order_index: int = Field(default=0)
    weight: float = Field(default=1.0, ge=0.0, description="Relative weight of this question in the final score")
    criteria: list[RubricCriterion] = Field(..., min_length=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_question_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Normalize question_id -> id
            if "question_id" in data and "id" not in data:
                data["id"] = data["question_id"]
            # Normalize text -> prompt / title
            txt = data.get("text") or ""
            if not data.get("prompt") and txt:
                data["prompt"] = txt
            if not data.get("title"):
                data["title"] = txt[:60] if txt else data.get("prompt", "Question")[:60]
        return data

    @model_validator(mode="after")
    def validate_unique_criteria(self) -> "PlannedQuestion":
        crit_ids = [c.id for c in self.criteria]
        if len(crit_ids) != len(set(crit_ids)):
            raise ValueError(f"Duplicate criterion IDs found in question '{self.id}': {crit_ids}")
        return self


class InterviewPlan(BaseModel):
    """
    Structured interview plan contract.
    Структурированный контракт плана интервью.
    """
    id: str = Field(default="plan-default")
    title: str = Field(..., min_length=2)
    role: str = Field(default="")
    candidate_name: str | None = Field(default=None, description="Candidate name associated with this plan")
    questions: list[PlannedQuestion] = Field(..., min_length=1)


class RubricRevision(BaseModel):
    """Immutable snapshot of the approved interview plan & rubric."""
    revision_id: str
    interview_id: str
    is_approved: bool = Field(default=False)
    approved_by: str | None = None
    questions: list[PlannedQuestion] = Field(..., min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TranscriptSegment(BaseModel):
    """A recognized segment of speech with timestamps and source track."""
    id: str = Field(..., description="Stable segment ID (e.g. 'seg-001')")
    track_id: TrackType
    start_time_ms: int = Field(ge=0)
    end_time_ms: int = Field(ge=0)
    text: str = Field(..., min_length=1)
    is_final: bool = Field(default=True, description="False for UI partials; only True segments enter assessment")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    speaker_role: str = Field(default="unknown")


class TranscriptRevision(BaseModel):
    """Immutable revision of the combined transcript."""
    revision_id: str
    interview_id: str
    segments: list[TranscriptSegment] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class QuestionAnswerLink(BaseModel):
    """Association between a planned/detected question and candidate response segments."""
    question_id: str
    segment_ids: list[str] = Field(default_factory=list)
    is_manually_adjusted: bool = False
    notes: str | None = None


class EvidenceRef(BaseModel):
    """Direct reference to a transcript segment proving a scoring claim."""
    segment_id: str
    exact_quote: str = Field(..., min_length=1, description="Exact text verbatim from segment; validated programmatically")
    start_char: int | None = None
    end_char: int | None = None


class CriterionScoreProposal(BaseModel):
    """AI proposal for a single criterion."""
    criterion_id: str
    score: float | None = Field(
        default=None,
        description="Proposed score on criterion scale, or null if insufficient data/not answered"
    )
    explanation: str = Field(..., min_length=1)
    evidence: list[EvidenceRef] = Field(
        default_factory=list,
        description="Verifiable quotes from transcript. Model confidence is not a proof; evidence is."
    )
    requires_human_review: bool = Field(
        default=False,
        description="Flagged if ambiguous, conflicting evidence, or low confidence"
    )
    review_reason: str | None = None


class AssessmentProposal(BaseModel):
    """
    AI-generated assessment for a specific question response.
    Never overwrites human decisions; strictly an immutable recommendation.
    """
    id: str
    interview_id: str
    question_id: str
    rubric_revision_id: str
    transcript_revision_id: str
    model_profile_id: str
    scores: list[CriterionScoreProposal] = Field(..., min_length=1)
    critical_errors: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HumanCriterionScore(BaseModel):
    """Final or overridden score decided by the interviewer."""
    criterion_id: str
    score: float | None = Field(default=None, description="Confirmed score or null if excluded/not observed")
    is_excluded: bool = Field(default=False, description="True if interviewer explicitly excluded this criterion")
    exclusion_reason: str | None = None
    override_reason: str | None = Field(default=None, description="Explanation if changed from AI proposal")


class HumanQuestionAssessment(BaseModel):
    """Interviewer confirmation for a question."""
    question_id: str
    is_excluded: bool = Field(default=False)
    exclusion_reason: str | None = None
    criteria_scores: list[HumanCriterionScore] = Field(default_factory=list)
    reviewer_notes: str | None = None


class ReportRevision(BaseModel):
    """
    Immutable confirmed final interview summary and deterministic score snapshot.
    """
    revision_id: str
    interview_id: str
    final_score: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Deterministic score on 0-100 scale, or null if no valid criteria were assessed"
    )
    coverage_percentage: float = Field(
        ge=0.0,
        le=100.0,
        description="Percentage of planned questions weight that was actually scored (not excluded)"
    )
    question_scores: dict[str, float | None] = Field(
        default_factory=dict,
        description="Normalized score [0, 1] per question before scale multiplication"
    )
    summary_markdown: str = Field(..., description="Confirmed textual summary of findings and evidence")
    is_confirmed: bool = Field(default=False)
    confirmed_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Re-export follow-up models
from contracts.followups import (  # noqa: E402
    FollowUpKind,
    FollowUpLLMResponse,
    FollowUpLLMSuggestion,
    FollowUpMode,
    FollowUpsStateResponse,
    FollowUpStatus,
    FollowUpSuggestion,
    FollowUpTrigger,
    GenerateFollowUpsRequest,
    PatchFollowUpSuggestionRequest,
)

__all__ = [
    "InterviewStatus",
    "RubricCriterion",
    "PlannedQuestion",
    "InterviewPlan",
    "RubricRevision",
    "TranscriptSegment",
    "TranscriptRevision",
    "QuestionAnswerLink",
    "EvidenceRef",
    "CriterionScoreProposal",
    "AssessmentProposal",
    "HumanCriterionScore",
    "HumanQuestionAssessment",
    "ReportRevision",
    "FollowUpKind",
    "FollowUpLLMResponse",
    "FollowUpLLMSuggestion",
    "FollowUpMode",
    "FollowUpsStateResponse",
    "FollowUpStatus",
    "FollowUpSuggestion",
    "FollowUpTrigger",
    "GenerateFollowUpsRequest",
    "PatchFollowUpSuggestionRequest",
]
