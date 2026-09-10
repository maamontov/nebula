"""
Durable Pipeline Worker for Nebula.
Processes background jobs with lease timeouts, retry backoff, and idempotent execution:
- TRANSCRIBE_AUDIO: Audio chunk transcription via STT adapter
- EVALUATE_QUESTION: Technical answer scoring via LLM adapter with EvidenceValidator checks
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import struct
import uuid
from typing import Any

from dotenv import load_dotenv

# Ensure environment variables (.env) are loaded
load_dotenv()

from backend.adapters.llm import LLMRateLimitError, OpenAICompatibleAdapter
from backend.adapters.resilient_llm import ResilientLLMAdapter
from backend.adapters.stt import OpenAICompatibleSTTAdapter, STTRateLimitError
from backend.core.audio_utils import pcm_s16le_to_wav_bytes
from backend.core.evidence_validator import validate_proposal

from backend.core.matcher import QuestionMatcher
from backend.core.revisions import TranscriptDiffEngine
from backend.core.summary_generator import ExecutiveSummaryGenerator
from backend.core.profiles import (
    get_plusvibe_gemini_model,
    get_plusvibe_provider,
    get_plusvibe_whisper_stt,
)
from backend.db.repository import Repository
from contracts.audio import TrackType
from contracts.domain import (
    AssessmentProposal,
    CriterionScoreProposal,
    EvidenceRef,
    TranscriptRevision,
    TranscriptSegment,
)

logger = logging.getLogger("nebula.pipeline")


def is_pcm_silence(audio_bytes: bytes, threshold_mean_abs: float = 12.0) -> bool:
    """Returns True if audio chunk is silence or quiet ambient noise below threshold."""
    if len(audio_bytes) < 2:
        return True
    sample_count = len(audio_bytes) // 2
    samples = struct.unpack(f"<{sample_count}h", audio_bytes[: sample_count * 2])
    mean_abs = sum(abs(s) for s in samples) / sample_count
    return mean_abs < threshold_mean_abs


class PipelineWorker:
    def __init__(
        self,
        repository: Repository,
        stt_adapter: OpenAICompatibleSTTAdapter | None = None,
        llm_adapter: OpenAICompatibleAdapter | ResilientLLMAdapter | None = None,
    ) -> None:
        self.repo = repository
        self.provider = get_plusvibe_provider()
        self.stt_adapter = stt_adapter or OpenAICompatibleSTTAdapter(
            get_plusvibe_whisper_stt(), api_key_env="PLUSVIBE_API_KEY"
        )
        self.llm_adapter = llm_adapter or ResilientLLMAdapter()
        self._running = False

    async def process_one_job(self) -> bool:
        """Claims and executes a single job with atomic lease lock. Returns True if job was processed."""
        job = self.repo.claim_next_job(lock_duration_sec=60)
        if not job:
            return False

        job_id = job["id"]
        job_type = job["type"]
        interview_id = job["interview_id"]
        payload = job["payload"]
        owner_token = job.get("locked_by")

        # Check if interview exists before processing (privacy lifecycle protection)
        interview = self.repo.get_interview(interview_id)
        if not interview:
            logger.warning("Interview %s was deleted. Discarding job %s without saving results.", interview_id, job_id)
            self.repo.fail_job(job_id, "Interview was deleted (privacy lifecycle)", owner_token=owner_token)
            return True

        try:
            if job_type == "TRANSCRIBE_AUDIO":
                grouped_ids = await self._handle_transcribe(interview_id, payload, owner_token=owner_token)
                for gid in grouped_ids:
                    self.repo.complete_job(gid, owner_token=owner_token)
            elif job_type == "EVALUATE_QUESTION":
                await self._handle_evaluate(interview_id, payload)
            elif job_type == "BATCH_RETRANSCRIBE":
                await self._handle_batch_retranscribe(interview_id, payload)
            elif job_type == "GENERATE_SUMMARY":
                await self._handle_generate_summary(interview_id, payload)
            else:
                raise ValueError(f"Unknown job type: {job_type}")

            # Re-verify interview wasn't deleted while job was running
            if not self.repo.get_interview(interview_id):
                logger.warning("Interview %s deleted during execution. Discarding job %s.", interview_id, job_id)
                self.repo.fail_job(job_id, "Interview was deleted during execution", owner_token=owner_token)
                return True

            self.repo.complete_job(job_id, owner_token=owner_token)
            self.repo.record_audit_event(
                event_id=f"audit-{uuid.uuid4().hex[:8]}",
                interview_id=interview_id,
                event_type=f"JOB_COMPLETED_{job_type}",
                payload={"job_id": job_id},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s (%s) failed: %s", job_id, job_type, exc)
            self.repo.fail_job(job_id, str(exc), owner_token=owner_token)
            self.repo.record_audit_event(
                event_id=f"audit-{uuid.uuid4().hex[:8]}",
                interview_id=interview_id,
                event_type=f"JOB_FAILED_{job_type}",
                payload={"job_id": job_id, "error": str(exc)},
            )
            if isinstance(exc, (STTRateLimitError, LLMRateLimitError)):
                retry_wait = min(getattr(exc, "retry_after", 5.0) or 5.0, 30.0)
                logger.info("Pacing worker loop due to rate limit: sleeping %.1fs", retry_wait)
                await asyncio.sleep(retry_wait)
            return False

    async def _handle_transcribe(
        self,
        interview_id: str,
        payload: dict[str, Any],
        owner_token: str | None = None,
    ) -> list[str]:
        audio_bytes = bytes.fromhex(payload["audio_hex"])
        track_id = payload.get("track_id", "candidate")
        start_ms = payload.get("start_ms", 0)
        end_ms = payload.get("end_ms", 0)
        sample_rate = payload.get("sample_rate", 16000)
        channels = payload.get("channels", 1)
        format_val = payload.get("format", "pcm_s16le")
        language = payload.get("language", "ru")

        is_real_stt = (
            isinstance(self.stt_adapter, OpenAICompatibleSTTAdapter)
            and self.stt_adapter._external_client is None
        )

        # 1. Skip single silence chunks for real remote STT to prevent 502 upstream errors and unnecessary API calls
        if is_real_stt and (format_val == "pcm_s16le" or not audio_bytes.startswith(b"RIFF")) and is_pcm_silence(audio_bytes):
            logger.debug("Skipping silence chunk for interview %s (track %s, %d-%d ms)", interview_id, track_id, start_ms, end_ms)
            return []

        # 2. Dynamic batch grouping: combine waiting consecutive chunks into a coherent 3-4s phrase
        grouped_job_ids: list[str] = []
        if is_real_stt and format_val == "pcm_s16le":
            adjacent = self.repo.claim_adjacent_transcribe_jobs(
                interview_id=interview_id,
                track_id=track_id,
                max_additional=3,
                owner_token=owner_token,
            )
            for adj_job in adjacent:
                adj_payload = adj_job.get("payload", {})
                adj_hex = adj_payload.get("audio_hex", "")
                if not adj_hex:
                    continue
                adj_bytes = bytes.fromhex(adj_hex)
                # If an adjacent chunk is silence, complete it and stop grouping at natural speech pause
                if is_pcm_silence(adj_bytes):
                    grouped_job_ids.append(adj_job["id"])
                    break
                audio_bytes += adj_bytes
                end_ms = adj_payload.get("end_ms", end_ms)
                grouped_job_ids.append(adj_job["id"])

        # If audio payload is raw PCM, wrap it into a compliant WAV container before STT
        if format_val == "pcm_s16le" or not audio_bytes.startswith(b"RIFF"):
            wav_bytes = pcm_s16le_to_wav_bytes(audio_bytes, sample_rate, channels)
        else:
            wav_bytes = audio_bytes

        filename = f"chunk_{track_id}_{start_ms}_{end_ms}.wav"
        res = await self.stt_adapter.transcribe_audio(
            wav_bytes,
            filename=filename,
            content_type="audio/wav",
            language=language,
        )

        cleaned_text = res.text.strip()
        # Filter out empty text, punctuation, and known Whisper silence hallucinations
        WHISPER_HALLUCINATIONS = {
            "продолжение следует...",
            "продолжение следует",
            "субтитры сделал",
            "спасибо за просмотр",
            "спасибо за просмотр!",
            "до скорых встреч!",
            "до скорых встреч",
            "редактор субтитров",
        }
        if (
            not cleaned_text
            or cleaned_text in (".", "...", ",", "!", "?", "—", "-")
            or cleaned_text.lower() in WHISPER_HALLUCINATIONS
        ):
            logger.debug("Transcribed chunk %s produced empty/hallucination text, skipping segment.", filename)
            return grouped_job_ids

        if not self.repo.get_interview(interview_id):
            logger.warning("Interview %s was deleted before saving transcript segment.", interview_id)
            return grouped_job_ids

        segment_id = payload.get("segment_id", f"seg-{uuid.uuid4().hex[:8]}")
        speaker_role = payload.get("speaker_role")
        if not speaker_role:
            speaker_role = "unknown" if str(track_id).lower() == "shared" else str(track_id).lower()

        self.repo.add_transcript_segment(
            segment_id=segment_id,
            interview_id=interview_id,
            track_id=track_id,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            text=cleaned_text,
            is_final=True,
            speaker_role=speaker_role,
        )
        return grouped_job_ids


    async def _handle_evaluate(self, interview_id: str, payload: dict[str, Any]) -> None:
        inv = self.repo.get_interview(interview_id)
        if not inv:
            logger.warning("Interview %s was deleted before evaluation. Discarding.", interview_id)
            return

        def is_candidate_segment(s: dict[str, Any]) -> bool:
            role = str(s.get("speaker_role", "")).lower()
            if role == "candidate":
                return True
            if role in ("interviewer", "unknown"):
                return False
            track = str(s.get("track_id", "")).lower()
            return track in ("candidate", "tracktype.candidate")

        question_id = payload["question_id"]
        rubric_rev = payload.get("rubric_revision_id") or inv.get("active_rubric_revision_id") or "rub-rev-1"
        trans_rev = payload.get("transcript_revision_id") or inv.get("active_transcript_revision_id") or "trans-rev-1"

        # 1. Fetch Question details and criteria from latest interview plan
        plan_dict = self.repo.get_latest_plan(interview_id)
        question_text = ""
        criteria = []
        questions = []
        if plan_dict and "payload" in plan_dict:
            questions = plan_dict["payload"].get("questions", [])
            for q in questions:
                if q.get("id") == question_id:
                    question_text = q.get("prompt") or q.get("text", "")
                    criteria = q.get("criteria", [])
                    break

        rubric_description = payload.get("rubric_description")
        if not rubric_description:
            if criteria:
                rubric_description = "; ".join(f"{c.get('id')}: {c.get('title')}" for c in criteria)
            else:
                rubric_description = "Technical depth & correctness"

        # 2. Fetch transcript segments and associations for this interview
        all_segments = self.repo.get_transcript_segments(interview_id, revision_id=trans_rev)
        if not all_segments:
            all_segments = self.repo.get_transcript_segments(interview_id)
        assocs = self.repo.get_associations(interview_id)

        # Identify candidate speech segments associated with this question (strictly exclude interviewer/unknown)
        associated_seg_ids = {a["segment_id"] for a in assocs if a.get("question_id") == question_id}
        candidate_segments_data: list[dict[str, Any]] = [
            s for s in all_segments
            if s["id"] in associated_seg_ids and is_candidate_segment(s)
        ]

        # If no associations for this question, run QuestionMatcher across all plan questions
        if not candidate_segments_data and questions and all_segments:
            matcher = QuestionMatcher()
            match_results = matcher.associate_segments(questions, all_segments, existing_associations=assocs)
            for r in match_results:
                assoc_id = f"assoc-{uuid.uuid4().hex[:8]}"
                self.repo.save_association(
                    assoc_id=assoc_id,
                    interview_id=interview_id,
                    question_id=r.question_id,
                    segment_id=r.segment_id,
                    confidence=r.confidence,
                    is_ambiguous=r.is_ambiguous,
                    is_manually_adjusted=False,
                    notes=r.notes,
                    revision_id=trans_rev,
                )
            assocs = self.repo.get_associations(interview_id)
            associated_seg_ids = {a["segment_id"] for a in assocs if a.get("question_id") == question_id}
            candidate_segments_data = [
                s for s in all_segments
                if s["id"] in associated_seg_ids and is_candidate_segment(s)
            ]

        # Backwards-compatibility fallback if provided in payload directly (e.g. mock unit tests)
        if not candidate_segments_data and payload.get("candidate_text"):
            candidate_segments_data = [
                {
                    "id": payload.get("segment_id", "seg-default"),
                    "track_id": "candidate",
                    "start_time_ms": 0,
                    "end_time_ms": 10000,
                    "text": payload["candidate_text"],
                }
            ]

        first_crit_id = criteria[0]["id"] if criteria else "criterion-core"

        # If STILL no candidate speech segments for this question, record explicit unanswered proposal
        if not candidate_segments_data:
            logger.info("No candidate speech found for question %s in interview %s. Creating unanswered proposal.", question_id, interview_id)
            empty_scores = [
                {
                    "criterion_id": c.get("id", first_crit_id),
                    "score": None,
                    "explanation": "Ответ кандидата отсутствует (нет сопоставленных сегментов речи).",
                    "evidence": [],
                }
                for c in (criteria if criteria else [{"id": first_crit_id}])
            ]
            prop_id = f"prop-{uuid.uuid4().hex[:8]}"
            provider_val = getattr(self.provider, "id", None)
            prov_id = provider_val if isinstance(provider_val, str) else "system"
            model_val = getattr(getattr(self.llm_adapter, "model", None), "upstream_model_id", None)
            if not isinstance(model_val, str):
                model_val = getattr(self.llm_adapter, "model_id", None)
            mod_id = model_val if isinstance(model_val, str) else "system-no-answer"

            self.repo.save_assessment_proposal(
                proposal_id=prop_id,
                interview_id=interview_id,
                question_id=question_id,
                model_profile_id=mod_id,
                scores=empty_scores,
                critical_errors=[],
                rubric_revision_id=rubric_rev,
                transcript_revision_id=trans_rev,
                is_rejected=False,
                validation_errors=[],
                provider_id=prov_id,
            )
            return

        # 3. Build candidate speech text with explicit individual segment IDs
        transcript_content = "\n".join(
            f"[{s['id']}]: \"{s.get('text', '').strip()}\""
            for s in candidate_segments_data
        )
        primary_seg_id = candidate_segments_data[0]["id"]

        schema_desc = f"""
