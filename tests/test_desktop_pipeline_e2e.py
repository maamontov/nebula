"""
End-to-end integration test for the Desktop Capture -> Server Ingestion -> STT Pipeline -> Question Matcher -> Scoring -> Review flow.
Тест сквозного пайплайна: захват/спул -> серверный прием чанков -> манифесты -> гейт готовности -> STT воркер -> QuestionMatcher -> оценка без утечек -> финализация.
"""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.adapters.stt import STTTranscriptionResult
from backend.api.app import app
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_e2e.db"
    db = Database(str(db_file))
    db.init_schema()
    return db


@pytest.fixture
def repo(test_db):
    return Repository(test_db)


@pytest.fixture
def client(test_db, monkeypatch):
    monkeypatch.setattr("backend.api.app.get_db", lambda: test_db)
    monkeypatch.setenv("NEBULA_DISABLE_AUTH", "1")
    return TestClient(app)


def _create_pcm_chunk(sample_count: int = 16000, val: int = 500) -> bytes:
    """Create raw 16-bit little-endian PCM samples."""
    return (val.to_bytes(2, byteorder="little", signed=True)) * sample_count


@pytest.mark.asyncio
async def test_full_desktop_pipeline_lifecycle(client: TestClient, repo: Repository, tmp_path):
    interview_id = "inv-e2e-pipeline-1"

    # 1. Create interview with planned questions
    plan_payload = {
        "questions": [
            {
                "id": "q1",
                "prompt": "Как работает механизм garbage collection в Go?",
                "text": "Как работает механизм garbage collection в Go?",
                "criteria": [
                    {
                        "id": "crit-gc-tri-color",
                        "title": "Трехцветный марк-энд-свип (tri-color mark and sweep)",
                        "weight": 1.0,
                    }
                ],
            },
            {
                "id": "q2",
                "prompt": "Что такое распределенный консенсус Raft?",
                "text": "Что такое распределенный консенсус Raft?",
                "criteria": [
                    {
                        "id": "crit-raft-leader",
                        "title": "Выборы лидера и репликация лога",
                        "weight": 1.0,
                    }
                ],
            },
        ]
    }

    resp = client.post(
        "/api/v1/interviews",
        json={
            "id": interview_id,
            "title": "Go Lead — E2E Test",
            "candidate_name": "Виктор Корбут",
            "role": "Go Lead",
            "plan": plan_payload,
        },
    )
    assert resp.status_code == 200

    # Advance to ready -> recording
    assert client.post(f"/api/v1/interviews/{interview_id}/status", json={"current_status": "draft", "target_status": "ready"}).status_code == 200
    assert client.post(
        f"/api/v1/interviews/{interview_id}/status",
        json={
            "current_status": "ready",
            "target_status": "recording",
            "consent_confirmed_at": "2026-09-10T12:00:00Z",
            "consent_version": "consent-v1.0-ru",
        },
    ).status_code == 200

    # 2. Simulate Desktop Spool Chunk delivery (Stage B)
    # 2. Simulate Desktop Spool Chunk delivery (Stage B)
    # Interviewer chunk 0
    interviewer_pcm = _create_pcm_chunk(16000, 300)
    sha_int = hashlib.sha256(interviewer_pcm).hexdigest()
    resp_chunk_int = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={
            "metadata": {
                "interview_id": interview_id,
                "track_id": "interviewer",
                "capture_epoch": 1,
                "sequence": 0,
                "start_time_ms": 0,
                "end_time_ms": 1000,
                "sample_rate": 16000,
                "channels": 1,
                "sample_count": 16000,
                "format": "pcm_s16le",
                "checksum_sha256": sha_int,
                "size_bytes": len(interviewer_pcm),
            },
            "payload_hex": interviewer_pcm.hex(),
        },
    )
    assert resp_chunk_int.status_code == 200

    # Candidate chunk 0 (speech about Go GC)
    candidate_pcm = _create_pcm_chunk(16000, 800)
    sha_cand = hashlib.sha256(candidate_pcm).hexdigest()
    resp_chunk_cand = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={
            "metadata": {
                "interview_id": interview_id,
                "track_id": "candidate",
                "capture_epoch": 1,
                "sequence": 0,
                "start_time_ms": 1000,
                "end_time_ms": 2000,
                "sample_rate": 16000,
                "channels": 1,
                "sample_count": 16000,
                "format": "pcm_s16le",
                "checksum_sha256": sha_cand,
                "size_bytes": len(candidate_pcm),
            },
            "payload_hex": candidate_pcm.hex(),
        },
    )
    assert resp_chunk_cand.status_code == 200

    # 3. Verify Server Readiness Gate (Stage C) BEFORE manifests are uploaded
    readiness_pre = client.get(f"/api/v1/interviews/{interview_id}/readiness").json()
    assert readiness_pre["is_ready"] is False
    assert readiness_pre["state"] in ("STT_IN_PROGRESS", "AWAITING_MANIFEST")

    # Attempting to move to REVIEW before ready MUST return 409 Conflict
    conflict_resp = client.post(
        f"/api/v1/interviews/{interview_id}/status",
        json={"current_status": "processing", "target_status": "review"},
    )
    assert conflict_resp.status_code == 409

    # 4. Stop interview and deliver manifests from Tauri desktop
    manifest_interviewer = {
        "interview_id": interview_id,
        "session_id": interview_id,
        "track_id": "interviewer",
        "capture_epoch": 1,
        "sample_rate": 16000,
        "channels": 1,
        "total_chunks": 1,
        "total_samples": 16000,
        "total_duration_ms": 1000,
        "is_sealed": True,
        "chunks": [
            {
                "sequence": 0,
                "sequence_number": 0,
                "epoch": 1,
                "start_time_ms": 0,
                "end_time_ms": 1000,
                "sample_count": 16000,
                "checksum_sha256": sha_int,
                "format": "pcm_s16le",
            }
        ],
    }
    manifest_candidate = {
        "interview_id": interview_id,
        "session_id": interview_id,
        "track_id": "candidate",
        "capture_epoch": 1,
        "sample_rate": 16000,
        "channels": 1,
        "total_chunks": 1,
        "total_samples": 16000,
        "total_duration_ms": 1000,
        "is_sealed": True,
        "chunks": [
            {
                "sequence": 0,
                "sequence_number": 0,
                "epoch": 1,
                "start_time_ms": 1000,
                "end_time_ms": 2000,
                "sample_count": 16000,
                "checksum_sha256": sha_cand,
                "format": "pcm_s16le",
            }
        ],
    }

    stop_resp = client.post(
        f"/api/v1/interviews/{interview_id}/stop",
        json={"manifests": [manifest_interviewer, manifest_candidate]},
    )
    assert stop_resp.status_code == 200

    # 5. Check readiness again: manifests are present, but STT jobs are still pending!
    readiness_mid = client.get(f"/api/v1/interviews/{interview_id}/readiness").json()
    assert readiness_mid["is_ready"] is False
    assert readiness_mid["stt_jobs"]["pending"] >= 1

    # 6. Process STT jobs with PipelineWorker
    mock_stt = MagicMock()
    # When transcribing candidate audio, return text mentioning Go GC
    mock_stt.transcribe_audio = AsyncMock(
        side_effect=[
            # Interviewer speech
            STTTranscriptionResult(
                text="Расскажите, как работает garbage collection в Go?",
                model_id="test-whisper",
                latency_seconds=0.5,
                raw_response={},
            ),
            # Candidate speech
            STTTranscriptionResult(
                text="В Go используется трехцветный mark-and-sweep сборщик мусора с минимальным STW.",
                model_id="test-whisper",
                latency_seconds=0.8,
                raw_response={},
            ),
        ]
    )

    mock_llm = MagicMock()
    mock_llm.execute_request = AsyncMock(
        return_value=(
            {
                "scores": [
                    {
                        "criterion_id": "crit-gc-tri-color",
                        "score": 5.0,
                        "explanation": "Четко упомянут трехцветный mark-and-sweep сборщик мусора.",
                        "evidence": [
                            {
                                "segment_id": "will_be_matched",  # worker will validate against actual segment
                                "exact_quote": "В Go используется трехцветный mark-and-sweep сборщик мусора с минимальным STW.",
                            }
                        ],
                    }
                ],
                "critical_errors": [],
            },
            "test-llm-model",
        )
    )

    worker = PipelineWorker(repo, stt_adapter=mock_stt, llm_adapter=mock_llm)

    # Drain all queued TRANSCRIBE_AUDIO jobs
    processed_count = 0
    while await worker.process_one_job():
        processed_count += 1
    assert processed_count >= 2

    # 7. Verify segments created
    segments = repo.get_transcript_segments(interview_id)
    assert len(segments) == 2
    cand_segs = [s for s in segments if s["track_id"] == "candidate"]
    assert len(cand_segs) == 1
    cand_seg_id = cand_segs[0]["id"]
    assert "трехцветный" in cand_segs[0]["text"]

    # 8. Check Readiness Gate: all chunks, manifests, and STT jobs are complete!
    readiness_post = client.get(f"/api/v1/interviews/{interview_id}/readiness").json()
    assert readiness_post["is_ready"] is True
    assert readiness_post["state"] == "READY_FOR_REVIEW"

    # 9. Gate allows moving to REVIEW!
    to_review = client.post(
        f"/api/v1/interviews/{interview_id}/status",
        json={"current_status": "processing", "target_status": "review"},
    )
    assert to_review.status_code == 200

    # 10. Evaluate Question q1 (Candidate spoke on Go GC)
    # Fix the segment_id in mock LLM response to match the real segment id
    mock_llm.execute_request = AsyncMock(
        return_value=(
            {
                "scores": [
                    {
                        "criterion_id": "crit-gc-tri-color",
                        "score": 5.0,
                        "explanation": "Четко упомянут трехцветный mark-and-sweep сборщик мусора.",
                        "evidence": [
                            {
                                "segment_id": cand_seg_id,
                                "exact_quote": "В Go используется трехцветный mark-and-sweep сборщик мусора с минимальным STW.",
                            }
                        ],
                    }
                ],
                "critical_errors": [],
            },
            "test-llm-model",
        )
    )

    repo.enqueue_job(
        job_id="job-eval-q1",
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={"question_id": "q1"},
    )
    assert await worker.process_one_job() is True

    # 11. Evaluate Question q2 (Raft) — NO candidate speech!
    # Stage E ensures this does NOT fall back to q1's candidate segments!
    repo.enqueue_job(
        job_id="job-eval-q2",
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={"question_id": "q2"},
    )
    assert await worker.process_one_job() is True

    # Verify proposals
    proposals = repo.get_assessment_proposals(interview_id)
    prop_by_q = {p["question_id"]: p for p in proposals}
    assert "q1" in prop_by_q
    assert "q2" in prop_by_q

    # q1 proposal: valid, score 5.0, verified verbatim evidence
    p1 = prop_by_q["q1"]
    assert p1["is_rejected"] == 0
    assert p1["scores"][0]["score"] == 5.0
    assert p1["scores"][0]["evidence"][0]["segment_id"] == cand_seg_id

    # q2 proposal: explicit unanswered proposal, score is None, NO cross-question leak!
    p2 = prop_by_q["q2"]
    assert p2["is_rejected"] == 0
    assert p2["scores"][0]["score"] is None
    assert "отсутствует" in p2["scores"][0]["explanation"]

    # 12. Human Review & Scoring approval
    review_resp = client.post(
        f"/api/v1/interviews/{interview_id}/assessments/q1/review",
        json={
            "expected_transcript_revision": "trans-rev-1",
            "scores": [{"criterion_id": "crit-gc-tri-color", "score": 5.0, "explanation": "Отличный ответ"}],
            "reviewer_notes": "Глубокие знания рантайма",
            "reviewer_id": "tech-lead-user",
        },
    )
    assert review_resp.status_code == 200

    # Approve q2 with excluded or zero score
    review_resp_q2 = client.post(
        f"/api/v1/interviews/{interview_id}/assessments/q2/review",
        json={
            "expected_transcript_revision": "trans-rev-1",
            "scores": [{"criterion_id": "crit-raft-leader", "score": 1.0, "explanation": "Кандидат не ответил"}],
            "reviewer_notes": "Вопрос пропущен",
            "reviewer_id": "tech-lead-user",
        },
    )
    assert review_resp_q2.status_code == 200

    # 13. Finalize interview report
    finalize_resp = client.post(
        f"/api/v1/interviews/{interview_id}/report/finalize",
        json={
            "summary_markdown": "Кандидат отлично знает GC в Go, но пропустил вопрос по Raft.",
            "hiring_recommendation": "STRONG_HIRE",
            "confirmed_by": "tech-lead-user",
        },
    )
    assert finalize_resp.status_code == 200

    # 14. Verify Report Revision generated
    report = repo.get_latest_report_revision(interview_id)
    assert report is not None
    assert report["final_score_100"] is not None
    assert report["coverage_percentage"] == 100.0
