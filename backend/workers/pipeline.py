"""
Durable Pipeline Worker for Nebula.
Processes background jobs with lease timeouts, retry backoff, and idempotent execution:
- TRANSCRIBE_AUDIO: Audio chunk transcription via STT adapter
- EVALUATE_QUESTION: Technical answer scoring via LLM adapter with EvidenceValidator checks
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import struct
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Ensure environment variables (.env) are loaded
load_dotenv()

import time
from datetime import UTC, datetime

import httpx

from backend.adapters.llm import (
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMTransientError,
    OpenAICompatibleAdapter,
)
from backend.adapters.resilient_llm import ResilientLLMAdapter
from backend.adapters.stt import (
    OpenAICompatibleSTTAdapter,
    STTAuthenticationError,
    STTRateLimitError,
    STTTransientError,
)
from backend.core.audio_utils import pcm_s16le_to_wav_bytes
from backend.core.evidence_validator import validate_proposal
from backend.core.followup_generator import (
    FollowUpContext,
    format_followup_prompt,
    validate_followup_response,
)
from backend.core.matcher import QuestionMatcher
from backend.core.profiles import (
    get_plusvibe_gemini_model,
    get_plusvibe_provider,
    get_plusvibe_whisper_stt,
)
from backend.core.revisions import TranscriptDiffEngine
from backend.core.summary_generator import ExecutiveSummaryGenerator
from backend.core.turn_assembler import (
    AssembledTurn,
    AudioChunkRef,
    TurnAssembler,
    TurnAssemblyCursor,
)
from backend.db.repository import Repository, RepositoryConflictError
from contracts.audio import TrackType
from contracts.domain import (
    AssessmentProposal,
    CriterionScoreProposal,
    EvidenceRef,
    PlannedQuestion,
    RubricCriterion,
    TranscriptRevision,
    TranscriptSegment,
)
from contracts.followups import (
    FollowUpLLMResponse,
    FollowUpMode,
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


DEFAULT_JOB_DEADLINES: dict[str, float | None] = {
    "TRANSCRIBE_AUDIO": 30.0,
    "TRANSCRIBE_TURN": 30.0,
    "EVALUATE_QUESTION": 30.0,
    "GENERATE_FOLLOWUPS": 20.0,
    "GENERATE_SUMMARY": 60.0,
    "BATCH_RETRANSCRIBE": None,
}


class PipelineWorker:
    def __init__(
        self,
        repository: Repository,
        stt_adapter: OpenAICompatibleSTTAdapter | None = None,
        llm_adapter: OpenAICompatibleAdapter | ResilientLLMAdapter | None = None,
        followup_llm_adapter: OpenAICompatibleAdapter | None = None,
        http_client: httpx.AsyncClient | None = None,
        provider_concurrency_cap: int = 5,
    ) -> None:
        self.repo = repository
        self.provider = get_plusvibe_provider()
        self._provider_concurrency_cap = provider_concurrency_cap
        self._provider_semaphore = asyncio.Semaphore(provider_concurrency_cap)
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0)
        )

        self.stt_adapter = stt_adapter or OpenAICompatibleSTTAdapter(
            get_plusvibe_whisper_stt(),
            api_key_env="PLUSVIBE_API_KEY",
            http_client=self._http_client,
        )
        self.llm_adapter = llm_adapter or ResilientLLMAdapter(http_client=self._http_client)
        self.followup_llm_adapter = followup_llm_adapter or OpenAICompatibleAdapter(
            self.provider,
            get_plusvibe_gemini_model(),
            http_client=self._http_client,
        )
        self._running = False
        self._stop_event = asyncio.Event()

    async def _lease_heartbeat(
        self,
        job_id: str,
        owner_token: str,
        grouped_ids: list[str],
        interval_sec: float = 15.0,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(interval_sec)
                ok = self.repo.renew_job_lease(job_id, owner_token, extension_sec=60)
                if not ok:
                    logger.warning("Lease renewal failed for job %s (lost ownership or expired). Signalling abort.", job_id)
                    if cancel_event:
                        cancel_event.set()
                    break
                for gid in list(grouped_ids):
                    self.repo.renew_job_lease(gid, owner_token, extension_sec=60)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error renewing lease for job %s: %s", job_id, exc)

    async def process_one_job(
        self,
        include_types: list[str] | tuple[str, ...] | None = None,
        exclude_types: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """Claims and executes a single job with atomic lease lock. Returns True if job was processed."""
        job = self.repo.claim_next_job(
            lock_duration_sec=60,
            include_types=include_types,
            exclude_types=exclude_types,
        )
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

        active_grouped_ids: list[str] = []
        heartbeat_task = None
        cancel_event = asyncio.Event()
        if owner_token:
            heartbeat_task = asyncio.create_task(
                self._lease_heartbeat(job_id, owner_token, active_grouped_ids, cancel_event=cancel_event)
            )

        t_start = time.perf_counter()
        now_dt = datetime.now(UTC)
        queue_wait_ms: int | None = None
        if job.get("created_at"):
            with contextlib.suppress(Exception):
                c_dt = datetime.fromisoformat(job["created_at"])
                queue_wait_ms = max(0, int((now_dt - c_dt).total_seconds() * 1000))

        try:
            async def _run_handler():
                if job_type == "TRANSCRIBE_AUDIO":
                    return await self._handle_transcribe_audio(
                        interview_id, payload, owner_token=owner_token, job_id=job_id
                    )
                elif job_type == "TRANSCRIBE_TURN":
                    return await self._handle_transcribe_turn(
                        interview_id, payload, owner_token=owner_token, job_id=job_id
                    )
                elif job_type == "EVALUATE_QUESTION":
                    return await self._handle_evaluate(interview_id, payload, owner_token=owner_token, job_id=job_id)
                elif job_type == "BATCH_RETRANSCRIBE":
                    return await self._handle_batch_retranscribe(interview_id, payload, owner_token=owner_token, job_id=job_id)
                elif job_type == "GENERATE_SUMMARY":
                    return await self._handle_generate_summary(interview_id, payload, owner_token=owner_token, job_id=job_id)
                elif job_type == "GENERATE_FOLLOWUPS":
                    return await self._handle_generate_followups(interview_id, payload, owner_token=owner_token, job_id=job_id)
                else:
                    raise ValueError(f"Unknown job type: {job_type}")

            deadline_sec = DEFAULT_JOB_DEADLINES.get(job_type, 60.0)
            if deadline_sec is not None and deadline_sec > 0:
                res_meta = await asyncio.wait_for(_run_handler(), timeout=deadline_sec)
            else:
                res_meta = await _run_handler()

            if cancel_event.is_set():
                raise RepositoryConflictError(f"Job {job_id} lease lost during execution, aborting commit")

            handler_meta: dict[str, Any] = {}
            if isinstance(res_meta, dict):
                handler_meta.update(res_meta)

            # Re-verify interview wasn't deleted while job was running
            if not self.repo.get_interview(interview_id):
                logger.warning("Interview %s deleted during execution. Discarding job %s.", interview_id, job_id)
                self.repo.fail_job(job_id, "Interview was deleted during execution", owner_token=owner_token)
                return True

            execution_duration_ms = int((time.perf_counter() - t_start) * 1000)
            self.repo.complete_job(job_id, owner_token=owner_token)
            completed_payload: dict[str, Any] = {
                "job_id": job_id,
                "attempt": job.get("attempts", 1),
                "queue_wait_ms": queue_wait_ms,
                "execution_duration_ms": execution_duration_ms,
            }
            completed_payload.update(handler_meta)
            self.repo.record_audit_event(
                event_id=f"audit-{uuid.uuid4().hex[:8]}",
                interview_id=interview_id,
                event_type=f"JOB_COMPLETED_{job_type}",
                payload=completed_payload,
            )
            return True
        except Exception as exc:
            logger.exception("Job %s (%s) failed", job_id, job_type)
            is_terminal = getattr(exc, "is_terminal", False)
            if isinstance(exc, (LLMAuthenticationError, STTAuthenticationError, RepositoryConflictError)):
                is_terminal = True

            retry_delay_sec = 0
            if isinstance(
                exc,
                (STTRateLimitError, LLMRateLimitError, LLMTransientError, STTTransientError, asyncio.TimeoutError),
            ):
                retry_delay_sec = int(getattr(exc, "retry_after", 10.0) or 10.0)
            elif hasattr(exc, "retry_delay_sec"):
                retry_delay_sec = int(exc.retry_delay_sec)

            err_msg = str(exc).strip()
            if not err_msg and isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                err_msg = f"Job execution timed out (deadline {deadline_sec}s exceeded)"
            elif not err_msg:
                err_msg = exc.__class__.__name__

            try:
                self.repo.fail_job(
                    job_id,
                    err_msg,
                    owner_token=owner_token,
                    retry_delay_sec=retry_delay_sec,
                    is_terminal=is_terminal,
                )
            except RepositoryConflictError as rce:
                logger.warning("Could not fail job %s: %s", job_id, rce)

            for gid in active_grouped_ids:
                try:
                    self.repo.fail_job(
                        gid,
                        f"Parent batch job {job_id} failed: {err_msg}",
                        owner_token=owner_token,
                        is_terminal=is_terminal,
                        retry_delay_sec=retry_delay_sec,
                    )
                except (sqlite3.Error, RepositoryConflictError, RuntimeError) as fail_err:
                    logger.debug("Failed to mark child job %s as failed: %s", gid, fail_err)

            execution_duration_ms = int((time.perf_counter() - t_start) * 1000)
            self.repo.record_audit_event(
                event_id=f"audit-{uuid.uuid4().hex[:8]}",
                interview_id=interview_id,
                event_type=f"JOB_FAILED_{job_type}",
                payload={
                    "job_id": job_id,
                    "attempt": job.get("attempts", 1),
                    "queue_wait_ms": queue_wait_ms,
                    "execution_duration_ms": execution_duration_ms,
                    "error": str(exc),
                    "is_terminal": is_terminal,
                    "retry_delay_sec": retry_delay_sec,
                },
            )
            return False
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _handle_transcribe(
        self,
        interview_id: str,
        payload: dict[str, Any],
        owner_token: str | None = None,
        active_grouped_ids: list[str] | None = None,
    ) -> list[str]:
        """Legacy alias forwarding to _handle_transcribe_audio."""
        await self._handle_transcribe_audio(interview_id, payload, owner_token=owner_token)
        return []

    async def _handle_transcribe_audio(
        self,
        interview_id: str,
        payload: dict[str, Any],
        owner_token: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """
        Chunk ingestion event handler. Runs TurnAssembler on continuous audio chunks from cursor
        and atomically commits updated cursor and enqueues TRANSCRIBE_TURN jobs.
        """
        track_id = payload.get("track_id", "candidate")
        capture_epoch = payload.get("capture_epoch")
        if capture_epoch is None:
            capture_epoch = payload.get("epoch")
        sequence = payload.get("sequence")

        # Fallback resolution of sequence and epoch from audio_chunks if missing
        if capture_epoch is None or sequence is None:
            start_ms = payload.get("start_ms")
            end_ms = payload.get("end_ms")
            with self.repo.db.transaction() as conn:
                matches = conn.execute(
                    """
                    SELECT sequence, capture_epoch, track_id FROM audio_chunks
                    WHERE interview_id = ? AND track_id = ? AND start_time_ms = ? AND end_time_ms = ?
                    """,
                    (interview_id, track_id, start_ms, end_ms),
                ).fetchall()
                if len(matches) == 1:
                    sequence = matches[0]["sequence"]
                    capture_epoch = matches[0]["capture_epoch"]
                    track_id = matches[0]["track_id"]

        # Legacy fallback if still cannot identify chunk sequence in repository
        if capture_epoch is None or sequence is None:
            if "audio_hex" in payload:
                logger.info("Executing legacy direct STT fallback for job %s (missing epoch/sequence)", job_id)
                await self._handle_legacy_direct_transcribe(interview_id, payload)
                return
            raise ValueError(f"Cannot identify audio chunk sequence/epoch for TRANSCRIBE_AUDIO job {job_id}")

        inv = self.repo.get_interview(interview_id)
        if not inv or inv["status"] in ("finalized", "deleted"):
            return

        cursor_seq, cursor_off = self.repo.get_turn_assembly_cursor(interview_id, track_id, capture_epoch)
        chunk_records = self.repo.get_audio_chunks_for_assembly(
            interview_id=interview_id,
            track_id=track_id,
            capture_epoch=capture_epoch,
            from_sequence=cursor_seq,
        )
        if not chunk_records:
            return

        chunk_refs: list[AudioChunkRef] = []
        for cr in chunk_records:
            fp_str = cr.get("file_path")
            pcm_bytes = None
            if fp_str and Path(fp_str).exists():
                raw = Path(fp_str).read_bytes()
                pcm_bytes = raw[44:] if raw.startswith(b"RIFF") and len(raw) >= 44 else raw
            elif cr["sequence"] == sequence and "audio_hex" in payload:
                pcm_bytes = bytes.fromhex(payload["audio_hex"])

            if pcm_bytes is not None:
                chunk_refs.append(
                    AudioChunkRef(
                        sequence=cr["sequence"],
                        start_time_ms=cr["start_time_ms"],
                        end_time_ms=cr["end_time_ms"],
                        sample_rate=cr.get("sample_rate", 16000),
                        channels=cr.get("channels", 1),
                        format=cr.get("format", "pcm_s16le"),
                        pcm_bytes=pcm_bytes,
                    )
                )

        if not chunk_refs:
            return

        is_flush = bool(payload.get("is_flush", False))
        if not is_flush:
            spool_dir = Path(os.getenv("NEBULA_SPOOL_DIR", "data/spool")).resolve()
            manifest_path = spool_dir / interview_id / track_id / "manifest.json"
            if manifest_path.exists():
                try:
                    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    total_expected = manifest_data.get("total_chunks", 0)
                    if total_expected > 0 and chunk_records[-1]["sequence"] + 1 >= total_expected:
                        is_flush = True
                except Exception:
                    pass

        assembler = TurnAssembler()
        out = assembler.assemble(
            chunks=chunk_refs,
            cursor=TurnAssemblyCursor(cursor_seq, cursor_off),
            is_flush=is_flush,
            track_id=track_id,
            capture_epoch=capture_epoch,
            language=payload.get("language", "ru"),
        )

        self.repo.commit_turn_assembly_results(
            interview_id=interview_id,
            track_id=track_id,
            capture_epoch=capture_epoch,
            next_sequence=out.next_sequence,
            next_sample_offset=out.next_sample_offset,
            turns=out.turns,
            expected_sequence=cursor_seq,
            expected_sample_offset=cursor_off,
        )
        return {
            "assembled_turns_count": len(out.turns),
            "next_sequence": out.next_sequence,
        }

    async def _handle_transcribe_turn(
        self,
        interview_id: str,
        payload: dict[str, Any],
        owner_token: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Executes STT transcription for a coherent speech turn.
        Reconstructs PCM from audio chunks, calls STT adapter, filters hallucinations,
        and saves the transcript segment idempotently.
        """
        track_id = payload["track_id"]
        capture_epoch = payload["capture_epoch"]
        first_seq = payload["first_sequence"]
        first_off = payload["first_sample_offset"]
        last_seq = payload["last_sequence"]
        last_off = payload["last_sample_offset"]
        start_ms = payload["start_ms"]
        end_ms = payload["end_ms"]
        sample_rate = payload.get("sample_rate", 16000)
        channels = payload.get("channels", 1)
        language = payload.get("language", "ru")
        segment_id = payload.get("segment_id", f"seg-{uuid.uuid4().hex[:8]}")

        inv = self.repo.get_interview(interview_id)
        if not inv:
            logger.warning("Interview %s was deleted before transcribing turn %s.", interview_id, job_id)
            return None
        target_revision_id = payload.get("transcript_revision_id") or inv.get("active_transcript_revision_id") or "trans-rev-1"

        pcm_bytes = self.repo.get_turn_audio_pcm(
            interview_id=interview_id,
            track_id=track_id,
            capture_epoch=capture_epoch,
            first_sequence=first_seq,
            first_sample_offset=first_off,
            last_sequence=last_seq,
            last_sample_offset=last_off,
        )

        if not pcm_bytes:
            logger.debug("Turn %s has empty audio, skipping STT.", job_id)
            return {"audio_duration_ms": 0, "call_duration_ms": 0, "is_empty": True}

        wav_bytes = pcm_s16le_to_wav_bytes(pcm_bytes, sample_rate, channels)
        filename = f"turn_{track_id}_{start_ms}_{end_ms}.wav"

        async with self._provider_semaphore:
            res = await self.stt_adapter.transcribe_audio(
                wav_bytes,
                filename=filename,
                content_type="audio/wav",
                language=language,
            )

        cleaned_text = res.text.strip()
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
        audio_duration_ms = max(0, end_ms - start_ms)
        call_duration_ms = int(getattr(res, "latency_seconds", 0) * 1000)
        provider_id = getattr(getattr(self, "provider", None), "id", "system")
        upstream_model_id = getattr(res, "model_id", "default")

        if (
            not cleaned_text
            or cleaned_text in (".", "...", ",", "!", "?", "—", "-")
            or cleaned_text.lower() in WHISPER_HALLUCINATIONS
        ):
            logger.debug("Transcribed turn %s produced empty/hallucination text, skipping segment.", filename)
            return {
                "audio_duration_ms": audio_duration_ms,
                "call_duration_ms": call_duration_ms,
                "provider_id": provider_id,
                "upstream_model_id": upstream_model_id,
                "is_skipped": True,
            }

        speaker_role = payload.get("speaker_role")
        if not speaker_role:
            speaker_role = "unknown" if str(track_id).lower() == "shared" else str(track_id).lower()

        self.repo.save_turn_transcript_segment(
            segment_id=segment_id,
            interview_id=interview_id,
            track_id=track_id,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            text=cleaned_text,
            target_revision_id=target_revision_id,
            is_final=True,
            speaker_role=speaker_role,
            owner_token=owner_token,
            job_id=job_id,
        )
        return {
            "audio_duration_ms": audio_duration_ms,
            "call_duration_ms": call_duration_ms,
            "provider_id": provider_id,
            "upstream_model_id": upstream_model_id,
            "segment_id": segment_id,
        }

    async def _handle_legacy_direct_transcribe(
        self,
        interview_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Legacy direct transcription fallback for pending jobs without sequence/epoch."""
        audio_bytes = bytes.fromhex(payload["audio_hex"])
        track_id = payload.get("track_id", "candidate")
        start_ms = payload.get("start_ms", 0)
        end_ms = payload.get("end_ms", 0)
        sample_rate = payload.get("sample_rate", 16000)
        channels = payload.get("channels", 1)
        format_val = payload.get("format", "pcm_s16le")
        language = payload.get("language", "ru")

        if format_val == "pcm_s16le" or not audio_bytes.startswith(b"RIFF"):
            wav_bytes = pcm_s16le_to_wav_bytes(audio_bytes, sample_rate, channels)
        else:
            wav_bytes = audio_bytes

        filename = f"chunk_{track_id}_{start_ms}_{end_ms}.wav"
        async with self._provider_semaphore:
            res = await self.stt_adapter.transcribe_audio(
                wav_bytes,
                filename=filename,
                content_type="audio/wav",
                language=language,
            )

        cleaned_text = res.text.strip()
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
            return

        if not self.repo.get_interview(interview_id):
            return

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


    async def _handle_evaluate(
        self,
        interview_id: str,
        payload: dict[str, Any],
        owner_token: str | None = None,
        job_id: str | None = None,
    ) -> None:
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

        # 2. Fetch transcript segments and associations strictly for this revision
        all_segments = self.repo.get_transcript_segments(interview_id, revision_id=trans_rev)
        assocs = self.repo.get_associations(interview_id, revision_id=trans_rev)

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
            assocs = self.repo.get_associations(interview_id, revision_id=trans_rev)
            associated_seg_ids = {a["segment_id"] for a in assocs if a.get("question_id") == question_id}
            candidate_segments_data = [
                s for s in all_segments
                if s["id"] in associated_seg_ids and is_candidate_segment(s)
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
            prop_id = f"prop-eval-{job_id}" if job_id else f"prop-{uuid.uuid4().hex[:8]}"
            provider_val = getattr(self.provider, "id", None)
            prov_id = provider_val if isinstance(provider_val, str) else "system"
            model_val = getattr(getattr(self.llm_adapter, "model", None), "upstream_model_id", None)
            if not isinstance(model_val, str):
                model_val = getattr(self.llm_adapter, "model_id", None)
            mod_id = model_val if isinstance(model_val, str) else "default-evaluator"
            current_inv = self.repo.get_interview(interview_id)
            is_stale = False
            stale_reason = None
            if current_inv:
                curr_rub = current_inv.get("active_rubric_revision_id") or "rub-rev-1"
                curr_trans = current_inv.get("active_transcript_revision_id") or "trans-rev-1"
                if curr_rub != rubric_rev or curr_trans != trans_rev:
                    is_stale = True
                    stale_reason = f"Revision mismatch during evaluation (eval: {rubric_rev}/{trans_rev}, current: {curr_rub}/{curr_trans})"

            self.repo.save_assessment_proposal(
                proposal_id=prop_id,
                interview_id=interview_id,
                question_id=question_id,
                model_profile_id=mod_id,
                scores=empty_scores,
                critical_errors=[],
                rubric_revision_id=rubric_rev,
                transcript_revision_id=trans_rev,
                is_stale=is_stale,
                stale_reason=stale_reason,
                is_rejected=False,
                validation_errors=[],
                provider_id=prov_id,
                owner_token=owner_token,
                job_id=job_id,
            )
            return {
                "question_id": question_id,
                "call_duration_ms": 0,
                "provider_id": prov_id,
                "upstream_model_id": mod_id,
                "scores_count": len(empty_scores),
                "is_rejected": False,
                "is_stale": is_stale,
                "is_unanswered": True,
            }


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
        t_eval0 = time.perf_counter()
        async with self._provider_semaphore:
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

        call_duration_ms = int((time.perf_counter() - t_eval0) * 1000)

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

        prop_id = f"prop-eval-{job_id}" if job_id else f"prop-{uuid.uuid4().hex[:8]}"
        proposal = AssessmentProposal(
            id=prop_id,
            interview_id=interview_id,
            question_id=question_id,
            rubric_revision_id=rubric_rev,
            transcript_revision_id=trans_rev,
            model_profile_id=actual_model_id,
            scores=proposal_scores_objs,
            critical_errors=critical_errors,
        )

        # Build TranscriptRevision with only allowed candidate segments for this question
        transcript_segment_objs: list[TranscriptSegment] = []
        for s in candidate_segments_data:
            transcript_segment_objs.append(
                TranscriptSegment(
                    id=s["id"],
                    track_id=TrackType.CANDIDATE,
                    start_time_ms=s.get("start_time_ms", 0),
                    end_time_ms=s.get("end_time_ms", 0),
                    text=s.get("text", "").strip(),
                    speaker_role="candidate",
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
            owner_token=owner_token,
            job_id=job_id,
        )
        return {
            "question_id": question_id,
            "call_duration_ms": call_duration_ms,
            "provider_id": prov_id,
            "upstream_model_id": mod_id,
            "scores_count": len(scores),
            "is_rejected": is_rejected,
            "is_stale": is_stale,
        }

    async def _handle_batch_retranscribe(
        self,
        interview_id: str,
        payload: dict[str, Any],
        owner_token: str | None = None,
        job_id: str | None = None,
    ) -> None:
        inv = self.repo.get_interview(interview_id)
        if not inv or inv.get("status") == "deleted":
            logger.warning("Interview %s was deleted before batch retranscribe.", interview_id)
            return

        old_rev_id = payload.get("old_revision_id") or inv.get("active_transcript_revision_id") or "trans-rev-1"

        existing_revs = self.repo.get_transcript_revisions(interview_id)
        existing_rev_ids = {r["id"] for r in existing_revs}
        new_rev_id = payload.get("new_revision_id")
        if not new_rev_id:
            next_num = len(existing_revs) + 1
            while f"trans-rev-{next_num}" in existing_rev_ids:
                next_num += 1
            new_rev_id = f"trans-rev-{next_num}"
            revision_number = next_num
        else:
            existing_rev = next((r for r in existing_revs if r["id"] == new_rev_id), None)
            revision_number = (
                existing_rev["revision_number"]
                if existing_rev
                else payload.get("revision_number", len(existing_revs) + 1)
            )

        if inv.get("active_transcript_revision_id") == new_rev_id:
            logger.info(
                "Batch revision %s is already published and active for interview %s. Skipping re-processing.",
                new_rev_id,
                interview_id,
            )
            return

        self.repo.create_transcript_revision(
            revision_id=new_rev_id,
            interview_id=interview_id,
            revision_number=revision_number,
            is_batch_final=True,
        )

        old_segs = self.repo.get_transcript_segments(interview_id, revision_id=old_rev_id)
        if not old_segs:
            old_segs = self.repo.get_transcript_segments(interview_id)

        def resolve_speaker_role(t_id: str, seg_start: int, seg_end: int) -> str:
            if t_id in ("candidate", "interviewer"):
                return t_id
            roles_overlapping = set()
            best_overlap = 0
            inherited_role = "unknown"
            for old_s in old_segs:
                old_role = old_s.get("speaker_role")
                if old_role in ("candidate", "interviewer"):
                    o_start = max(seg_start, old_s.get("start_time_ms", 0))
                    o_end = min(seg_end, old_s.get("end_time_ms", 0))
                    overlap = max(0, o_end - o_start)
                    if overlap > 0:
                        roles_overlapping.add(old_role)
                        if overlap > best_overlap:
                            best_overlap = overlap
                            inherited_role = old_role
            if len(roles_overlapping) == 1:
                return inherited_role
            return "unknown"

        # Checkpoint and staged segments recovery
        checkpoint = payload.get("checkpoint") or {}
        empty_turns: set[str] = set(checkpoint.get("empty_turns", []))
        existing_staged = self.repo.get_transcript_segments(interview_id, revision_id=new_rev_id)
        staged_segs_map: dict[str, dict[str, Any]] = {s["id"]: s for s in existing_staged}

        # Ingest new segments
        new_segments = payload.get("segments")
        provenance = payload.get("provenance") or (
            "manual_import" if (new_segments is not None and len(new_segments) > 0) else "batch_stt"
        )
        if not new_segments:
            chunks = self.repo.get_audio_chunks(interview_id)
            chunks_by_group: dict[tuple[str, int], list[dict[str, Any]]] = {}
            for c in chunks:
                key = (c["track_id"], c.get("capture_epoch", 1))
                chunks_by_group.setdefault(key, []).append(c)

            new_segments = []

            for (track_id, epoch), group_chunks in chunks_by_group.items():
                group_chunks.sort(key=lambda c: c["sequence"])
                assembler = TurnAssembler()
                chunk_window_size = 50
                total_chunks = len(group_chunks)
                chunk_idx = 0
                cursor = TurnAssemblyCursor(group_chunks[0]["sequence"], 0)
                turns_to_transcribe: list[AssembledTurn] = []

                while chunk_idx < total_chunks:
                    window_slice = group_chunks[chunk_idx : chunk_idx + chunk_window_size]
                    is_last_window = (chunk_idx + chunk_window_size >= total_chunks)

                    chunk_refs: list[AudioChunkRef] = []
                    for c in window_slice:
                        fp_str = c.get("file_path")
                        if not fp_str or not Path(fp_str).exists():
                            raise ValueError(
                                f"Audio chunk file {fp_str} missing or corrupted for chunk sequence={c.get('sequence')}"
                            )
                        raw_bytes = Path(fp_str).read_bytes()
                        pcm = (
                            raw_bytes[44:]
                            if raw_bytes.startswith(b"RIFF") and len(raw_bytes) >= 44
                            else raw_bytes
                        )
                        chunk_refs.append(
                            AudioChunkRef(
                                sequence=c["sequence"],
                                start_time_ms=c["start_time_ms"],
                                end_time_ms=c["end_time_ms"],
                                sample_rate=c.get("sample_rate", 16000),
                                channels=c.get("channels", 1),
                                format=c.get("format", "pcm_s16le"),
                                pcm_bytes=pcm,
                            )
                        )

                    if not chunk_refs:
                        chunk_idx += chunk_window_size
                        continue

                    assembly_out = assembler.assemble(
                        chunks=chunk_refs,
                        cursor=cursor,
                        is_flush=is_last_window,
                        track_id=track_id,
                        capture_epoch=epoch,
                    )
                    turns_to_transcribe.extend(assembly_out.turns)
                    cursor = TurnAssemblyCursor(assembly_out.next_sequence, assembly_out.next_sample_offset)

                    del chunk_refs

                    next_idx = next(
                        (i for i, c in enumerate(group_chunks) if c["sequence"] >= cursor.sequence),
                        total_chunks,
                    )
                    if next_idx <= chunk_idx and not is_last_window:
                        chunk_idx += max(1, chunk_window_size // 2)
                    else:
                        chunk_idx = next_idx

                for turn in turns_to_transcribe:
                    turn_seg_id = (
                        f"seg-b-{track_id}-{epoch}-{turn.first_sequence}-"
                        f"{turn.first_sample_offset}-{turn.last_sequence}-{turn.last_sample_offset}"
                    )

                    # Resume from staged segments if already transcribed on earlier attempt
                    if turn_seg_id in staged_segs_map:
                        new_segments.append(staged_segs_map[turn_seg_id])
                        continue

                    if turn_seg_id in empty_turns:
                        continue

                    # Yield event loop to allow other tasks to proceed
                    await asyncio.sleep(0)

                    # Renew lease during long processing
                    if job_id and owner_token:
                        lease_ok = self.repo.update_job_checkpoint(
                            job_id, owner_token, {}, extension_sec=120
                        )
                        if not lease_ok:
                            raise RepositoryConflictError(
                                f"Job {job_id} lease lost during batch retranscribe"
                            )

                    pcm_bytes = self.repo.get_turn_audio_pcm(
                        interview_id=interview_id,
                        track_id=track_id,
                        capture_epoch=epoch,
                        first_sequence=turn.first_sequence,
                        first_sample_offset=turn.first_sample_offset,
                        last_sequence=turn.last_sequence,
                        last_sample_offset=turn.last_sample_offset,
                    )
                    if not pcm_bytes:
                        empty_turns.add(turn_seg_id)
                        continue

                    wav_bytes = pcm_s16le_to_wav_bytes(pcm_bytes, turn.sample_rate, turn.channels)
                    async with self._provider_semaphore:
                        res = await asyncio.wait_for(
                            self.stt_adapter.transcribe_audio(
                                wav_bytes,
                                filename=f"batch_{track_id}_{turn.first_sequence}_{turn.last_sequence}.wav",
                                content_type="audio/wav",
                            ),
                            timeout=60.0,
                        )
                    text_val = res.text.strip()
                    whisper_hallucinations = {
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
                        text_val
                        and text_val not in (".", "...", ",", "!", "?", "—", "-")
                        and text_val.lower() not in whisper_hallucinations
                    ):
                        assigned_role = resolve_speaker_role(track_id, turn.start_ms, turn.end_ms)
                        # Save segment immediately into staged revision
                        self.repo.add_transcript_segment(
                            segment_id=turn_seg_id,
                            interview_id=interview_id,
                            track_id=track_id,
                            start_time_ms=turn.start_ms,
                            end_time_ms=turn.end_ms,
                            text=text_val,
                            is_final=True,
                            revision_id=new_rev_id,
                            speaker_role=assigned_role,
                        )
                        seg_dict = {
                            "id": turn_seg_id,
                            "track_id": track_id,
                            "start_time_ms": turn.start_ms,
                            "end_time_ms": turn.end_ms,
                            "text": text_val,
                            "speaker_role": assigned_role,
                        }
                        staged_segs_map[turn_seg_id] = seg_dict
                        new_segments.append(seg_dict)
                        if job_id and owner_token:
                            self.repo.update_job_checkpoint(
                                job_id,
                                owner_token,
                                {"last_turn_id": turn_seg_id},
                                extension_sec=120,
                            )
                    else:
                        empty_turns.add(turn_seg_id)
                        if job_id and owner_token:
                            self.repo.update_job_checkpoint(
                                job_id,
                                owner_token,
                                {"empty_turns": [turn_seg_id]},
                                extension_sec=120,
                            )
        else:
            # Manual import segments provided directly
            for seg in new_segments:
                assigned_role = seg.get("speaker_role")
                if not assigned_role or assigned_role == "unknown":
                    assigned_role = resolve_speaker_role(
                        seg.get("track_id", "candidate"),
                        seg.get("start_time_ms", 0),
                        seg.get("end_time_ms", 0),
                    )
                    seg["speaker_role"] = assigned_role

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

        committed_new_segs = self.repo.get_transcript_segments(interview_id, revision_id=new_rev_id)
        if not committed_new_segs:
            logger.warning(
                "Retranscription for interview %s yielded 0 segments. Aborting activation to prevent empty transcript.",
                interview_id,
            )
            return

        try:
            self.repo.publish_batch_transcript_revision(
                interview_id=interview_id,
                expected_old_revision_id=old_rev_id,
                target_revision_id=new_rev_id,
                owner_token=owner_token,
                job_id=job_id,
                modified_question_ids=list(modified_questions) if modified_questions else None,
            )
        except RepositoryConflictError as exc:
            logger.warning(
                "Batch retranscription for interview %s aborted due to conflict: %s. Active revision kept unchanged.",
                interview_id,
                exc,
            )
            self.repo.record_audit_event(
                event_id=f"audit-{uuid.uuid4().hex[:8]}",
                interview_id=interview_id,
                event_type="BATCH_RETRANSCRIPTION_CONFLICT",
                payload={
                    "expected_old_revision_id": old_rev_id,
                    "target_revision_id": new_rev_id,
                    "conflict_reason": str(exc),
                },
            )
            exc.is_terminal = True
            raise

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

    async def _handle_generate_summary(
        self,
        interview_id: str,
        payload: dict[str, Any],
        owner_token: str | None = None,
        job_id: str | None = None,
    ) -> None:
        interview = self.repo.get_interview(interview_id)
        candidate_name = interview.get("candidate_name", "Кандидат") if interview else "Кандидат"
        role = interview.get("role", "Инженер") if interview else "Инженер"
        active_trans_rev = (interview.get("active_transcript_revision_id") or "trans-rev-1") if interview else "trans-rev-1"
        active_rub_rev = (interview.get("active_rubric_revision_id") or "rub-rev-1") if interview else "rub-rev-1"

        # Capture decisions snapshot before generation
        snapshot_hash = self.repo.compute_decisions_snapshot_hash(interview_id)

        human_assessments = self.repo.get_human_assessments(interview_id)
        proposals = self.repo.get_assessment_proposals(interview_id)

        limitations: list[str] = []
        decisions_map: dict[str, dict[str, Any]] = {}

        # 1. Filter proposals: strictly current active revisions, non-stale, non-rejected
        for p in proposals:
            if p.get("is_rejected") or p.get("is_stale"):
                continue
            p_trans = p.get("transcript_revision_id") or "trans-rev-1"
            p_rub = p.get("rubric_revision_id") or "rub-rev-1"
            if p_trans != active_trans_rev or p_rub != active_rub_rev:
                continue
            decisions_map[p["question_id"]] = p

        # 2. Human decisions take precedence and can mark exclusions
        for h in human_assessments:
            h_trans = h.get("transcript_revision_id") or "trans-rev-1"
            h_rub = h.get("rubric_revision_id") or "rub-rev-1"
            if h.get("is_stale") or h_trans != active_trans_rev or h_rub != active_rub_rev:
                decisions_map.pop(h["question_id"], None)
                continue

            if h.get("is_excluded"):
                decisions_map.pop(h["question_id"], None)
                ex_reason = h.get("exclusion_reason") or "Исключен экспертом"
                limitations.append(f"Вопрос {h['question_id']} исключен из оценки: {ex_reason}")
            else:
                decisions_map[h["question_id"]] = h

        # Check for unassessed planned questions
        plan = self.repo.get_latest_plan(interview_id)
        if plan and "payload" in plan:
            for q in plan["payload"].get("questions", []):
                q_id = q.get("id")
                if q_id and q_id not in decisions_map and not any(q_id in lim for lim in limitations):
                    limitations.append(f"Вопрос {q_id} не был оценен или пропущен.")

        summary_gen = ExecutiveSummaryGenerator(
            llm_adapter=self.llm_adapter if isinstance(self.llm_adapter, ResilientLLMAdapter) else None
        )
        async with self._provider_semaphore:
            summary_res = await summary_gen.generate_summary(
                candidate_name=candidate_name,
                role=role,
                decisions=list(decisions_map.values()),
                audio_health_summary=payload.get("audio_health", {}),
                limitations=limitations,
            )

        # Programmatically validate quotes against actual candidate segments of active revision
        all_segs = self.repo.get_transcript_segments(interview_id, revision_id=active_trans_rev)
        cand_segs = [s for s in all_segs if s.get("speaker_role") == "candidate"]
        cand_text = " ".join([s.get("text", "") for s in cand_segs]).lower()

        for group_name in ("key_strengths", "growth_areas"):
            for item in summary_res.get(group_name, []):
                quote = item.get("evidence_quote")
                if quote and str(quote).strip():
                    cleaned_quote = str(quote).strip().lower()
                    if cleaned_quote not in cand_text:
                        logger.warning("Stripped hallucinated/unverified quote from summary %s: %s", group_name, quote)
                        item["evidence_quote"] = None

        if not self.repo.get_interview(interview_id):
            logger.warning("Interview %s was deleted before saving summary proposal.", interview_id)
            return

        prop_id = f"sum-prop-{job_id}" if job_id else f"sum-{uuid.uuid4().hex[:8]}"
        self.repo.save_summary_proposal(
            proposal_id=prop_id,
            interview_id=interview_id,
            model_profile_id=summary_res.get("model_profile_id", "google/gemini-3.8-flash"),
            summary_data=summary_res,
            transcript_revision_id=active_trans_rev,
            rubric_revision_id=active_rub_rev,
            decisions_snapshot_hash=snapshot_hash,
            owner_token=owner_token,
            job_id=job_id,
        )

    async def _handle_generate_followups(
        self,
        interview_id: str,
        payload: dict[str, Any],
        owner_token: str | None = None,
        job_id: str | None = None,
    ) -> None:
        request_id = payload.get("request_id")
        if not request_id:
            logger.warning("Missing request_id in GENERATE_FOLLOWUPS payload: %s", payload)
            return

        with self.repo.db.transaction() as conn:
            req_row = conn.execute(
                "SELECT * FROM followup_requests WHERE id = ?", (request_id,)
            ).fetchone()
        if not req_row:
            logger.warning("Followup request %s not found for job %s", request_id, job_id)
            return

        # 1. Lifecycle check
        interview = self.repo.get_interview(interview_id)
        if not interview:
            logger.warning("Interview %s deleted. Aborting followups for job %s", interview_id, job_id)
            return
        inv_status = str(interview.get("status", "")).lower()
        if inv_status in ("processing", "review", "finalized", "deleted"):
            logger.info("Interview %s in status %s. Superseding followups job %s", interview_id, inv_status, job_id)
            with self.repo.db.transaction() as conn:
                conn.execute("UPDATE followup_requests SET outcome = 'superseded' WHERE id = ?", (request_id,))
            return

        # 2. Revision check
        active_rubric = interview.get("active_rubric_revision_id") or "rub-rev-1"
        active_trans = interview.get("active_transcript_revision_id") or "trans-rev-1"
        if req_row["rubric_revision_id"] != active_rubric or req_row["transcript_revision_id"] != active_trans:
            logger.info("Revisions changed for interview %s. Superseding followups request %s", interview_id, request_id)
            with self.repo.db.transaction() as conn:
                conn.execute("UPDATE followup_requests SET outcome = 'superseded' WHERE id = ?", (request_id,))
            return

        # 3. Check segment text & roles
        context_data = json.loads(req_row["context_json"])
        cand_segs = context_data.get("candidate_segments", [])
        with self.repo.db.transaction() as conn:
            for c_seg in cand_segs:
                seg_row = conn.execute(
                    "SELECT text, speaker_role, track_id FROM transcript_segments WHERE interview_id = ? AND revision_id = ? AND id = ?",
                    (interview_id, active_trans, c_seg["id"]),
                ).fetchone()
                if not seg_row or seg_row["text"].strip() != c_seg["text"].strip():
                    conn.execute("UPDATE followup_requests SET outcome = 'superseded' WHERE id = ?", (request_id,))
                    return
                role = (seg_row["speaker_role"] or "unknown").lower()
                track = str(seg_row["track_id"]).lower()
                is_cand = (role != "interviewer") if track == "candidate" else (role == "candidate")
                if not is_cand:
                    conn.execute("UPDATE followup_requests SET outcome = 'superseded' WHERE id = ?", (request_id,))
                    return

        # 4. Reconstruct FollowUpContext from context_data
        q_data = context_data["question"]
        question = PlannedQuestion(
            id=q_data["id"],
            title=q_data["title"],
            prompt=q_data["prompt"],
            criteria=[
                RubricCriterion(id=c["id"], title=c["title"], description=c["description"])
                for c in q_data["criteria"]
            ],
        )

        candidate_segments = [
            TranscriptSegment(
                id=s["id"],
                track_id=TrackType.CANDIDATE,
                start_time_ms=0,
                end_time_ms=0,
                text=s["text"],
                is_final=True,
                speaker_role="candidate",
            )
            for s in cand_segs
        ]

        interviewer_segments = [
            TranscriptSegment(
                id=s["id"],
                track_id=TrackType.INTERVIEWER,
                start_time_ms=0,
                end_time_ms=0,
                text=s["text"],
                is_final=True,
                speaker_role="interviewer",
            )
            for s in context_data.get("interviewer_segments", [])
        ]

        fu_context = FollowUpContext(
            interview_id=interview_id,
            question_id=req_row["question_id"],
            mode=FollowUpMode(req_row["mode"]),
            rubric_revision_id=req_row["rubric_revision_id"],
            transcript_revision_id=req_row["transcript_revision_id"],
            question=question,
            role_title=context_data.get("role_title", ""),
            candidate_segments=candidate_segments,
            interviewer_segments=interviewer_segments,
            decisions_history=context_data.get("decisions_history", []),
            candidate_fingerprint=req_row["candidate_fingerprint"],
            context_hash=req_row["context_hash"],
        )

        # 5. Format prompt and schema
        messages = format_followup_prompt(fu_context)
        json_schema = FollowUpLLMResponse.model_json_schema()

        # 6. Call LLM with timeout and isolated adapter
        t0 = time.perf_counter()
        try:
            async with self._provider_semaphore:
                response_envelope = await asyncio.wait_for(
                    self.followup_llm_adapter.execute_request(
                        messages=messages,
                        json_schema=json_schema,
                        schema_name="followup_suggestions",
                        max_retries=1,
                    ),
                    timeout=20.0,
                )
        except LLMAuthenticationError as auth_err:
            logger.error("Auth error in follow-up generation for %s: %s", request_id, auth_err)
            with self.repo.db.transaction() as conn:
                conn.execute(
                    "UPDATE followup_requests SET outcome = 'failed', error_code = 'auth_error' WHERE id = ?",
                    (request_id,),
                )
            auth_err.is_terminal = True
            raise
        except (LLMRateLimitError, LLMTransientError, asyncio.TimeoutError) as retryable_err:
            retry_sec = getattr(retryable_err, "retry_after", None) or 10.0
            logger.warning("Retryable error in follow-up generation for %s: %s", request_id, retryable_err)
            retryable_err.retry_delay_sec = int(retry_sec)
            raise
        except Exception as exc:
            logger.exception("Unexpected error in follow-up generation for %s: %s", request_id, exc)
            raise

        latency_ms = int((time.perf_counter() - t0) * 1000)

        # Unpack envelope and extract exact upstream model & usage
        if isinstance(response_envelope, dict):
            res_dict = response_envelope.get("data") or {}
            upstream_model = response_envelope.get("model") or getattr(
                self.followup_llm_adapter.model, "upstream_model_id", None
            )
            usage = response_envelope.get("usage") or {}
            usage_tokens = usage.get("total_tokens")
        else:
            res_dict = response_envelope
            upstream_model = getattr(self.followup_llm_adapter.model, "upstream_model_id", None)
            usage_tokens = None

        logger.info(
            "Follow-up generated for request %s (job %s) with upstream model %s in %d ms",
            request_id,
            job_id,
            upstream_model,
            latency_ms,
        )

        # 7. Validate response
        validated_resp, errors = validate_followup_response(res_dict, fu_context)
        if errors and not validated_resp.suggestions and any("schema" in e.lower() for e in errors):
            with self.repo.db.transaction() as conn:
                conn.execute(
                    "UPDATE followup_requests SET outcome = 'failed', error_code = 'validation_error' WHERE id = ?",
                    (request_id,),
                )
            val_err = ValueError(f"Followup validation failure: {errors}")
            val_err.is_terminal = True
            raise val_err

        # 8. Pre-commit check: lifecycle and revisions
        interview = self.repo.get_interview(interview_id)
        if not interview:
            return
        inv_status = str(interview.get("status", "")).lower()
        if inv_status in ("processing", "review", "finalized", "deleted"):
            with self.repo.db.transaction() as conn:
                conn.execute("UPDATE followup_requests SET outcome = 'superseded' WHERE id = ?", (request_id,))
            return
        if (
            interview.get("active_rubric_revision_id") != req_row["rubric_revision_id"]
            or interview.get("active_transcript_revision_id") != req_row["transcript_revision_id"]
        ):
            with self.repo.db.transaction() as conn:
                conn.execute("UPDATE followup_requests SET outcome = 'superseded' WHERE id = ?", (request_id,))
            return

        # 9. Save suggestions
        sug_payload = [s.model_dump() for s in validated_resp.suggestions]
        outcome = "ready" if sug_payload else "no_suggestions"
        if owner_token and job_id:
            self.repo.save_followup_suggestions(
                job_id=job_id,
                request_id=request_id,
                owner_token=owner_token,
                suggestions=sug_payload,
                outcome=outcome,
                model_profile_id=getattr(self.followup_llm_adapter.model, "id", "google/gemini-3.8-flash"),
                provider_id=getattr(self.followup_llm_adapter.provider, "id", "plusvibe"),
                usage_tokens=usage_tokens,
                latency_ms=latency_ms,
            )
        return {
            "question_id": req_row["question_id"],
            "call_duration_ms": latency_ms,
            "provider_id": getattr(self.followup_llm_adapter.provider, "id", "plusvibe"),
            "upstream_model": upstream_model,
            "usage_tokens": usage_tokens,
            "suggestions_count": len(sug_payload),
        }

    async def run_loop(self, poll_interval_sec: float = 0.25) -> None:
        """
        Runs dedicated consumers matching Stage 4 specifications:
        1. Assembly: TRANSCRIBE_AUDIO (concurrency 1)
        2. Live STT: TRANSCRIBE_TURN (concurrency 2)
        3. Assessment: EVALUATE_QUESTION (concurrency 1)
        4. Follow-ups: GENERATE_FOLLOWUPS (concurrency 1)
        5. Background: BATCH_RETRANSCRIBE, GENERATE_SUMMARY, etc. (concurrency 1)
        """
        self._running = True
        self._stop_event.clear()

        async def _consumer_loop(
            consumer_name: str,
            include_types: list[str] | None = None,
            exclude_types: list[str] | None = None,
        ) -> None:
            while self._running:
                try:
                    did_work = await self.process_one_job(
                        include_types=include_types,
                        exclude_types=exclude_types,
                    )
                    if not did_work:
                        # Queue is empty for these types: interruptible wait
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._stop_event.wait(), timeout=poll_interval_sec)
                    else:
                        # Cooperative yield without artificial 300ms sleep
                        await asyncio.sleep(0)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.exception("Error in %s consumer loop: %s", consumer_name, e)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop_event.wait(), timeout=poll_interval_sec)

        # Concurrency: 1 Assembly + 2 Live STT + 1 Assessment + 1 Followups + 1 Background = 6 workers
        consumers = [
            asyncio.create_task(_consumer_loop("assembly", include_types=["TRANSCRIBE_AUDIO"])),
            asyncio.create_task(_consumer_loop("live_stt_1", include_types=["TRANSCRIBE_TURN"])),
            asyncio.create_task(_consumer_loop("live_stt_2", include_types=["TRANSCRIBE_TURN"])),
            asyncio.create_task(_consumer_loop("assessment", include_types=["EVALUATE_QUESTION"])),
            asyncio.create_task(_consumer_loop("followups", include_types=["GENERATE_FOLLOWUPS"])),
            asyncio.create_task(
                _consumer_loop(
                    "background",
                    exclude_types=[
                        "TRANSCRIBE_AUDIO",
                        "TRANSCRIBE_TURN",
                        "EVALUATE_QUESTION",
                        "GENERATE_FOLLOWUPS",
                    ],
                )
            ),
        ]

        try:
            await asyncio.gather(*consumers)
        except asyncio.CancelledError:
            self._running = False
            self._stop_event.set()
            for c in consumers:
                c.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
        finally:
            self._running = False
            self._stop_event.set()
            if self._owns_http_client:
                await self.close()

    def stop(self) -> None:
        """Signals all consumers to stop."""
        self._running = False
        self._stop_event.set()

    async def close(self) -> None:
        """Stops worker and closes owned HTTP client resources."""
        self.stop()
        if self._owns_http_client and self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    from backend.adapters.resilient_llm import ResilientLLMAdapter
    from backend.adapters.stt import OpenAICompatibleSTTAdapter
    from backend.core.profiles import get_plusvibe_whisper_stt
    from backend.db.database import get_db
    from backend.db.repository import Repository

    db = get_db()
    db.init_schema()
    repo = Repository(db)
    worker = PipelineWorker(repository=repo)
    logger.info("Starting Nebula Pipeline Worker daemon...")
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        logger.info("Pipeline Worker stopped by user.")

