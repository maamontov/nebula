"""
Question-to-Transcript Association and Robustness Engine.
Adheres to docs/implementation-plan.md Sections 7, 10, and 11:
- Associates candidate responses with planned questions.
- Identifies clarification / follow-up queries.
- Detects interruptions and track overlaps.
- Explicitly flags ambiguities instead of guessing (Principle: Ambiguity is never hidden).
"""

from dataclasses import dataclass, field
from typing import Any
import re


@dataclass
class InterruptionEvent:
    interrupted_track: str
    interrupting_track: str
    overlap_start_ms: int
    overlap_end_ms: int
    overlap_duration_ms: int


@dataclass
class SegmentAssociation:
    segment_id: str
    question_id: str
    confidence: float
    is_ambiguous: bool
    is_clarification: bool = False
    is_manually_adjusted: bool = False
    interruption: InterruptionEvent | None = None
    notes: str = ""


class QuestionMatcher:
    def __init__(self, ambiguity_threshold: float = 0.55):
        self.ambiguity_threshold = ambiguity_threshold

    def detect_interruptions(self, segments: list[dict[str, Any]]) -> list[InterruptionEvent]:
        """Detects overlapping speech turns across different tracks."""
        interruptions = []
        for i, s1 in enumerate(segments):
            for j, s2 in enumerate(segments):
                if i >= j:
                    continue
                if s1["track_id"] != s2["track_id"]:
                    start1, end1 = s1["start_time_ms"], s1["end_time_ms"]
                    start2, end2 = s2["start_time_ms"], s2["end_time_ms"]

                    # Check overlap
                    overlap_start = max(start1, start2)
                    overlap_end = min(end1, end2)
                    if overlap_end > overlap_start:
                        duration = overlap_end - overlap_start
                        if duration >= 250:  # Ignore micro-overlaps < 250ms
                            first = s1 if start1 < start2 else s2
                            second = s2 if start1 < start2 else s1
                            interruptions.append(
                                InterruptionEvent(
                                    interrupted_track=first["track_id"],
                                    interrupting_track=second["track_id"],
                                    overlap_start_ms=overlap_start,
                                    overlap_end_ms=overlap_end,
                                    overlap_duration_ms=duration,
                                )
                            )
        return interruptions

    def _extract_keywords(self, text: str) -> set[str]:
        words = re.findall(r"[а-яА-Яa-zA-Z0-9_\-]+", text.lower())
        stopwords = {
            "и", "в", "на", "с", "по", "к", "для", "не", "что", "это", "как", "из", "о", "об",
            "ли", "же", "от", "до", "при", "бы", "то", "же", "он", "она", "они", "мы", "вы",
            "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with", "is"
        }
        return {w for w in words if len(w) > 2 and w not in stopwords}

    def calculate_relevance(self, candidate_text: str, question_text: str, criteria: list[Any]) -> float:
        """Calculates keyword Jaccard overlap between candidate answer and question criteria."""
        cand_kw = self._extract_keywords(candidate_text)
        if not cand_kw:
            return 0.0

        crit_words = []
        for c in criteria:
            if isinstance(c, dict):
                crit_words.append(f"{c.get('title', '')} {c.get('description', '')}")
            elif isinstance(c, str):
                crit_words.append(c)

        target_text = f"{question_text} {' '.join(crit_words)}"
        target_kw = self._extract_keywords(target_text)
        if not target_kw:
            return 0.5

        overlap = cand_kw.intersection(target_kw)
        score = len(overlap) / max(len(target_kw), 1)
        # Normalize with min floor
        return min(1.0, score * 2.5)

    def associate_segments(
        self,
        questions: list[dict[str, Any]],
        segments: list[dict[str, Any]],
        existing_associations: list[dict[str, Any]] | None = None,
    ) -> list[SegmentAssociation]:
        """
        Associates speech segments with planned questions.
        Handles:
        1. Preservation of manual adjustments (never overwritten by automated matcher).
        2. Contextual continuity from interviewer questions.
        3. Clarifications (short follow-ups from interviewer).
        4. Interruption detection.
        5. Ambiguity flagging.
        """
        associations: list[SegmentAssociation] = []
        if not questions or not segments:
            return associations

        existing_by_seg: dict[str, dict[str, Any]] = {}
        if existing_associations:
            existing_by_seg = {a["segment_id"]: a for a in existing_associations}

        interruptions = self.detect_interruptions(segments)
        active_question_id = questions[0].get("id") or questions[0].get("question_id")

        for idx, seg in enumerate(segments):
            seg_id = seg["id"]

            # If segment was already manually adjusted by a human reviewer, strictly preserve it!
            existing = existing_by_seg.get(seg_id)
            if existing and existing.get("is_manually_adjusted"):
                active_question_id = existing["question_id"]
                associations.append(
                    SegmentAssociation(
                        segment_id=seg_id,
                        question_id=existing["question_id"],
                        confidence=existing.get("confidence", 1.0),
                        is_ambiguous=bool(existing.get("is_ambiguous", 0)),
                        is_clarification=False,
                        is_manually_adjusted=True,
                        interruption=None,
                        notes=existing.get("notes") or "Ручная привязка интервьюером",
                    )
                )
                continue

            seg_text = seg["text"].strip()
            track = seg["track_id"]

            # Find if this segment experienced an interruption
            seg_int = next(
                (
                    e
                    for e in interruptions
                    if (e.overlap_start_ms >= seg["start_time_ms"] and e.overlap_end_ms <= seg["end_time_ms"])
                    or (seg["start_time_ms"] <= e.overlap_start_ms <= seg["end_time_ms"])
                ),
                None,
            )

            if track == "interviewer":
                # Check if interviewer asked a new question or clarification
                is_clarification = False
                matched_qid = None
                best_sim = 0.0

                # Check if it's a short clarification (e.g. "А что насчет ...?")
                if len(seg_text.split()) <= 10 and any(
                    marker in seg_text.lower() for marker in ["а что", "а как", "уточните", "почему", "в чем"]
                ):
                    is_clarification = True
                    matched_qid = active_question_id
                else:
                    # Match against all questions
                    for q in questions:
                        qid = q.get("id") or q.get("question_id")
                        q_text = q.get("text") or q.get("prompt") or ""
                        crit = q.get("criteria") or q.get("evaluation_criteria") or []
                        sim = self.calculate_relevance(seg_text, q_text, crit)
                        if sim > best_sim:
                            best_sim = sim
                            matched_qid = qid

                if matched_qid and best_sim > 0.3:
                    active_question_id = matched_qid

                associations.append(
                    SegmentAssociation(
                        segment_id=seg["id"],
                        question_id=active_question_id,
                        confidence=1.0 if not is_clarification else 0.9,
                        is_ambiguous=False,
                        is_clarification=is_clarification,
                        interruption=seg_int,
                        notes="Вопрос интервьюера" if not is_clarification else "Уточняющий вопрос",
                    )
                )

            else:
                # Candidate response segment
                curr_q = next(
                    (q for q in questions if (q.get("id") or q.get("question_id")) == active_question_id),
                    questions[0],
                )
                curr_q_text = curr_q.get("text") or curr_q.get("prompt") or ""
                curr_crit = curr_q.get("criteria") or curr_q.get("evaluation_criteria") or []

                relevance = self.calculate_relevance(seg_text, curr_q_text, curr_crit)

                # Check if answer might belong to a different question
                candidate_other_matches = []
                for other_q in questions:
                    other_qid = other_q.get("id") or other_q.get("question_id")
                    if other_qid == active_question_id:
                        continue
                    other_text = other_q.get("text") or other_q.get("prompt") or ""
                    other_crit = other_q.get("criteria") or other_q.get("evaluation_criteria") or []
                    other_rel = self.calculate_relevance(seg_text, other_text, other_crit)
                    if other_rel > relevance:
                        candidate_other_matches.append((other_qid, other_rel))

                is_ambiguous = False
                notes = "Ответ кандидата"
                target_qid = active_question_id
                target_confidence = max(0.3, relevance)

                if candidate_other_matches:
                    is_ambiguous = True
                    candidate_other_matches.sort(key=lambda x: x[1], reverse=True)
                    alt_id, alt_rel = candidate_other_matches[0]
                    if relevance < 0.15 and alt_rel >= 0.15:
                        target_qid = alt_id
                        active_question_id = alt_id
                        target_confidence = alt_rel
                        notes = f"Неоднозначность: ответ отнесен к вопросу {alt_id} (уверенность {alt_rel:.2f}) при несовпадении с активным ({relevance:.2f}). Требуется ревью."
                    else:
                        notes = f"Неоднозначность: ответ больше похож на вопрос {alt_id}. Требуется ревью человека."
                elif relevance < self.ambiguity_threshold:
                    is_ambiguous = True
                    notes = f"Неоднозначность: низкая тематическая уверенность ({relevance:.2f}). Требуется подтверждение."

                associations.append(
                    SegmentAssociation(
                        segment_id=seg["id"],
                        question_id=target_qid,
                        confidence=target_confidence,
                        is_ambiguous=is_ambiguous,
                        is_clarification=False,
                        interruption=seg_int,
                        notes=notes,
                    )
                )

        return associations
