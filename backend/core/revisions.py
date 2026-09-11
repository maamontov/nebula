"""
Transcript Revisions & Diff Engine.
Adheres strictly to docs/implementation-plan.md Section 6 & 10:
- Compares live transcript (trans-rev-1) against batch final transcript (trans-rev-2).
- Tracks segment matches, insertions, deletions, and textual edits.
- Validates quote persistence in EvidenceRef to detect broken evidence.
- Flags dependent proposals as stale (is_stale=True) without destroying human overrides.
"""

import difflib
from dataclasses import dataclass
from typing import Any


@dataclass
class SegmentDiff:
    segment_id_old: str | None
    segment_id_new: str | None
    track_id: str
    old_text: str | None
    new_text: str | None
    similarity_ratio: float
    is_modified: bool
    is_inserted: bool
    is_deleted: bool


@dataclass
class QuestionDiffReport:
    question_id: str
    is_modified: bool
    diff_ratio: float
    changed_segments: list[SegmentDiff]
    broken_evidence: list[dict[str, Any]]
    stale_reason: str | None = None


class TranscriptDiffEngine:
    def __init__(self, similarity_threshold: float = 0.95):
        self.similarity_threshold = similarity_threshold

    def calculate_text_similarity(self, text_a: str, text_b: str) -> float:
        matcher = difflib.SequenceMatcher(None, text_a.strip().lower(), text_b.strip().lower())
        return matcher.ratio()

    def compare_revisions(
        self,
        old_segments: list[dict[str, Any]],
        new_segments: list[dict[str, Any]],
    ) -> list[SegmentDiff]:
        """
        Matches segments between revisions based on track and timestamp overlap.
        Returns list of SegmentDiff objects.
        """
        diffs: list[SegmentDiff] = []
        matched_new_ids: set[str] = set()

        for old in old_segments:
            old_id = old.get("id") or old.get("segment_id")
            old_track = old.get("track_id", "candidate")
            old_start = old.get("start_time_ms", 0)
            old_end = old.get("end_time_ms", 0)
            old_text = old.get("text", "").strip()

            # Find best candidate in new_segments with same track and overlapping timestamps
            best_match: dict[str, Any] | None = None
            best_overlap = 0

            for new in new_segments:
                new_id = new.get("id") or new.get("segment_id")
                if new.get("track_id") != old_track:
                    continue
                new_start = new.get("start_time_ms", 0)
                new_end = new.get("end_time_ms", 0)

                # Overlap calculation
                overlap_start = max(old_start, new_start)
                overlap_end = min(old_end, new_end)
                overlap_ms = max(0, overlap_end - overlap_start)

                if overlap_ms > best_overlap:
                    best_overlap = overlap_ms
                    best_match = new

            if best_match:
                new_id = best_match.get("id") or best_match.get("segment_id")
                matched_new_ids.add(new_id)
                new_text = best_match.get("text", "").strip()
                sim = self.calculate_text_similarity(old_text, new_text)
                is_mod = sim < self.similarity_threshold

                diffs.append(
                    SegmentDiff(
                        segment_id_old=old_id,
                        segment_id_new=new_id,
                        track_id=old_track,
                        old_text=old_text,
                        new_text=new_text,
                        similarity_ratio=sim,
                        is_modified=is_mod,
                        is_inserted=False,
                        is_deleted=False,
                    )
                )
            else:
                # Segment was deleted in new revision
                diffs.append(
                    SegmentDiff(
                        segment_id_old=old_id,
                        segment_id_new=None,
                        track_id=old_track,
                        old_text=old_text,
                        new_text=None,
                        similarity_ratio=0.0,
                        is_modified=True,
                        is_inserted=False,
                        is_deleted=True,
                    )
                )

        # Detect inserted segments
        for new in new_segments:
            new_id = new.get("id") or new.get("segment_id")
            if new_id not in matched_new_ids:
                diffs.append(
                    SegmentDiff(
                        segment_id_old=None,
                        segment_id_new=new_id,
                        track_id=new.get("track_id", "candidate"),
                        old_text=None,
                        new_text=new.get("text", "").strip(),
                        similarity_ratio=0.0,
                        is_modified=True,
                        is_inserted=True,
                        is_deleted=False,
                    )
                )

        return diffs

    def evaluate_question_diff(
        self,
        question_id: str,
        question_segment_ids: list[str],
        segment_diffs: list[SegmentDiff],
        existing_evidence: list[dict[str, Any]],
        new_segments: list[dict[str, Any]],
    ) -> QuestionDiffReport:
        """
        Determines if a question's response was modified in the new revision,
        and verifies if existing evidence quotes remain valid.
        """
        relevant_diffs = [
            d for d in segment_diffs if d.segment_id_old in question_segment_ids or d.segment_id_new in question_segment_ids
        ]

        modified_segments = [d for d in relevant_diffs if d.is_modified or d.is_inserted or d.is_deleted]
        is_question_modified = len(modified_segments) > 0

        # Calculate average similarity for relevant segments
        if relevant_diffs:
            avg_similarity = sum(d.similarity_ratio for d in relevant_diffs) / len(relevant_diffs)
        else:
            avg_similarity = 1.0

        # Validate existing quotes against new segments
        broken_evidence: list[dict[str, Any]] = []
        new_full_text = " ".join(s.get("text", "").strip() for s in new_segments).lower()

        for ev in existing_evidence:
            quote = ev.get("exact_quote", "").strip().lower()
            if quote and quote not in new_full_text:
                # Fuzzy check
                matcher = difflib.SequenceMatcher(None, quote, new_full_text)
                match = matcher.find_longest_match(0, len(quote), 0, len(new_full_text))
                if match.size / len(quote) < 0.85:
                    broken_evidence.append(ev)

        stale_reason = None
        if is_question_modified:
            reasons = []
            if modified_segments:
                reasons.append(f"{len(modified_segments)} сегмент(ов) изменены или дополнены")
            if broken_evidence:
                reasons.append(f"{len(broken_evidence)} цитат(ы) доказательств не найдены в новой стенограмме")
            stale_reason = "; ".join(reasons)

        return QuestionDiffReport(
            question_id=question_id,
            is_modified=is_question_modified,
            diff_ratio=avg_similarity,
            changed_segments=modified_segments,
            broken_evidence=broken_evidence,
            stale_reason=stale_reason,
        )
