import pytest

from backend.core.scoring import (
    ScoringError,
    calculate_interview_score,
)
from contracts.domain import (
    HumanCriterionScore,
    HumanQuestionAssessment,
    PlannedQuestion,
    RubricCriterion,
    RubricRevision,
)


@pytest.fixture
def standard_rubric() -> RubricRevision:
    crit1 = RubricCriterion(id="c1", title="Concurrency", min_score=1.0, max_score=5.0, weight=2.0)
    crit2 = RubricCriterion(id="c2", title="Memory", min_score=1.0, max_score=5.0, weight=1.0)
    crit3 = RubricCriterion(id="c3", title="System Design", min_score=0.0, max_score=10.0, weight=1.0)

    q1 = PlannedQuestion(
        id="q1",
        title="Multithreading",
        prompt="Explain synchronization primitives.",
        weight=2.0,
        criteria=[crit1, crit2],
    )
    q2 = PlannedQuestion(
        id="q2",
        title="Architecture",
        prompt="Design a rate limiter.",
        weight=1.0,
        criteria=[crit3],
    )

    return RubricRevision(
        revision_id="rub-rev-1",
        interview_id="int-1",
        is_approved=True,
        questions=[q1, q2],
    )


def test_perfect_score(standard_rubric):
    assessments = [
        HumanQuestionAssessment(
            question_id="q1",
            criteria_scores=[
                HumanCriterionScore(criterion_id="c1", score=5.0),
                HumanCriterionScore(criterion_id="c2", score=5.0),
            ],
        ),
        HumanQuestionAssessment(
            question_id="q2",
            criteria_scores=[
                HumanCriterionScore(criterion_id="c3", score=10.0),
            ],
        ),
    ]
    res = calculate_interview_score(standard_rubric, assessments)
    assert res.final_score_100 == 100.0
    assert res.coverage_percentage == 100.0
    assert res.question_results["q1"].normalized_score == 1.0
    assert res.question_results["q2"].normalized_score == 1.0


def test_minimum_score(standard_rubric):
    assessments = [
        HumanQuestionAssessment(
            question_id="q1",
            criteria_scores=[
                HumanCriterionScore(criterion_id="c1", score=1.0),
                HumanCriterionScore(criterion_id="c2", score=1.0),
            ],
        ),
        HumanQuestionAssessment(
            question_id="q2",
            criteria_scores=[
                HumanCriterionScore(criterion_id="c3", score=0.0),
            ],
        ),
    ]
    res = calculate_interview_score(standard_rubric, assessments)
    assert res.final_score_100 == 0.0
    assert res.coverage_percentage == 100.0


def test_mixed_scores_and_weights(standard_rubric):
    assessments = [
        HumanQuestionAssessment(
            question_id="q1",
            criteria_scores=[
                HumanCriterionScore(criterion_id="c1", score=3.0),
                HumanCriterionScore(criterion_id="c2", score=5.0),
            ],
        ),
        HumanQuestionAssessment(
            question_id="q2",
            criteria_scores=[
                HumanCriterionScore(criterion_id="c3", score=5.0),
            ],
        ),
    ]
    res = calculate_interview_score(standard_rubric, assessments)
    expected_score = (11.0 / 18.0) * 100.0
    assert pytest.approx(res.final_score_100, rel=1e-5) == expected_score
    assert res.coverage_percentage == 100.0


def test_question_exclusion_updates_denominator(standard_rubric):
    assessments = [
        HumanQuestionAssessment(
            question_id="q1",
            is_excluded=True,
            exclusion_reason="Not enough time during interview",
        ),
        HumanQuestionAssessment(
            question_id="q2",
            criteria_scores=[
                HumanCriterionScore(criterion_id="c3", score=10.0),
            ],
        ),
    ]
    res = calculate_interview_score(standard_rubric, assessments)
    assert res.final_score_100 == 100.0
    assert pytest.approx(res.coverage_percentage, rel=1e-4) == 33.3333
    assert res.question_results["q1"].is_excluded is True
    assert res.question_results["q1"].normalized_score is None


def test_all_questions_excluded_returns_none_score(standard_rubric):
    assessments = [
        HumanQuestionAssessment(question_id="q1", is_excluded=True, exclusion_reason="Candidate late"),
        HumanQuestionAssessment(question_id="q2", is_excluded=True, exclusion_reason="Candidate late"),
    ]
    res = calculate_interview_score(standard_rubric, assessments)
    assert res.final_score_100 is None
    assert res.coverage_percentage == 0.0


def test_invalid_scale_raises_error():
    bad_crit = RubricCriterion(id="bad", title="Bad Scale", min_score=5.0, max_score=1.0)
    rubric = RubricRevision(
        revision_id="r-bad",
        interview_id="int-1",
        questions=[PlannedQuestion(id="q", title="Question 1", prompt="Prompt", criteria=[bad_crit])],
    )
    with pytest.raises(ScoringError, match="invalid scale"):
        calculate_interview_score(rubric, [])


def test_score_out_of_range_raises_error(standard_rubric):
    assessments = [
        HumanQuestionAssessment(
            question_id="q1",
            criteria_scores=[
                HumanCriterionScore(criterion_id="c1", score=99.0),
            ],
        )
    ]
    with pytest.raises(ScoringError, match="outside scale"):
        calculate_interview_score(standard_rubric, assessments)
