"""
Durable Pipeline Worker for Nebula.
Processes background jobs with lease timeouts, retry backoff, and idempotent execution:
- TRANSCRIBE_AUDIO: Audio chunk transcription via STT adapter
- EVALUATE_QUESTION: Technical answer scoring via LLM adapter with EvidenceValidator checks
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from backend.adapters.llm import OpenAICompatibleAdapter
from backend.adapters.resilient_llm import ResilientLLMAdapter
from backend.adapters.stt import OpenAICompatibleSTTAdapter
from backend.core.evidence_validator import validate_proposal
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
    TranscriptRevision,
    TranscriptSegment,
)

logger = logging.getLogger("nebula.pipeline")


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
        """Claims and executes a single job. Returns True if job was processed."""
        job = self.repo.claim_next_job(lock_duration_sec=60)
        if not job:
            return False

        job_id = job["id"]
        job_type = job["type"]
        interview_id = job["interview_id"]
        payload = job["payload"]

        # Check if interview exists before processing (privacy lifecycle protection)
        interview = self.repo.get_interview(interview_id)
        if not interview:
            logger.warning("Interview %s was deleted. Discarding job %s without saving results.", interview_id, job_id)
            self.repo.fail_job(job_id, "Interview was deleted (privacy lifecycle)")
            return True

        try:
            if job_type == "TRANSCRIBE_AUDIO":
                await self._handle_transcribe(interview_id, payload)
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
                self.repo.fail_job(job_id, "Interview was deleted during execution")
                return True

            self.repo.complete_job(job_id)
            self.repo.record_audit_event(
                event_id=f"audit-{uuid.uuid4().hex[:8]}",
                interview_id=interview_id,
                event_type=f"JOB_COMPLETED_{job_type}",
                payload={"job_id": job_id},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Job %s (%s) failed: %s", job_id, job_type, exc)
            self.repo.fail_job(job_id, str(exc))
            self.repo.record_audit_event(
                event_id=f"audit-{uuid.uuid4().hex[:8]}",
                interview_id=interview_id,
                event_type=f"JOB_FAILED_{job_type}",
                payload={"job_id": job_id, "error": str(exc)},
            )
            return False

    async def _handle_transcribe(self, interview_id: str, payload: dict[str, Any]) -> None:
        audio_bytes = bytes.fromhex(payload["audio_hex"])
        track_id = payload.get("track_id", "candidate")
        start_ms = payload.get("start_ms", 0)
        end_ms = payload.get("end_ms", 0)
        language = payload.get("language", "ru")

        res = await self.stt_adapter.transcribe_audio(
            audio_bytes,
            filename="chunk.wav",
            content_type="audio/wav",
            language=language,
        )

        if not self.repo.get_interview(interview_id):
            logger.warning("Interview %s was deleted before saving transcript segment.", interview_id)
            return

        segment_id = payload.get("segment_id", f"seg-{uuid.uuid4().hex[:8]}")
        self.repo.add_transcript_segment(
            segment_id=segment_id,
            interview_id=interview_id,
            track_id=track_id,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            text=res.text.strip(),
            is_final=True,
        )

    async def _handle_evaluate(self, interview_id: str, payload: dict[str, Any]) -> None:
        if not self.repo.get_interview(interview_id):
            logger.warning("Interview %s was deleted before evaluation. Discarding.", interview_id)
            return

        question_id = payload["question_id"]
        candidate_text = payload["candidate_text"]
        segment_id = payload.get("segment_id", "seg-default")
        rubric_description = payload.get("rubric_description", "Technical depth & correctness")

        # Build prompt & schema
        schema_desc = """
Respond strictly with a JSON object conforming to:
{
  "scores": [
    {
      "criterion_id": "criterion-core",
      "score": 5.0,
      "explanation": "...",
      "evidence": [{"segment_id": "''' + segment_id + '''", "exact_quote": "..."}]
    }
  ],
  "critical_errors": []
}
"""
        messages = [
            {
                "role": "system",
                "content": f"Ты эксперт технической оценки собеседований. Оцени ответ кандидата по рубрике: {rubric_description}. Обязательно включи дословную точную цитату exact_quote из ответа с указанием segment_id='{segment_id}'.\n{schema_desc}",
            },
            {
                "role": "user",
                "content": f"Транскрипт кандидата [{segment_id}]: \"{candidate_text}\"",
            },
        ]

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
            if self.llm_adapter.last_fallback_event:
                self.repo.record_audit_event(
                    event_id=f"audit-{uuid.uuid4().hex[:8]}",
                    interview_id=interview_id,
                    event_type="MODEL_FALLBACK_TRIGGERED",
                    payload=self.llm_adapter.last_fallback_event,
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
            if isinstance(raw_res, tuple):
                llm_res, actual_model_id = raw_res
            else:
                llm_res = raw_res
                actual_model_id = getattr(getattr(self.llm_adapter, "model", None), "upstream_model_id", "mock-model")

        parsed_json = llm_res.get("data", llm_res) if isinstance(llm_res, dict) else {}
        scores = parsed_json.get("scores", [])
        critical_errors = parsed_json.get("critical_errors", [])

        # Validate with EvidenceValidator
        rubric_rev = payload.get("rubric_revision_id", "rub-rev-1")
        trans_rev = payload.get("transcript_revision_id", "trans-rev-1")

        proposal = AssessmentProposal(
            id=f"prop-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            question_id=question_id,
            rubric_revision_id=rubric_rev,
            transcript_revision_id=trans_rev,
            model_profile_id=actual_model_id,
            scores=scores,
            critical_errors=critical_errors,
        )

        transcript = TranscriptRevision(
            revision_id=trans_rev,
            interview_id=interview_id,
            segments=[
                TranscriptSegment(
                    id=segment_id,
                    track_id=TrackType.CANDIDATE,
                    start_time_ms=0,
                    end_time_ms=10000,
                    text=candidate_text,
                )
            ],
        )

        validation = validate_proposal(proposal, transcript)
        if not validation.is_valid:
            logger.warning("Evidence validation failed for proposal %s: %s", proposal.id, validation.errors)

        if not self.repo.get_interview(interview_id):
            logger.warning("Interview %s was deleted before saving evaluation proposal.", interview_id)
            return

        self.repo.save_assessment_proposal(
            proposal_id=proposal.id,
            interview_id=interview_id,
            question_id=question_id,
            model_profile_id=actual_model_id,
            scores=scores,
            critical_errors=critical_errors,
            rubric_revision_id=rubric_rev,
            transcript_revision_id=trans_rev,
        )

    async def _handle_batch_retranscribe(self, interview_id: str, payload: dict[str, Any]) -> None:
        new_rev_id = payload.get("new_revision_id", "trans-rev-2")
        old_rev_id = payload.get("old_revision_id", "trans-rev-1")
        revision_number = payload.get("revision_number", 2)

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
        new_segments = payload.get("segments", [])
        for seg in new_segments:
            self.repo.add_transcript_segment(
                segment_id=seg["id"],
                interview_id=interview_id,
                track_id=seg.get("track_id", "candidate"),
                start_time_ms=seg.get("start_time_ms", 0),
                end_time_ms=seg.get("end_time_ms", 0),
                text=seg.get("text", "").strip(),
                is_final=True,
                revision_id=new_rev_id,
            )

        # Run TranscriptDiffEngine
        diff_engine = TranscriptDiffEngine()
        old_segs = self.repo.get_transcript_segments(interview_id, revision_id=old_rev_id)
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

        self.repo.record_audit_event(
            event_id=f"audit-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            event_type="BATCH_RETRANSCRIPTION_COMPLETED",
            payload={
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

