import hashlib
import math
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.adapters.stt import STTTranscriptionResult
from backend.core.audio_utils import pcm_s16le_to_wav_bytes
from backend.core.turn_assembler import (
    AudioChunkRef,
    TurnAssembler,
    TurnAssemblyCursor,
)
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "stage3.db"))
    database.init_schema()
    return database


@pytest.fixture
def repo(db):
    return Repository(db)


def _create_sine_pcm(duration_sec: float = 2.0, freq: float = 440.0, rate: int = 16000) -> bytes:
    sample_count = int(rate * duration_sec)
    return b"".join(
        struct.pack("<h", int(15000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(sample_count)
    )


def _create_silence_pcm(duration_sec: float = 1.0, rate: int = 16000) -> bytes:
    sample_count = int(rate * duration_sec)
    return b"\x00" * (sample_count * 2)


def _save_chunk(db, tmp_path, interview_id, seq, start_ms, end_ms, pcm_bytes):
    chunk_file = tmp_path / f"chunk_{interview_id}_{seq}.wav"
    chunk_file.write_bytes(pcm_s16le_to_wav_bytes(pcm_bytes, 16000, 1))
    file_bytes = chunk_file.read_bytes()
    sha = hashlib.sha256(file_bytes).hexdigest()

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO audio_chunks (
                interview_id, track_id, capture_epoch, sequence,
                start_time_ms, end_time_ms, sample_rate, channels,
                sample_count, format, checksum_sha256, size_bytes,
                file_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interview_id, "shared", 1, seq,
                start_ms, end_ms, 16000, 1,
                len(pcm_bytes) // 2, "pcm_s16le", sha, len(file_bytes),
                str(chunk_file), "2026-09-12T12:00:00Z",
            ),
        )


@pytest.mark.asyncio
async def test_batch_retranscribe_resumes_without_retranscribing_completed_turns(tmp_path, db, repo):
    """R5: Retry resumes from staged segments and checkpoints without re-calling STT for already completed turns."""
    interview_id = "inv-r5-resume"
    repo.create_interview(interview_id, "Resume Test", "Candidate", "Engineer")
    repo.add_transcript_segment(
        segment_id="init-1",
        interview_id=interview_id,
        track_id="shared",
        start_time_ms=0,
        end_time_ms=1000,
        text="Initial text",
        revision_id="trans-rev-1",
    )

    # Chunk 0: 2s speech + 1s silence (turn 1)
    pcm_0 = _create_sine_pcm(2.0) + _create_silence_pcm(1.0)
    _save_chunk(db, tmp_path, interview_id, 0, 0, 3000, pcm_0)

    # Chunk 1: 2s speech + 1s silence (turn 2)
    pcm_1 = _create_sine_pcm(2.0, freq=880.0) + _create_silence_pcm(1.0)
    _save_chunk(db, tmp_path, interview_id, 1, 3000, 6000, pcm_1)

    stt_call_count = 0
    stt_mock = MagicMock()

    async def counting_stt(*args, **kwargs):
        nonlocal stt_call_count
        stt_call_count += 1
        if stt_call_count == 1:
            return STTTranscriptionResult(text="First turn transcribed", model_id="test", latency_seconds=0.1, raw_response={})
        elif stt_call_count == 2:
            # Crash on second turn during first attempt
            raise RuntimeError("Temporary STT connection reset")
        else:
            return STTTranscriptionResult(text="Second turn transcribed", model_id="test", latency_seconds=0.1, raw_response={})

    stt_mock.transcribe_audio = AsyncMock(side_effect=counting_stt)
    worker = PipelineWorker(repository=repo, stt_adapter=stt_mock)

    payload = {
        "old_revision_id": "trans-rev-1",
        "new_revision_id": "trans-rev-2",
    }

    # First attempt: crashes on turn 2
    with pytest.raises(RuntimeError) as exc_info:
        await worker._handle_batch_retranscribe(interview_id, payload)
    assert "Temporary STT connection reset" in str(exc_info.value)
    assert stt_call_count == 2

    # Check that turn 1 was saved into trans-rev-2 before crash
    staged_segs = repo.get_transcript_segments(interview_id, revision_id="trans-rev-2")
    assert len(staged_segs) == 1
    assert staged_segs[0]["text"] == "First turn transcribed"
    # Active revision is still trans-rev-1 (not activated prematurely!)
    assert repo.get_interview(interview_id)["active_transcript_revision_id"] == "trans-rev-1"

    # Second attempt (retry): should NOT re-transcribe turn 1!
    await worker._handle_batch_retranscribe(interview_id, payload)

    # Only 1 additional STT call was made for turn 2!
    assert stt_call_count == 3, f"Expected 3 total STT calls, got {stt_call_count}"

    # Now active revision is activated to trans-rev-2 and contains both turns
    inv = repo.get_interview(interview_id)
    assert inv["active_transcript_revision_id"] == "trans-rev-2"
    active_segs = repo.get_transcript_segments(interview_id, revision_id="trans-rev-2")
    assert len(active_segs) == 2
    texts = [s["text"] for s in active_segs]
    assert "First turn transcribed" in texts
    assert "Second turn transcribed" in texts


@pytest.mark.asyncio
async def test_batch_retranscribe_fails_on_missing_chunk_file(tmp_path, db, repo):
    """R5 acceptance: Missing/corrupted audio chunk must raise an explicit error instead of silently skipping audio."""
    interview_id = "inv-missing-chunk"
    repo.create_interview(interview_id, "Missing Test", "Candidate", "Engineer")

    # Save audio_chunk record pointing to a non-existent file
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO audio_chunks (
                interview_id, track_id, capture_epoch, sequence,
                start_time_ms, end_time_ms, sample_rate, channels,
                sample_count, format, checksum_sha256, size_bytes,
                file_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interview_id, "shared", 1, 0,
                0, 2000, 16000, 1,
                32000, "pcm_s16le", "fake-sha", 64000,
                str(tmp_path / "non_existent_chunk.wav"), "2026-09-12T12:00:00Z",
            ),
        )

    worker = PipelineWorker(repository=repo, stt_adapter=MagicMock())
    payload = {"old_revision_id": "trans-rev-1", "new_revision_id": "trans-rev-2"}

    with pytest.raises(ValueError) as exc_info:
        await worker._handle_batch_retranscribe(interview_id, payload)
    assert "missing or corrupted" in str(exc_info.value)


