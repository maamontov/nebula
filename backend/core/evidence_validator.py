"""
Programmatic validation of Evidence and Assessment Proposals.
Prevents hallucinations, fake quotes, prompt injections, and protected personal attribute bias.
See docs/implementation-plan.md Section 4.3 and 7.2.
"""
import re
from dataclasses import dataclass

from contracts.domain import (
    AssessmentProposal,
    EvidenceRef,
    TranscriptRevision,
)

FORBIDDEN_ATTRIBUTES_KEYWORDS = [
    # Accent / Speech style
    "акцент", "произношение", "тембр", "дикция", "скорость речи",
    "accent", "pronunciation", "timbre", "speech rate",
    # Personal attributes
    "возраст", "молодой", "пожилой", "старый", "age", "elderly",
    "пол кандидата", "мужчина", "женщина", "gender",
    "национальность", "раса", "внешность", "эмоциональность",
]

SUSPICIOUS_INJECTION_PATTERNS = [
    r"ignore\s+(previous|all)\s+instructions",
    r"поставь\s+максимальный\s+балл",
    r"give\s+(full|maximum|100)\s+score",
    r"system\s*prompt",
    r"you\s+are\s+now",
]


@dataclass(frozen=True)
class EvidenceValidationResult:
    is_valid: bool
    requires_human_review: bool
    errors: list[str]
    warnings: list[str]


def _normalize_text(text: str) -> str:
    """Normalizes whitespace and lowercases for quote matching."""
    return re.sub(r"\s+", " ", text).strip().lower()


def validate_evidence_quote(evidence: EvidenceRef, segment_text: str) -> tuple[bool, str | None]:
    """
    Checks whether exact_quote actually exists inside segment_text.
    """
    if not evidence.exact_quote.strip():
        return False, "Evidence quote cannot be empty."

    norm_quote = _normalize_text(evidence.exact_quote)
    norm_segment = _normalize_text(segment_text)

    if norm_quote not in norm_segment:
        return False, (
            f"Quote '{evidence.exact_quote}' not found verbatim in segment text: '{segment_text}'."
        )

    return True, None


def validate_proposal(
    proposal: AssessmentProposal,
    transcript: TranscriptRevision,
) -> EvidenceValidationResult:
    """
    Performs rigorous verification of an AssessmentProposal against the real TranscriptRevision:
    1. Ensures segment IDs exist in the transcript revision.
    2. Ensures verbatim quotes exist in the referenced segments.
    3. Flags forbidden personal attribute bias.
    4. Detects suspicious prompt injection attempts.
    """
    errors: list[str] = []
    warnings: list[str] = []
    requires_review = False

    segments_by_id = {s.id: s for s in transcript.segments}

    for score_item in proposal.scores:
        # 1. Check explanation for forbidden personal bias
        exp_lower = score_item.explanation.lower()
        for forbidden in FORBIDDEN_ATTRIBUTES_KEYWORDS:
            if forbidden in exp_lower:
                errors.append(
                    f"Criterion '{score_item.criterion_id}' explanation contains forbidden attribute reference: '{forbidden}'"
                )

        # 2. Check for prompt injection traces in explanation
        for pattern in SUSPICIOUS_INJECTION_PATTERNS:
            if re.search(pattern, exp_lower):
                warnings.append(
                    f"Criterion '{score_item.criterion_id}' explanation contains suspicious prompt injection pattern: '{pattern}'"
                )
                requires_review = True

        # 3. If score is provided, evidence should ideally be present
        if score_item.score is not None and not score_item.evidence:
            warnings.append(
                f"Criterion '{score_item.criterion_id}' has a score ({score_item.score}) but no supporting evidence quotes."
            )
            requires_review = True

        # 4. Validate every evidence item
        for ev in score_item.evidence:
            seg = segments_by_id.get(ev.segment_id)
            if not seg:
                errors.append(
                    f"Evidence references non-existent segment_id '{ev.segment_id}' in revision '{transcript.revision_id}'."
                )
                continue

            is_valid_quote, err_msg = validate_evidence_quote(ev, seg.text)
            if not is_valid_quote:
                errors.append(
                    f"Invalid evidence for criterion '{score_item.criterion_id}': {err_msg}"
                )

    if proposal.critical_errors:
        requires_review = True
        warnings.extend(proposal.critical_errors)

    is_valid = len(errors) == 0
    if not is_valid:
        requires_review = True

    return EvidenceValidationResult(
        is_valid=is_valid,
        requires_human_review=requires_review,
        errors=errors,
        warnings=warnings,
    )
