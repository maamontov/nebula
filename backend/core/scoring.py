"""
Deterministic scoring engine for Nebula interviews.
Adheres strictly to docs/implementation-plan.md Section 7.3:
- Normalizes criterion score to [0, 1] on its respective scale.
- Computes weighted average for question criteria.
- Computes weighted average across answered questions * 100.
- Handles explicit exclusion of questions and criteria by recalculating denominators.
- Computes actual planned weight coverage percentage.
- Does not invent zero scores for missing questions; missing questions result in null or reduced coverage.
"""
import math
from dataclasses import dataclass

from contracts.domain import (
    HumanQuestionAssessment,
    RubricRevision,
)


class ScoringError(Exception):
    """Raised when rubric or assessment contains mathematically invalid configuration."""


@dataclass(frozen=True)
class QuestionScoringResult:
    question_id: str
    normalized_score: float | None  # [0, 1] or None if excluded / no scored criteria
    is_excluded: bool
    active_weight: float
    criterion_normalized_scores: dict[str, float | None]


@dataclass(frozen=True)
class InterviewScoringResult:
    final_score_100: float | None  # [0, 100] or None if no questions scored
    coverage_percentage: float  # [0, 100]%
    total_planned_weight: float
    evaluated_weight: float
    question_results: dict[str, QuestionScoringResult]


def calculate_interview_score(
    rubric: RubricRevision,
    assessments: list[HumanQuestionAssessment],
) -> InterviewScoringResult:
    """
    Computes deterministic interview score and coverage.
    """
    if not rubric.questions:
        raise ScoringError("Rubric must contain at least one question.")

    assessments_map = {a.question_id: a for a in assessments}

    total_planned_weight = sum(q.weight for q in rubric.questions)
    if math.isnan(total_planned_weight) or math.isinf(total_planned_weight) or total_planned_weight <= 0:
        raise ScoringError("Total planned weight across questions must be strictly positive and finite.")

    question_results: dict[str, QuestionScoringResult] = {}
    total_evaluated_weight = 0.0
    weighted_score_sum = 0.0

    for question in rubric.questions:
        if math.isnan(question.weight) or math.isinf(question.weight) or question.weight < 0:
            raise ScoringError(f"Question '{question.id}' has invalid weight {question.weight}.")

        # Check criteria validation
        for crit in question.criteria:
            if math.isnan(crit.min_score) or math.isinf(crit.min_score) or math.isnan(crit.max_score) or math.isinf(crit.max_score):
                raise ScoringError(f"Criterion '{crit.id}' has non-finite scale limits.")
            if crit.max_score <= crit.min_score:
                raise ScoringError(
                    f"Criterion '{crit.id}' has invalid scale: max ({crit.max_score}) <= min ({crit.min_score})."
                )
            if math.isnan(crit.weight) or math.isinf(crit.weight) or crit.weight < 0:
                raise ScoringError(f"Criterion '{crit.id}' has invalid weight {crit.weight}.")

        assessment = assessments_map.get(question.id)

        # If question has no assessment or is explicitly excluded
        if assessment is None or assessment.is_excluded:
            question_results[question.id] = QuestionScoringResult(
                question_id=question.id,
                normalized_score=None,
                is_excluded=bool(assessment and assessment.is_excluded),
                active_weight=question.weight,
                criterion_normalized_scores={},
            )
            continue

        # Evaluate criteria within this question
        criteria_map = {c.id: c for c in question.criteria}
        seen_criterion_ids = set()
        user_scores_map = {}

        for cs in assessment.criteria_scores:
            if cs.criterion_id not in criteria_map:
                raise ScoringError(
                    f"Unknown criterion '{cs.criterion_id}' in assessment for question '{question.id}'."
                )
            if cs.criterion_id in seen_criterion_ids:
                raise ScoringError(
                    f"Duplicate criterion '{cs.criterion_id}' in assessment for question '{question.id}'."
                )
            seen_criterion_ids.add(cs.criterion_id)
            if cs.score is not None:
                if math.isnan(cs.score) or math.isinf(cs.score):
                    raise ScoringError(f"Score for criterion '{cs.criterion_id}' must be a finite number.")
            user_scores_map[cs.criterion_id] = cs

        crit_norm_scores: dict[str, float | None] = {}
        crit_weight_sum = 0.0
        crit_weighted_value_sum = 0.0

        for crit_id, crit in criteria_map.items():
            user_cs = user_scores_map.get(crit_id)
            if user_cs is None or user_cs.is_excluded or user_cs.score is None:
                crit_norm_scores[crit_id] = None
                continue

            # Validate range
            raw_score = user_cs.score
            if raw_score < crit.min_score or raw_score > crit.max_score:
                raise ScoringError(
                    f"Score {raw_score} for criterion '{crit_id}' is outside scale [{crit.min_score}, {crit.max_score}]."
                )

            # Normalization to [0, 1]
            normalized_c = (raw_score - crit.min_score) / (crit.max_score - crit.min_score)
            crit_norm_scores[crit_id] = normalized_c

            if crit.weight > 0:
                crit_weight_sum += crit.weight
                crit_weighted_value_sum += crit.weight * normalized_c

        # Question score is weighted average of valid criteria
        if crit_weight_sum > 0:
            q_score = crit_weighted_value_sum / crit_weight_sum
            question_results[question.id] = QuestionScoringResult(
                question_id=question.id,
                normalized_score=q_score,
                is_excluded=False,
                active_weight=question.weight,
                criterion_normalized_scores=crit_norm_scores,
            )
            # Question participates in final score if its weight > 0
            if question.weight > 0:
                total_evaluated_weight += question.weight
                weighted_score_sum += question.weight * q_score
        else:
            # All criteria were excluded or missing
            question_results[question.id] = QuestionScoringResult(
                question_id=question.id,
                normalized_score=None,
                is_excluded=False,
                active_weight=question.weight,
                criterion_normalized_scores=crit_norm_scores,
            )

    # Calculate coverage
    coverage_percentage = (total_evaluated_weight / total_planned_weight) * 100.0

    # Final score
    if total_evaluated_weight > 0:
        final_norm = weighted_score_sum / total_evaluated_weight
        final_score_100 = final_norm * 100.0
    else:
        final_score_100 = None

    return InterviewScoringResult(
        final_score_100=final_score_100,
        coverage_percentage=coverage_percentage,
        total_planned_weight=total_planned_weight,
        evaluated_weight=total_evaluated_weight,
        question_results=question_results,
    )
