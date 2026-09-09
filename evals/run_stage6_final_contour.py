"""
Stage 6: Final Processing Contour (Batch-STT, Revisions, Human Override Protection & Sealed Export) Acceptance Runner.

Verifies:
1. Creation of multi-question live session with initial transcript (trans-rev-1).
2. Initial AI proposals generated and evaluated.
3. Human reviewer confirms and manually overrides a question evaluation with reviewer notes.
4. Batch-STT re-transcription execution generating trans-rev-2 with improved accuracy.
5. Invariant: Batch processing NEVER overwrites human decisions (human override preserved 100%).
6. Stale detection: Modified questions flagged as is_stale=True with explicit stale_reason and diff reports.
7. Conflict prevention: Outdated revision submissions rejected with HTTP 409 Conflict.
8. Executive summary generated via Resilient AI and confirmed by reviewer.
9. Final report immutable snapshot created with deterministic 100-pt score and SHA-256 cryptographic seal.
"""

import sys
import os
import time
import json
import uuid
import tempfile
import asyncio
import hashlib
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.database import Database
from backend.db.repository import Repository
from backend.core.revisions import TranscriptDiffEngine
from backend.core.summary_generator import ExecutiveSummaryGenerator
from backend.adapters.resilient_llm import ResilientLLMAdapter
from backend.workers.pipeline import PipelineWorker
from backend.api import app as api_module
from starlette.testclient import TestClient


