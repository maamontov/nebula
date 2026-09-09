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

        try:
            if job_type == "TRANSCRIBE_AUDIO":
                await self._handle_transcribe(interview_id, payload)
            elif job_type == "EVALUATE_QUESTION":
                await self._handle_evaluate(interview_id, payload)
            else:
                raise ValueError(f"Unknown job type: {job_type}")

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
            llm_res = await self.llm_adapter.execute_request(
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
            actual_model_id = self.llm_adapter.model.upstream_model_id

        parsed_json = llm_res.get("data", {})
        scores = parsed_json.get("scores", [])
        critical_errors = parsed_json.get("critical_errors", [])

        # Validate with EvidenceValidator
        proposal = AssessmentProposal(
            id=f"prop-{uuid.uuid4().hex[:8]}",
            interview_id=interview_id,
            question_id=question_id,
            rubric_revision_id="rub-rev-1",
            transcript_revision_id="trans-rev-1",
            model_profile_id=actual_model_id,
            scores=scores,
            critical_errors=critical_errors,
        )

        transcript = TranscriptRevision(
            revision_id="trans-rev-1",
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

        self.repo.save_assessment_proposal(
            proposal_id=proposal.id,
            interview_id=interview_id,
            question_id=question_id,
            model_profile_id=actual_model_id,
            scores=scores,
            critical_errors=critical_errors,
        )

    async def run_loop(self, poll_interval_sec: float = 1.0) -> None:
        self._running = True
        while self._running:
            did_work = await self.process_one_job()
            if not did_work:
                await asyncio.sleep(poll_interval_sec)

    def stop(self) -> None:
        self._running = False
