"""
Reproduction tests for audit issues R1, R2, R5, R7.
Demonstrates defects before fixes are applied.
"""
import pytest

from backend.core.evidence_validator import validate_proposal
from backend.db.database import Database
from backend.db.repository import Repository
from contracts.domain import (
    AssessmentProposal,
    CriterionScoreProposal,
    EvidenceRef,
    TranscriptRevision,
    TranscriptSegment,
)


def test_reproduce_r7_bias_validator_false_positive_on_technical_terms():
    """R7: 'age' inside technical terms like 'message' or 'storage' incorrectly flags forbidden attribute."""
    proposal = AssessmentProposal(
        id="prop-test",
        interview_id="int-test",
        question_id="q-1",
        rubric_revision_id="rub-rev-1",
        transcript_revision_id="trans-rev-1",
        model_profile_id="gpt-test",
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-arch",
                score=4.5,
                explanation="Candidate explained message queues and storage partitioning clearly.",
                evidence=[
                    EvidenceRef(
                        segment_id="seg-1",
                        exact_quote="We used message queues for async storage tasks.",
                        track_id="candidate",
                    )
                ],
            )
        ],
    )
    transcript = TranscriptRevision(
        revision_id="trans-rev-1",
        interview_id="int-test",
        segments=[
            TranscriptSegment(
                id="seg-1",
                interview_id="int-test",
                track_id="candidate",
                speaker_role="candidate",
                start_time_ms=0,
                end_time_ms=5000,
                text="We used message queues for async storage tasks.",
            )
        ],
    )
    result = validate_proposal(proposal, transcript, allowed_criteria_ids={"crit-arch"})
    # On buggy code, result.is_valid is False because 'age' matches inside 'message' and 'storage'
    assert result.is_valid is True, f"Expected valid proposal, got errors: {result.errors}"


def test_reproduce_r2_global_pk_conflict_between_interviews(tmp_path):
    """R2: Global PK on transcript_revisions causes second interview to lose revision metadata."""
    db_path = str(tmp_path / "test_r2.db")
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)

    # Create interview 1
    repo.create_interview("int-1", "Title 1", "Alice", "Developer")
    repo.add_transcript_segment("s1", "int-1", "shared", 0, 1000, "Hello from Alice", revision_id="trans-rev-1")
    # Role assignment creates trans-rev-2 for int-1
    repo.update_segment_speaker_role("int-1", "s1", "interviewer")
    revs_1 = repo.get_transcript_revisions("int-1")
    assert len(revs_1) >= 2, f"Expected at least 2 revisions for int-1, got {revs_1}"

    # Create interview 2
    repo.create_interview("int-2", "Title 2", "Bob", "Developer")
    repo.add_transcript_segment("s2", "int-2", "shared", 0, 1000, "Hello from Bob", revision_id="trans-rev-1")
    # Role assignment tries to create trans-rev-2 for int-2
    repo.update_segment_speaker_role("int-2", "s2", "interviewer")
    revs_2 = repo.get_transcript_revisions("int-2")
    # On buggy code, revs_2 is empty [] because trans-rev-1 and trans-rev-2 already exist for int-1!
    assert len(revs_2) >= 2, f"Expected at least 2 revisions for int-2, got {revs_2}"


@pytest.mark.asyncio
async def test_reproduce_r1_batch_activates_over_manual_edit(tmp_path):
    """R1: Concurrent manual edit (role assignment) during batch STT is overwritten by batch revision activation."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from backend.adapters.stt import STTTranscriptionResult
    from backend.db.repository import RepositoryConflictError
    from backend.workers.pipeline import PipelineWorker

    db_path = str(tmp_path / "test_r1.db")
    db = Database(db_path)
    db.init_schema()
    repo = Repository(db)

    interview_id = "int-r1"
    repo.create_interview(interview_id, "Title", "Cand", "Role")
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id=interview_id,
        track_id="shared",
        start_time_ms=0,
        end_time_ms=5000,
        text="Hello world",
        speaker_role="unknown",
        revision_id="trans-rev-1",
    )
    assert repo.get_interview(interview_id)["active_transcript_revision_id"] == "trans-rev-1"

    chunk_file = tmp_path / "chunk_0.wav"
    import math
    import struct

    from backend.core.audio_utils import pcm_s16le_to_wav_bytes
    sample_count = 16000 * 3
    fake_pcm = b"".join(
        struct.pack("<h", int(15000 * math.sin(2 * math.pi * 440 * i / 16000)))
        for i in range(sample_count)
    )
    chunk_file.write_bytes(pcm_s16le_to_wav_bytes(fake_pcm, 16000, 1))

    raw_file_bytes = chunk_file.read_bytes()
    import hashlib
    file_sha256 = hashlib.sha256(raw_file_bytes).hexdigest()

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
                0, 3000, 16000, 1,
                sample_count, "pcm_s16le", file_sha256, len(raw_file_bytes),
                str(chunk_file), "2026-09-12T12:00:00Z",
            ),
        )

    # Set up mock STT that triggers a manual edit during transcription
    stt_mock = MagicMock()
    manual_edit_done = asyncio.Event()

    async def fake_transcribe(*args, **kwargs):
        # While STT is running, reviewer manually marks seg-1 as interviewer
        repo.update_segment_speaker_role(interview_id, "seg-1", "interviewer")
        manual_edit_done.set()
        return STTTranscriptionResult(text="Hello world transcribed", model_id="test-stt", latency_seconds=0.1, raw_response={})

    stt_mock.transcribe_audio = AsyncMock(side_effect=fake_transcribe)

    worker = PipelineWorker(repository=repo, stt_adapter=stt_mock)

    # Enqueue batch retranscription targeting trans-rev-2 from trans-rev-1 without pre-supplied segments
    payload = {
        "old_revision_id": "trans-rev-1",
        "new_revision_id": "trans-rev-2",
    }

    # Execute batch retranscribe directly; conflict must be detected and raised
    with pytest.raises(RepositoryConflictError):
        await worker._handle_batch_retranscribe(interview_id, payload)

    # Check active revision:
    # During STT, update_segment_speaker_role created a revision and set it active.
    # On buggy code, batch unconditionally activates its new_rev_id (trans-rev-2)
    # with speaker_role='unknown', overwriting the human's manual decision!
    active_rev = repo.get_interview(interview_id)["active_transcript_revision_id"]
    active_segs = repo.get_transcript_segments(interview_id, revision_id=active_rev)
    seg_1 = next(s for s in active_segs if s["id"] == "seg-1")
    # Human assigned "interviewer", so active transcript MUST retain "interviewer"
    assert seg_1["speaker_role"] == "interviewer", (
        f"Human edit was overwritten! Expected speaker_role='interviewer', got '{seg_1['speaker_role']}' in revision {active_rev}"
    )