Respond strictly with a JSON object conforming to:
{{
  "scores": [
    {{
      "criterion_id": "{first_crit_id}",
      "score": 5.0,
      "explanation": "...",
      "evidence": [{{"segment_id": "{primary_seg_id}", "exact_quote": "..."}}]
    }}
  ],
  "critical_errors": []
}}
"""
        criteria_prompt = ""
        if criteria:
            criteria_prompt = "Критерии рубрики:\n" + "\n".join(
                f"- ID: '{c.get('id')}', Название: '{c.get('title')}', Вес: {c.get('weight', 1.0)}"
                for c in criteria
            )
        else:
            criteria_prompt = f"Рубрика: {rubric_description}"

        messages = [
            {
                "role": "system",
                "content": (
                    f"Ты эксперт технической оценки собеседований. "
                    f"Оцени ответ кандидата на вопрос: \"{question_text or rubric_description}\".\n"
                    f"{criteria_prompt}\n"
                    f"ВАЖНО:\n"
                    f"1. Шкала оценок: от 1.0 (минимум) до 5.0 (максимум).\n"
                    f"2. Для каждого критерия обязательно укажи segment_id и дословную точную цитату exact_quote из речи кандидата.\n"
                    f"3. Цитата обязана строго и дословно совпадать с текстом соответствующего сегмента кандидата. Не придумывай цитаты!\n"
                    f"{schema_desc}"
                ),
            },
            {
                "role": "user",
                "content": f"Речь кандидата по вопросу:\n{transcript_content}",
            },
        ]

        # 4. Execute request through resilient LLM adapter
        if isinstance(self.llm_adapter, ResilientLLMAdapter):
            llm_res, actual_model_id = await self.llm_adapter.execute_request(
                messages=messages,
                json_schema={
                    "type": "object",
                    "properties": {
                        "scores": {"type": "array"},
                        "critical_errors": {"type": "array"},
                    },
                    "required": ["scores", "critical_errors"],
                },
                schema_name="assessment_schema",
            )
            fallback_metadata = self.llm_adapter.last_fallback_event
            if fallback_metadata:
                self.repo.record_audit_event(
                    event_id=f"audit-{uuid.uuid4().hex[:8]}",
                    interview_id=interview_id,
                    event_type="MODEL_FALLBACK_TRIGGERED",
                    payload=fallback_metadata,
                )
        else:
            raw_res = await self.llm_adapter.execute_request(
                messages=messages,
                json_schema={
                    "type": "object",
                    "properties": {
                        "scores": {"type": "array"},
                        "critical_errors": {"type": "array"},
                    },
                    "required": ["scores", "critical_errors"],
                },
                schema_name="assessment_schema",
            )
            fallback_metadata = None
            if isinstance(raw_res, tuple):
                llm_res, actual_model_id = raw_res
            else:
                llm_res = raw_res
                actual_model_id = getattr(getattr(self.llm_adapter, "model", None), "upstream_model_id", "mock-model")

        parsed_json = llm_res.get("data", llm_res) if isinstance(llm_res, dict) else {}
        scores = parsed_json.get("scores", [])
        critical_errors = parsed_json.get("critical_errors", [])

        # 5. Programmatic Evidence Validation
        proposal_scores_objs: list[CriterionScoreProposal] = []
        for s in scores:
            ev_list = []
            for ev in s.get("evidence", []):
                ev_list.append(
                    EvidenceRef(
                        segment_id=ev.get("segment_id", ""),
                        exact_quote=ev.get("exact_quote", ""),
                        start_char=ev.get("start_char"),
                        end_char=ev.get("end_char"),
                    )
                )
            proposal_scores_objs.append(
                CriterionScoreProposal(
                    criterion_id=s.get("criterion_id", first_crit_id),
                    score=float(s["score"]) if s.get("score") is not None else None,
                    explanation=s.get("explanation", ""),
                    evidence=ev_list,
                )
            )

        proposal = AssessmentProposal(
            id=f"prop-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            question_id=question_id,
            rubric_revision_id=rubric_rev,
            transcript_revision_id=trans_rev,
            model_profile_id=actual_model_id,
            scores=proposal_scores_objs,
            critical_errors=critical_errors,
        )

        # Build TranscriptRevision with all segments available in interview
        transcript_segment_objs: list[TranscriptSegment] = []
        source_segments = all_segments if all_segments else candidate_segments_data
        for s in source_segments:
            cand = is_candidate_segment(s)
            track = TrackType.CANDIDATE if cand else TrackType.INTERVIEWER
            transcript_segment_objs.append(
                TranscriptSegment(
                    id=s["id"],
                    track_id=track,
                    start_time_ms=s.get("start_time_ms", 0),
                    end_time_ms=s.get("end_time_ms", 0),
                    text=s.get("text", "").strip(),
                    speaker_role="candidate" if cand else str(s.get("speaker_role", "unknown")),
                )
            )

        transcript_obj = TranscriptRevision(
            revision_id=trans_rev,
            interview_id=interview_id,
            segments=transcript_segment_objs,
        )

        allowed_criteria_ids = {c["id"] for c in criteria if "id" in c} if criteria else None
        validation = validate_proposal(proposal, transcript_obj, allowed_criteria_ids=allowed_criteria_ids)

        is_rejected = not validation.is_valid
        validation_errors = validation.errors if not validation.is_valid else []
        if is_rejected:
            logger.warning("Evidence validation failed for proposal %s: %s", proposal.id, validation.errors)

        current_inv = self.repo.get_interview(interview_id)
        if not current_inv:
            logger.warning("Interview %s was deleted before saving evaluation proposal.", interview_id)
            return

        is_stale = False
        stale_reason = None
        curr_rub = current_inv.get("active_rubric_revision_id") or "rub-rev-1"
        curr_trans = current_inv.get("active_transcript_revision_id") or "trans-rev-1"
        if curr_rub != rubric_rev or curr_trans != trans_rev:
            is_stale = True
            stale_reason = f"Revisions changed during evaluation (eval: {rubric_rev}/{trans_rev}, current: {curr_rub}/{curr_trans})"
            logger.warning("Proposal %s is stale: %s", proposal.id, stale_reason)

        provider_val = getattr(self.provider, "id", None)
        prov_id = provider_val if isinstance(provider_val, str) else "system"
        mod_id = actual_model_id if isinstance(actual_model_id, str) else str(actual_model_id)

        self.repo.save_assessment_proposal(
            proposal_id=proposal.id,
            interview_id=interview_id,
            question_id=question_id,
            model_profile_id=mod_id,
            scores=scores,
            critical_errors=critical_errors,
            rubric_revision_id=rubric_rev,
            transcript_revision_id=trans_rev,
            is_stale=is_stale,
            stale_reason=stale_reason,
            is_rejected=is_rejected,
            validation_errors=validation_errors,
            provider_id=prov_id,
            fallback_metadata=fallback_metadata,
        )

    async def _handle_batch_retranscribe(self, interview_id: str, payload: dict[str, Any]) -> None:
        existing_revs = self.repo.get_transcript_revisions(interview_id)
        existing_rev_ids = {r["id"] for r in existing_revs}
        new_rev_id = payload.get("new_revision_id")
        if not new_rev_id or new_rev_id in existing_rev_ids:
            next_num = len(existing_revs) + 1
            while f"trans-rev-{next_num}" in existing_rev_ids:
                next_num += 1
            new_rev_id = f"trans-rev-{next_num}"
            revision_number = next_num
        else:
            revision_number = payload.get("revision_number", len(existing_revs) + 1)

        old_rev_id = payload.get("old_revision_id", "trans-rev-1")

        if not self.repo.get_interview(interview_id):
            logger.warning("Interview %s was deleted before batch retranscribe.", interview_id)
            return

        self.repo.create_transcript_revision(
            revision_id=new_rev_id,
            interview_id=interview_id,
            revision_number=revision_number,
            is_batch_final=True,
        )

        # Ingest new segments
        new_segments = payload.get("segments")
        provenance = payload.get("provenance") or (
            "manual_import" if (new_segments is not None and len(new_segments) > 0) else "batch_stt"
        )
        if not new_segments:
            chunks = self.repo.get_audio_chunks(interview_id)
            new_segments = []
            for c in chunks:
                fp_str = c.get("file_path")
                if fp_str and Path(fp_str).exists():
                    raw_bytes = Path(fp_str).read_bytes()
                    sr = c.get("sample_rate", 16000)
                    ch = c.get("channels", 1)
                    fmt = c.get("format", "pcm_s16le")
                    if fmt == "pcm_s16le" or not raw_bytes.startswith(b"RIFF"):
                        wav_bytes = pcm_s16le_to_wav_bytes(raw_bytes, sr, ch)
                    else:
                        wav_bytes = raw_bytes

                    res = await self.stt_adapter.transcribe_audio(
                        wav_bytes,
                        filename=f"batch_{c['track_id']}_{c['sequence']}.wav",
                        content_type="audio/wav",
                    )
                    text_val = res.text.strip()
                    if text_val:
                        new_segments.append({
                            "id": f"seg-b-{c['track_id']}-{c['sequence']}",
                            "track_id": c["track_id"],
                            "start_time_ms": c["start_time_ms"],
                            "end_time_ms": c["end_time_ms"],
                            "text": text_val,
                        })

        old_segs = self.repo.get_transcript_segments(interview_id, revision_id=old_rev_id)
        if not old_segs:
            old_segs = self.repo.get_transcript_segments(interview_id)

        for seg in new_segments:
            assigned_role = seg.get("speaker_role")
            if not assigned_role or assigned_role == "unknown":
                t_id = seg.get("track_id", "candidate")
                if t_id in ("candidate", "interviewer"):
                    assigned_role = t_id
                else:
                    seg_start = seg.get("start_time_ms", 0)
                    seg_end = seg.get("end_time_ms", 0)
                    best_overlap = 0
                    inherited_role = "unknown"
                    for old_s in old_segs:
                        old_role = old_s.get("speaker_role")
                        if old_role in ("candidate", "interviewer"):
                            o_start = max(seg_start, old_s.get("start_time_ms", 0))
                            o_end = min(seg_end, old_s.get("end_time_ms", 0))
                            overlap = max(0, o_end - o_start)
                            if overlap > best_overlap:
                                best_overlap = overlap
                                inherited_role = old_role
                    assigned_role = inherited_role if best_overlap > 0 else "unknown"

            self.repo.add_transcript_segment(
                segment_id=seg["id"],
                interview_id=interview_id,
                track_id=seg.get("track_id", "candidate"),
                start_time_ms=seg.get("start_time_ms", 0),
                end_time_ms=seg.get("end_time_ms", 0),
                text=seg.get("text", "").strip(),
                is_final=True,
                revision_id=new_rev_id,
                speaker_role=assigned_role,
            )

        # Run TranscriptDiffEngine
        diff_engine = TranscriptDiffEngine()
        new_segs = self.repo.get_transcript_segments(interview_id, revision_id=new_rev_id)
        segment_diffs = diff_engine.compare_revisions(old_segs, new_segs)

        # Check existing associations / questions
        assocs = self.repo.get_associations(interview_id)
        existing_proposals = self.repo.get_assessment_proposals(interview_id)

        # Group segments by question
        questions_map: dict[str, list[str]] = {}
        for a in assocs:
            q_id = a.get("question_id")
            s_id = a.get("segment_id")
            if q_id and s_id:
                questions_map.setdefault(q_id, []).append(s_id)

        if not questions_map:
            plan = self.repo.get_latest_plan(interview_id)
            if plan and "payload" in plan:
                for q in plan["payload"].get("questions", []):
                    questions_map[q["id"]] = [s["id"] for s in old_segs]

        modified_questions = set()
        question_diff_reports = []

        for q_id, s_ids in questions_map.items():
            existing_evidence = []
            for p in existing_proposals:
                if p["question_id"] == q_id:
                    for sc in p.get("scores", []):
                        existing_evidence.extend(sc.get("evidence", []))

            report = diff_engine.evaluate_question_diff(
                question_id=q_id,
                question_segment_ids=s_ids,
                segment_diffs=segment_diffs,
                existing_evidence=existing_evidence,
                new_segments=new_segs,
            )
            question_diff_reports.append(report)
            if report.is_modified:
                modified_questions.add(q_id)

        # Mark modified proposals and human assessments as stale without deleting human overrides!
        if modified_questions:
            self.repo.mark_proposals_stale(
                interview_id=interview_id,
                question_ids=list(modified_questions),
                stale_reason=f"Стенограмма обновлена до {new_rev_id}. Обнаружены расхождения в тексте.",
            )

        if not self.repo.get_interview(interview_id):
            logger.warning("Interview %s deleted during retranscribe. Aborting activation.", interview_id)
            return

        committed_new_segs = self.repo.get_transcript_segments(interview_id, revision_id=new_rev_id)
        if not committed_new_segs:
            logger.warning(
                "Retranscription for interview %s yielded 0 segments. Aborting activation to prevent empty transcript.",
                interview_id,
            )
            return

        # ONLY AFTER ALL DATA AND STATUSES ARE SAVED, ACTIVATE NEW REVISION!
        self.repo.set_active_transcript_revision(interview_id, new_rev_id)

        self.repo.record_audit_event(
            event_id=f"audit-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            event_type="BATCH_RETRANSCRIPTION_COMPLETED",
            payload={
                "provenance": provenance,
                "old_revision_id": old_rev_id,
                "new_revision_id": new_rev_id,
                "modified_questions": list(modified_questions),
                "diff_reports": [
                    {
                        "question_id": r.question_id,
                        "is_modified": r.is_modified,
                        "diff_ratio": r.diff_ratio,
                        "stale_reason": r.stale_reason,
                    }
                    for r in question_diff_reports
                ],
            },
        )

    async def _handle_generate_summary(self, interview_id: str, payload: dict[str, Any]) -> None:
        interview = self.repo.get_interview(interview_id)
        candidate_name = interview.get("candidate_name", "Кандидат") if interview else "Кандидат"
        role = interview.get("role", "Инженер") if interview else "Инженер"

        human_assessments = self.repo.get_human_assessments(interview_id)
        proposals = self.repo.get_assessment_proposals(interview_id)

        decisions_map: dict[str, dict[str, Any]] = {}
        for p in proposals:
            decisions_map[p["question_id"]] = p
        # Human decisions take precedence
        for h in human_assessments:
            decisions_map[h["question_id"]] = h

        summary_gen = ExecutiveSummaryGenerator(
            llm_adapter=self.llm_adapter if isinstance(self.llm_adapter, ResilientLLMAdapter) else None
        )
        summary_res = await summary_gen.generate_summary(
            candidate_name=candidate_name,
            role=role,
            decisions=list(decisions_map.values()),
            audio_health_summary=payload.get("audio_health", {}),
        )

        if not self.repo.get_interview(interview_id):
            logger.warning("Interview %s was deleted before saving summary proposal.", interview_id)
            return

        prop_id = f"sum-{uuid.uuid4().hex[:8]}"
        self.repo.save_summary_proposal(
            proposal_id=prop_id,
            interview_id=interview_id,
            model_profile_id=summary_res.get("model_profile_id", "google/gemini-3.8-flash"),
            summary_data=summary_res,
        )

    async def run_loop(self, poll_interval_sec: float = 1.0) -> None:
        self._running = True
        while self._running:
            did_work = await self.process_one_job()
            if not did_work:
                await asyncio.sleep(poll_interval_sec)
            else:
                await asyncio.sleep(0.3)

    def stop(self) -> None:
        self._running = False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    from backend.db.database import get_db
    from backend.db.repository import Repository
    from backend.adapters.stt import OpenAICompatibleSTTAdapter
    from backend.adapters.resilient_llm import ResilientLLMAdapter
    from backend.core.profiles import get_plusvibe_whisper_stt

    db = get_db()
    db.init_schema()
    repo = Repository(db)
    worker = PipelineWorker(repository=repo)
    logger.info("Starting Nebula Pipeline Worker daemon...")
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        logger.info("Pipeline Worker stopped by user.")