def run_stage6_acceptance():
    print("=" * 80)
    print("STARTING STAGE 6: FINAL PROCESSING CONTOUR & REVISION ACCEPTANCE")
    print("=" * 80)

    start_time = time.perf_counter()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        db_path = tmp_db.name

    try:
        # 1. Setup Isolated Storage & Repository
        db = Database(db_path)
        db.init_schema()
        repo = Repository(db)
        api_module.app.dependency_overrides[api_module.get_repository] = lambda: repo
        client = TestClient(api_module.app)
        print(f"[Step 1/8] Initialized SQLite WAL Database at {db_path}")

        # 2. Multi-Question Session Setup
        interview_id = f"inv-stage6-{uuid.uuid4().hex[:6]}"
        questions = [
            {
                "question_id": "q1-linux-gil",
                "text": "В чем разница между процессами и потоками в Linux, и как устроен GIL в CPython?",
                "weight": 1.0,
                "criteria": ["Разделение памяти и адресного пространства", "Назначение GIL и счетчики ссылок"],
            },
            {
                "question_id": "q2-gil-bypass",
                "text": "Как обойти ограничения GIL при CPU-bound вычислениях в Python?",
                "weight": 1.0,
                "criteria": ["Многопроцессность (multiprocessing)", "C-расширения и release GIL"],
            },
            {
                "question_id": "q3-distributed-saga",
                "text": "Как устроен паттерн Saga для распределенных транзакций в микросервисах?",
                "weight": 1.5,
                "criteria": ["Оркестрация vs Хореография", "Компенсирующие транзакции при сбоях"],
            },
        ]

        setup_payload = {
            "id": interview_id,
            "title": "Principal Systems Engineer — Stage 6 Final Contour Verification",
            "candidate_name": "Алексей Смирнов",
            "role": "Principal Systems Architect",
            "plan": {"questions": questions},
        }

        resp = client.post("/api/v1/interviews", json=setup_payload)
        assert resp.status_code == 200

        # State transition with participant consent
        client.post(f"/api/v1/interviews/{interview_id}/status", json={"current_status": "draft", "target_status": "ready"})
        client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={
                "current_status": "ready",
                "target_status": "recording",
                "consent_confirmed_at": "2026-09-10T01:10:00Z",
                "consent_version": "v1.0",
            },
        )
        print(f"[Step 2/8] Created interview {interview_id} with 3 weighted questions and consent confirmed")

        # Ingest Initial Live Transcript (trans-rev-1)
        repo.create_transcript_revision("trans-rev-1", interview_id, revision_number=1, is_batch_final=False)

        live_segments = [
            {"id": "seg-1", "track_id": "interviewer", "start_time_ms": 1000, "end_time_ms": 4000, "text": "Вопрос про процессы, потоки и GIL в Python."},
            {"id": "seg-2", "track_id": "candidate", "start_time_ms": 4500, "end_time_ms": 15000, "text": "Процессы имеют изолированную память. Потоки разделяют память процесса. GIL защищает счетчики ссылок."},
            {"id": "seg-3", "track_id": "interviewer", "start_time_ms": 16000, "end_time_ms": 19000, "text": "Как обойти ограничения GIL при вычислениях?"},
            {"id": "seg-4", "track_id": "candidate", "start_time_ms": 19500, "end_time_ms": 30000, "text": "Использовать модуль multiprocessing или C-расширения с отпусканием GIL."},
            {"id": "seg-5", "track_id": "interviewer", "start_time_ms": 31000, "end_time_ms": 35000, "text": "Расскажите про паттерн Saga в микросервисах."},
            {"id": "seg-6", "track_id": "candidate", "start_time_ms": 35500, "end_time_ms": 50000, "text": "Паттерн Сага делает локальные транзакции сервисов и компенсирующие при сбоях."},
        ]

        for s in live_segments:
            repo.add_transcript_segment(
                segment_id=s["id"],
                interview_id=interview_id,
                track_id=s["track_id"],
                start_time_ms=s["start_time_ms"],
                end_time_ms=s["end_time_ms"],
                text=s["text"],
                is_final=True,
                revision_id="trans-rev-1",
            )

        # Question Associations for trans-rev-1
        repo.save_association("assoc-1", interview_id, "q1-linux-gil", "seg-2", 1.0, False, False, "Вопрос 1")
        repo.save_association("assoc-2", interview_id, "q2-gil-bypass", "seg-4", 1.0, False, False, "Вопрос 2")
        repo.save_association("assoc-3", interview_id, "q3-distributed-saga", "seg-6", 1.0, False, False, "Вопрос 3")

        # 3. AI Evaluation & Human Manual Override under trans-rev-1
        # Save AI Proposals for Q1, Q2, Q3
        repo.save_assessment_proposal(
            proposal_id="prop-q1",
            interview_id=interview_id,
            question_id="q1-linux-gil",
            model_profile_id="google/gemini-3.8-flash",
            scores=[{"criterion_id": "crit-core", "score": 4.0, "explanation": "Базовое объяснение", "evidence": [{"exact_quote": "Процессы имеют изолированную память."}]}],
            transcript_revision_id="trans-rev-1",
        )
        repo.save_assessment_proposal(
            proposal_id="prop-q2",
            interview_id=interview_id,
            question_id="q2-gil-bypass",
            model_profile_id="google/gemini-3.8-flash",
            scores=[{"criterion_id": "crit-bypass", "score": 4.5, "explanation": "Отличный ответ", "evidence": [{"exact_quote": "Использовать модуль multiprocessing"}]}],
            transcript_revision_id="trans-rev-1",
        )
        repo.save_assessment_proposal(
            proposal_id="prop-q3",
            interview_id=interview_id,
            question_id="q3-distributed-saga",
            model_profile_id="google/gemini-3.8-flash",
            scores=[{"criterion_id": "crit-saga", "score": 4.0, "explanation": "Хорошее понимание", "evidence": [{"exact_quote": "компенсирующие при сбоях"}]}],
            transcript_revision_id="trans-rev-1",
        )

        # Human Reviewer manually overrides Question 1 score to 4.8 with explicit justification!
        review_q1_resp = client.post(
            f"/api/v1/interviews/{interview_id}/assessments/q1-linux-gil/review",
            json={
                "expected_transcript_revision": "trans-rev-1",
                "scores": [{"criterion_id": "crit-core", "score": 4.8, "explanation": "Глубокое понимание разделения памяти"}],
                "reviewer_notes": "Ручная корректировка: кандидат отлично ориентируется в архитектуре Linux и GIL",
                "reviewer_id": "lead-architect-ivan",
                "is_manually_adjusted": True,
            },
        )
        assert review_q1_resp.status_code == 200
        print("[Step 3/8] Human reviewer submitted manual override for Question 1 (Score: 4.8/5.0, is_manually_adjusted=True)")

        # 4. Batch-STT Re-transcription (Generating trans-rev-2)
        # Improved transcript with higher accuracy & terminology
        batch_segments = [
            {"id": "seg-b1", "track_id": "interviewer", "start_time_ms": 1000, "end_time_ms": 4000, "text": "Вопрос про процессы, потоки и GIL в Python."},
            # Modified segment seg-b2 (Q1 text slightly refined)
            {"id": "seg-b2", "track_id": "candidate", "start_time_ms": 4500, "end_time_ms": 15000, "text": "Процессы в Linux имеют изолированное виртуальное адресное пространство. Потоки одного процесса разделяют общее адресное пространство. GIL в CPython защищает внутренние структуры и счетчики ссылок."},
            {"id": "seg-b3", "track_id": "interviewer", "start_time_ms": 16000, "end_time_ms": 19000, "text": "Как обойти ограничения GIL при вычислениях?"},
            {"id": "seg-b4", "track_id": "candidate", "start_time_ms": 19500, "end_time_ms": 30000, "text": "Использовать модуль multiprocessing или C-расширения с явным отпусканием GIL через Py_BEGIN_ALLOW_THREADS."},
            {"id": "seg-b5", "track_id": "interviewer", "start_time_ms": 31000, "end_time_ms": 35000, "text": "Расскажите про паттерн Saga в микросервисах."},
            # Modified segment seg-b6 (Q3 text significantly expanded)
            {"id": "seg-b6", "track_id": "candidate", "start_time_ms": 35500, "end_time_ms": 50000, "text": "Паттерн Saga реализует распределенные транзакции через последовательность локальных транзакций с оркестрацией или хореографией. В случае сбоя запускаются компенсирующие транзакции в обратном порядке."},
        ]

        # Enqueue BATCH_RETRANSCRIBE job
        repo.enqueue_job(
            job_id="job-batch-retranscribe",
            job_type="BATCH_RETRANSCRIBE",
            interview_id=interview_id,
            payload={
                "new_revision_id": "trans-rev-2",
                "old_revision_id": "trans-rev-1",
                "revision_number": 2,
                "segments": batch_segments,
            },
        )

        worker = PipelineWorker(repo)
        asyncio.run(worker.process_one_job())

        # Update associations for trans-rev-2
        repo.save_association("assoc-b1", interview_id, "q1-linux-gil", "seg-b2", 1.0, False, False, "Вопрос 1 (Batch)")
        repo.save_association("assoc-b2", interview_id, "q2-gil-bypass", "seg-b4", 1.0, False, False, "Вопрос 2 (Batch)")
        repo.save_association("assoc-b3", interview_id, "q3-distributed-saga", "seg-b6", 1.0, False, False, "Вопрос 3 (Batch)")
        print("[Step 4/8] Batch-STT re-transcription completed: created revision 'trans-rev-2'")

        # 5. Verify Invariant: Human Override Protection & Stale Invalidation
        human_q1 = repo.get_human_assessment_for_question(interview_id, "q1-linux-gil")
        assert human_q1 is not None, "Human assessment must exist"
        assert human_q1["scores"][0]["score"] == 4.8, "HUMAN OVERRIDE MUST NEVER BE OVERWRITTEN!"
        assert human_q1["is_manually_adjusted"] == 1
        assert "кандидат отлично ориентируется" in human_q1["reviewer_notes"]
        assert human_q1["is_stale"] == 1, "Modified transcript must flag human assessment as stale"
        print(f"[Step 5/8] Verified Human Override Invariant:")
        print(f"  - Q1 Score preserved: {human_q1['scores'][0]['score']}/5.0 (Notes: '{human_q1['reviewer_notes']}')")
        print(f"  - Q1 is_stale flagged: {bool(human_q1['is_stale'])} (Reason: '{human_q1['stale_reason']}')")

        # Test Transcript Diff API
        diff_resp = client.get(f"/api/v1/interviews/{interview_id}/revisions/transcript/diff?from_rev=trans-rev-1&to_rev=trans-rev-2")
        assert diff_resp.status_code == 200
        diff_data = diff_resp.json()
        assert diff_data["total_segment_diffs"] == 6
        assert len(diff_data["question_reports"]) == 3
        print(f"  - Revision Diff confirmed: {diff_data['total_segment_diffs']} segments analyzed across 3 questions")

        # 6. Verify Revision Conflict Protection (HTTP 409 Conflict)
        conflict_resp = client.post(
            f"/api/v1/interviews/{interview_id}/assessments/q1-linux-gil/review",
            json={
                "expected_transcript_revision": "trans-rev-1",  # Outdated revision!
                "scores": [{"criterion_id": "crit-core", "score": 5.0}],
                "reviewer_notes": "Attempting review against stale revision",
            },
        )
        assert conflict_resp.status_code == 409, f"Expected 409 Conflict for outdated revision, got {conflict_resp.status_code}"
        print(f"[Step 6/8] Conflict Protection Verified: Stale review submission rejected with HTTP 409 Conflict")
        print(f"  - Conflict detail: {conflict_resp.json()['detail']}")

        # Re-approve with expected revision 'trans-rev-2'
        valid_review_resp = client.post(
            f"/api/v1/interviews/{interview_id}/assessments/q1-linux-gil/review",
            json={
                "expected_transcript_revision": "trans-rev-2",  # Fresh revision
                "scores": [{"criterion_id": "crit-core", "score": 4.9}],
                "reviewer_notes": "Обновлено с учетом финальной стенограммы trans-rev-2",
                "reviewer_id": "lead-architect-ivan",
            },
        )
        assert valid_review_resp.status_code == 200
        print(f"  - Updated review on 'trans-rev-2' accepted successfully (Score: 4.9/5.0)")

        # Also confirm Q2 and Q3 under trans-rev-2
        client.post(
            f"/api/v1/interviews/{interview_id}/assessments/q2-gil-bypass/review",
            json={
                "expected_transcript_revision": "trans-rev-2",
                "scores": [{"criterion_id": "crit-bypass", "score": 4.7}],
                "reviewer_notes": "Подтверждено понимание C-расширений",
            },
        )
        client.post(
            f"/api/v1/interviews/{interview_id}/assessments/q3-distributed-saga/review",
            json={
                "expected_transcript_revision": "trans-rev-2",
                "scores": [{"criterion_id": "crit-saga", "score": 5.0}],
                "reviewer_notes": "Идеальный ответ по паттерну Saga",
            },
        )

        # 7. Generate & Confirm Executive Summary
        repo.enqueue_job(
            job_id="job-summary",
            job_type="GENERATE_SUMMARY",
            interview_id=interview_id,
            payload={"audio_health": {"is_healthy": True}},
        )
        asyncio.run(worker.process_one_job())

        sum_resp = client.get(f"/api/v1/interviews/{interview_id}/summary")
        assert sum_resp.status_code == 200
        sum_data = sum_resp.json()
        assert sum_data["has_summary"] is True
        summary_payload = sum_data["summary"]
        print(f"[Step 7/8] Executive Summary Generated via {sum_data['model_profile_id']}:")
        print(f"  - Recommendation: {summary_payload.get('hiring_recommendation')}")
        print(f"  - Rationale: {summary_payload.get('recommendation_rationale')[:100]}...")

        # Reviewer confirms summary
        conf_sum_resp = client.post(
            f"/api/v1/interviews/{interview_id}/summary/confirm",
            json={
                "reviewer_id": "lead-architect-ivan",
                "confirmed_markdown": summary_payload.get("summary_markdown", "Резюме утверждено."),
                "confirmed_recommendation": summary_payload.get("hiring_recommendation", "STRONG_HIRE"),
            },
        )
        assert conf_sum_resp.status_code == 200

        # 8. Finalize Report & Cryptographically Seal Export
        finalize_resp = client.post(
            f"/api/v1/interviews/{interview_id}/report/finalize",
            json={
                "confirmed_by": "lead-architect-ivan",
                "hiring_recommendation": summary_payload.get("hiring_recommendation", "STRONG_HIRE"),
            },
        )
        assert finalize_resp.status_code == 200
        fin_data = finalize_resp.json()
        assert fin_data["status"] == "finalized"
        assert fin_data["final_score_100"] > 90.0
        assert fin_data["coverage_percentage"] == 100.0
        assert len(fin_data["sha256_checksum"]) == 64
        print(f"[Step 8/8] Final Report Sealed with Cryptographic Hash:")
        print(f"  - Final Score: {fin_data['final_score_100']}/100")
        print(f"  - Coverage: {fin_data['coverage_percentage']}%")
        print(f"  - SHA-256 Checksum: {fin_data['sha256_checksum']}")

        # Verify Export
        export_resp = client.get(f"/api/v1/interviews/{interview_id}/export")
        assert export_resp.status_code == 200
        export_data = export_resp.json()
        assert export_data["is_draft"] is False
        assert export_data["sha256_checksum"] == fin_data["sha256_checksum"]
        assert export_data["report_revision"]["final_score_100"] == fin_data["final_score_100"]
        assert len(export_data["audit_trail"]) >= 8

        total_time = time.perf_counter() - start_time
        print(f"  - Total Stage 6 Runtime: {total_time:.2f}s")
        print("=" * 80)
        print("STAGE 6: FINAL PROCESSING CONTOUR & REVISION ACCEPTED 100%!")
        print("=" * 80)
        return True

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


if __name__ == "__main__":
    success = run_stage6_acceptance()
    sys.exit(0 if success else 1)
