import pytest

from backend.core.evidence_validator import validate_proposal
from contracts.audio import TrackType
from contracts.domain import (
    AssessmentProposal,
    CriterionScoreProposal,
    EvidenceRef,
    TranscriptRevision,
    TranscriptSegment,
)


@pytest.fixture
def sample_transcript() -> TranscriptRevision:
    return TranscriptRevision(
        revision_id="tx-rev-1",
        interview_id="inv-1",
        segments=[
            TranscriptSegment(
                id="seg-1",
                track_id=TrackType.INTERVIEWER,
                start_time_ms=0,
                end_time_ms=3000,
                text="Как вы организуете транзакции между микросервисами?",
            ),
            TranscriptSegment(
                id="seg-2",
                track_id=TrackType.CANDIDATE,
                start_time_ms=3500,
                end_time_ms=12000,
                text="Мы используем паттерн Saga с оркестрацией через Temporal для компенсационных транзакций.",
            ),
        ],
    )


def test_valid_evidence_passes(sample_transcript):
    proposal = AssessmentProposal(
        id="prop-1",
        interview_id="inv-1",
        question_id="q-saga",
        rubric_revision_id="rub-1",
        transcript_revision_id="tx-rev-1",
        model_profile_id="test-model",
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-arch",
                score=5.0,
                explanation="Кандидат детально описал использование Saga с оркестрацией через Temporal.",
                evidence=[
                    EvidenceRef(
                        segment_id="seg-2",
                        exact_quote="паттерн Saga с оркестрацией через Temporal",
                    )
                ],
            )
        ],
    )
    result = validate_proposal(proposal, sample_transcript)
    assert result.is_valid is True
    assert len(result.errors) == 0


def test_hallucinated_quote_fails(sample_transcript):
    proposal = AssessmentProposal(
        id="prop-2",
        interview_id="inv-1",
        question_id="q-saga",
        rubric_revision_id="rub-1",
        transcript_revision_id="tx-rev-1",
        model_profile_id="test-model",
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-arch",
                score=4.0,
                explanation="Кандидат упомянул Kafka Streams.",
                evidence=[
                    EvidenceRef(
                        segment_id="seg-2",
                        exact_quote="Мы также применяем Kafka Streams для стриминга",  # Hallucinated!
                    )
                ],
            )
        ],
    )
    result = validate_proposal(proposal, sample_transcript)
    assert result.is_valid is False
    assert any("not found verbatim" in err for err in result.errors)


def test_nonexistent_segment_id_fails(sample_transcript):
    proposal = AssessmentProposal(
        id="prop-3",
        interview_id="inv-1",
        question_id="q-saga",
        rubric_revision_id="rub-1",
        transcript_revision_id="tx-rev-1",
        model_profile_id="test-model",
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-arch",
                score=3.0,
                explanation="Ответ кандидата.",
                evidence=[
                    EvidenceRef(
                        segment_id="seg-999",  # Does not exist
                        exact_quote="любой текст",
                    )
                ],
            )
        ],
    )
    result = validate_proposal(proposal, sample_transcript)
    assert result.is_valid is False
    assert any("non-existent segment_id" in err for err in result.errors)


def test_forbidden_personal_bias_blocked(sample_transcript):
    proposal = AssessmentProposal(
        id="prop-4",
        interview_id="inv-1",
        question_id="q-saga",
        rubric_revision_id="rub-1",
        transcript_revision_id="tx-rev-1",
        model_profile_id="test-model",
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-arch",
                score=2.0,
                explanation="Кандидат ответил правильно, но у него заметный акцент и слишком высокая скорость речи.",
                evidence=[
                    EvidenceRef(
                        segment_id="seg-2",
                        exact_quote="паттерн Saga",
                    )
                ],
            )
        ],
    )
    result = validate_proposal(proposal, sample_transcript)
    assert result.is_valid is False
    assert any("forbidden attribute reference" in err for err in result.errors)


def test_technical_terms_do_not_trigger_false_positive_bias(sample_transcript):
    """
    R7: Words like 'message', 'storage', 'coverage', 'акцент на репликации', 'старый сервер'
    must not trigger forbidden personal bias.
    """
    valid_explanations = [
        "Candidate clearly explains message queues and storage partitioning.",
        "Candidate made a clear coverage assessment and leverage analysis.",
        "Кандидат сделал акцент на масштабируемости и репликации данных.",
        "Описан перенос нагрузки со старого сервера на новый кластер.",
    ]
    for exp in valid_explanations:
        proposal = AssessmentProposal(
            id="prop-tech",
            interview_id="inv-1",
            question_id="q-saga",
            rubric_revision_id="rub-1",
            transcript_revision_id="tx-rev-1",
            model_profile_id="test-model",
            scores=[
                CriterionScoreProposal(
                    criterion_id="crit-arch",
                    score=4.0,
                    explanation=exp,
                    evidence=[
                        EvidenceRef(
                            segment_id="seg-2",
                            exact_quote="паттерн Saga",
                        )
                    ],
                )
            ],
        )
        res = validate_proposal(proposal, sample_transcript)
        assert res.is_valid is True, f"Explanation '{exp}' failed validation: {res.errors}"


def test_genuine_protected_attributes_are_strictly_blocked(sample_transcript):
    """Real age, gender, accent, nationality or appearance judgments are blocked."""
    invalid_explanations = [
        ("Candidate's age is an issue for this fast-paced startup role.", "возраст"),
        ("Кандидат слишком старый для нашей молодой команды.", "возрастная дискриминация"),
        ("Кандидат женщина, поэтому ей будет сложно руководить мужским отделом.", "пол кандидата"),
        ("У кандидата слишком явный акцент в речи.", "акцент"),
        ("Оценка снижена из-за национальности кандидата.", "национальность"),
    ]
    for exp, expected_bias in invalid_explanations:
        proposal = AssessmentProposal(
            id="prop-bias",
            interview_id="inv-1",
            question_id="q-saga",
            rubric_revision_id="rub-1",
            transcript_revision_id="tx-rev-1",
            model_profile_id="test-model",
            scores=[
                CriterionScoreProposal(
                    criterion_id="crit-arch",
                    score=2.0,
                    explanation=exp,
                    evidence=[
                        EvidenceRef(
                            segment_id="seg-2",
                            exact_quote="паттерн Saga",
                        )
                    ],
                )
            ],
        )
        res = validate_proposal(proposal, sample_transcript)
        assert any(
            "forbidden attribute reference" in err and expected_bias in err
            for err in res.errors
        )