def test_turn_assembler_memory_large_stream_without_sample_map():
    """R9: TurnAssembler processes multi-chunk stream accurately using spans without sample_map."""
    assembler = TurnAssembler()
    pcm_sine_1s = _create_sine_pcm(1.0)
    pcm_silence_1s = _create_silence_pcm(1.0)

    # 10 chunks alternating speech and silence
    chunks = []
    for seq in range(10):
        pcm = pcm_sine_1s if (seq % 2 == 0) else pcm_silence_1s
        chunks.append(
            AudioChunkRef(
                sequence=seq,
                start_time_ms=seq * 1000,
                end_time_ms=(seq + 1) * 1000,
                sample_rate=16000,
                channels=1,
                format="pcm_s16le",
                pcm_bytes=pcm,
            )
        )

    out = assembler.assemble(
        chunks=chunks,
        cursor=TurnAssemblyCursor(0, 0),
        is_flush=True,
        track_id="candidate",
        capture_epoch=1,
    )

    # Should detect distinct speech turns
    assert len(out.turns) >= 4
    for turn in out.turns:
        assert turn.track_id == "candidate"
        assert turn.first_sequence <= turn.last_sequence
        assert turn.start_ms < turn.end_ms
        assert turn.deterministic_job_id.startswith("stt-turn-candidate-1-")
