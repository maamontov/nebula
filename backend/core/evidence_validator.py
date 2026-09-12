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

# Word-bounded and context-aware bias rules to prevent false positives on technical terms
# like 'message', 'storage', 'coverage', 'акцент на масштабируемости', 'старый сервер'.
BIAS_RULES: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = [
    # Accent / Speech style
    (
        "акцент",
        re.compile(r"\b(accent|акцент\w*)\b", re.IGNORECASE),
        re.compile(r"\b(акцент\s+на|accent\s+on)\b", re.IGNORECASE),
    ),
    (
        "произношение",
        re.compile(r"\b(pronunciation|произношени\w*)\b", re.IGNORECASE),
        None,
    ),
    (
        "тембр",
        re.compile(r"\b(timbre|тембр\w*)\b", re.IGNORECASE),
        None,
    ),
    (
        "дикция",
        re.compile(r"\b(diction|дикци\w*)\b", re.IGNORECASE),
        None,
    ),
    (
        "скорость речи",
        re.compile(r"\b(speech\s+rate|скорост\w+\s+речи)\b", re.IGNORECASE),
        None,
    ),
    # Personal attributes
    (
        "возраст",
        re.compile(r"\b(age|ages|aging|aged|возраст\w*)\b", re.IGNORECASE),
        None,
    ),
    (
        "пожилой",
        re.compile(r"\b(elderly|пожил\w+)\b", re.IGNORECASE),
        None,
    ),
    (
        "возрастная дискриминация",
        re.compile(
            r"\b(молод\w+\s+(кандидат|специалист|человек|разработчик|инженер)|"
            r"(кандидат|специалист|человек|разработчик|инженер)\s+молод\w*|"
            r"слишком\s+молод\w*|"
            r"стар\w+\s+(кандидат|специалист|человек|разработчик|инженер)|"
            r"(кандидат|специалист|человек|разработчик|инженер)\s+стар\w*|"
            r"слишком\s+стар\w*)\b",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        "пол кандидата",
        re.compile(r"\b(gender|пол\s+кандидата|мужчин\w*|женщин\w*)\b", re.IGNORECASE),
        None,
    ),
    (
        "национальность",
        re.compile(r"\b(nationality|национальност\w*)\b", re.IGNORECASE),
        None,
    ),
    (
        "раса",
        re.compile(r"\b(race|рас[аыеуой]|расов\w*)\b", re.IGNORECASE),
        None,
    ),
    (
        "внешность",
        re.compile(r"\b(appearance|внешност\w*)\b", re.IGNORECASE),
        None,
    ),
    (
        "эмоциональность",
        re.compile(r"\b(emotionality|эмоциональност\w*)\b", re.IGNORECASE),
        None,
    ),
]


def find_forbidden_attributes(text: str) -> list[str]:
    """
    Detects protected personal attributes and bias in explanation or follow-up text.
    Uses word boundaries and context awareness to avoid false positives on technical
    terms like 'message', 'storage', 'coverage', 'акцент на репликации', 'старый сервер'.
    """
    matches: list[str] = []
    for label, pattern, exc_pattern in BIAS_RULES:
        if pattern.search(text) and not (exc_pattern and exc_pattern.search(text)):
            matches.append(label)
    return matches


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
    allowed_criteria_ids: list[str] | set[str] | None = None,
    min_score: float = 1.0,
    max_score: float = 5.0,
) -> EvidenceValidationResult:
    """
    Performs rigorous verification of an AssessmentProposal against the real TranscriptRevision:
    1. Ensures segment IDs exist in the transcript revision.
    2. Ensures evidence quotes strictly originate from the candidate speech track (not interviewer).
    3. Ensures verbatim quotes exist in the referenced segments.
    4. Enforces criterion score ranges (e.g. 1.0 to 5.0) and membership in question criteria.
    5. Validates character offsets when provided.
    6. Flags forbidden personal attribute bias.
    7. Detects suspicious prompt injection attempts.
    """
    errors: list[str] = []
    warnings: list[str] = []
    requires_review = False

    segments_by_id = {s.id: s for s in transcript.segments}

    for score_item in proposal.scores:
        # 1. Check explanation for forbidden personal bias
        forbidden_matches = find_forbidden_attributes(score_item.explanation)
        for forbidden in forbidden_matches:
            errors.append(
                f"Criterion '{score_item.criterion_id}' explanation contains forbidden attribute reference: '{forbidden}'"
            )

        # 2. Check for prompt injection traces in explanation
        exp_lower = score_item.explanation.lower()
        for pattern in SUSPICIOUS_INJECTION_PATTERNS:
            if re.search(pattern, exp_lower):
                warnings.append(
                    f"Criterion '{score_item.criterion_id}' explanation contains suspicious prompt injection pattern: '{pattern}'"
                )
                requires_review = True

        # 3. Check criterion membership if allowed_criteria_ids is supplied
        if allowed_criteria_ids and score_item.criterion_id not in allowed_criteria_ids:
            errors.append(
                f"Criterion '{score_item.criterion_id}' does not belong to question criteria: {list(allowed_criteria_ids)}."
            )

        # 4. Check score range
        if score_item.score is not None and (score_item.score < min_score or score_item.score > max_score):
            errors.append(
                f"Score {score_item.score} for criterion '{score_item.criterion_id}' is out of allowed range [{min_score}, {max_score}]."
            )

        # 5. If score is provided, evidence should ideally be present
        if score_item.score is not None and not score_item.evidence:
            warnings.append(
                f"Criterion '{score_item.criterion_id}' has a score ({score_item.score}) but no supporting evidence quotes."
            )
            requires_review = True

        # 6. Validate every evidence item
        for ev in score_item.evidence:
            seg = segments_by_id.get(ev.segment_id)
            if not seg:
                errors.append(
                    f"Evidence references non-existent segment_id '{ev.segment_id}' in revision '{transcript.revision_id}'."
                )
                continue

            # Ensure evidence comes from candidate speech track or role!
            track_str = seg.track_id.value if hasattr(seg.track_id, "value") else str(seg.track_id).lower()
            role_str = getattr(seg, "speaker_role", "unknown").lower()
            if track_str == "candidate":
                is_cand = (role_str != "interviewer")
            elif track_str == "shared":
                is_cand = (role_str == "candidate")
            else:
                is_cand = (role_str == "candidate")

            if not is_cand:
                errors.append(
                    f"Evidence for criterion '{score_item.criterion_id}' references {track_str} track segment '{ev.segment_id}'. Evidence quotes must strictly come from candidate track."
                )

            is_valid_quote, err_msg = validate_evidence_quote(ev, seg.text)
            if not is_valid_quote:
                errors.append(
                    f"Invalid evidence for criterion '{score_item.criterion_id}': {err_msg}"
                )

            if ev.start_char is not None and ev.end_char is not None and (ev.start_char < 0 or ev.end_char > len(seg.text) or ev.start_char > ev.end_char):
                errors.append(
                    f"Invalid char offsets [{ev.start_char}:{ev.end_char}] for segment length {len(seg.text)}."
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
