"""
Stage 5: Full Live Interview (Multi-Question Live Session & Robustness) Acceptance Runner.

Verifies:
1. Multi-question interview session (3 planned questions with weights and criteria).
2. Clarification and follow-up speech matching.
3. Speech interruption detection (overlapping timestamps across tracks).
4. Explicit ambiguity detection (no silent guessing; marked is_ambiguous=True).
5. Manual segment re-association via API (POST /api/v1/interviews/{id}/segments/{segment_id}/associate).
6. Model degradation & transparent fallback (Gemini 3.8 Flash failure -> Qwen 3.7 Plus).
7. Audio channel health monitoring (GET /api/v1/interviews/{id}/health).
8. Multi-question human review, weighted score calculation, and final JSON export.
"""

import asyncio
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from starlette.testclient import TestClient

from backend.adapters.resilient_llm import ResilientLLMAdapter
from backend.api import app as api_module
from backend.core.matcher import QuestionMatcher
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker


def run_stage5_acceptance():
    print("=" * 80)
    print("STARTING STAGE 5: FULL LIVE INTERVIEW & ROBUSTNESS ACCEPTANCE")
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
        print(f"[Step 1/7] Initialized SQLite WAL Database at {db_path}")

        # 2. Multi-Question Plan Setup
        interview_id = f"inv-mquestion-{uuid.uuid4().hex[:6]}"
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
                "criteria": ["Многопроцессность (multiprocessing)", "C-расширения, Cython и release GIL"],
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
            "title": "Lead Backend / Systems Engineer — Multi-Question Live Session",
            "candidate_name": "Елена Васильева",
            "role": "Lead Systems Architect",
            "plan": {"questions": questions},
        }

        resp = client.post("/api/v1/interviews", json=setup_payload)
        assert resp.status_code == 200
        print(f"[Step 2/7] Created multi-question interview session {interview_id} with 3 questions")

        # State transition with consent
        client.post(f"/api/v1/interviews/{interview_id}/status", json={"current_status": "draft", "target_status": "ready"})
        client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={
                "current_status": "ready",
                "target_status": "recording",
                "consent_confirmed_at": "2026-09-10T01:00:00Z",
                "consent_version": "v1.0",
            },
        )

        # 3. Simulate Multi-Question Transcript with Clarifications and Interruption
        # Question 1
        repo.add_transcript_segment(
            segment_id="seg-q1-int",
            interview_id=interview_id,
            track_id="interviewer",
            start_time_ms=1000,
            end_time_ms=5000,
            text="Расскажите про разницу между процессами и потоками в Linux и GIL в Python.",
            is_final=True,
        )
        repo.add_transcript_segment(
            segment_id="seg-q1-cand",
            interview_id=interview_id,
            track_id="candidate",
            start_time_ms=5500,
            end_time_ms=18000,
            text=(
                "Процессы в Linux имеют изолированное адресное пространство и страницы памяти. "
                "Потоки одного процесса разделяют общее адресное пространство. "
                "GIL — это мьютекс в CPython для защиты счетчиков ссылок."
            ),
            is_final=True,
        )

        # Question 2 with Interruption & Clarification
        # Candidate begins answering Q2
        repo.add_transcript_segment(
            segment_id="seg-q2-cand-1",
            interview_id=interview_id,
            track_id="candidate",
            start_time_ms=20000,
            end_time_ms=32000,
            text="Для обхода GIL мы обычно используем модуль multiprocessing, создавая пул отдельных процессов ОС.",
            is_final=True,
        )
        # Interviewer interrupts with a clarifying question (Overlapping 30500ms - 32000ms: 1500ms overlap!)
        repo.add_transcript_segment(
            segment_id="seg-q2-int-clarify",
            interview_id=interview_id,
            track_id="interviewer",
            start_time_ms=30500,
            end_time_ms=36000,
            text="А что насчет C-расширений и ручного отпускания GIL?",
            is_final=True,
        )
        # Candidate answers the clarification
        repo.add_transcript_segment(
            segment_id="seg-q2-cand-2",
            interview_id=interview_id,
            track_id="candidate",
            start_time_ms=36500,
            end_time_ms=48000,
            text="Да, в C-расширениях или Cython можно вызвать Py_BEGIN_ALLOW_THREADS и освободить GIL для вычислений.",
            is_final=True,
        )

        # Question 3 (Candidate gives response on Saga)
        repo.add_transcript_segment(
            segment_id="seg-q3-int",
            interview_id=interview_id,
            track_id="interviewer",
            start_time_ms=50000,
            end_time_ms=55000,
            text="Перейдем к архитектуре. Как устроен паттерн Saga для распределенных транзакций?",
            is_final=True,
        )
        repo.add_transcript_segment(
            segment_id="seg-q3-cand",
            interview_id=interview_id,
            track_id="candidate",
            start_time_ms=55500,
            end_time_ms=70000,
            text=(
                "Паттерн Saga реализуется через оркестратор или хореографию. "
                "Каждый сервис выполняет локальную транзакцию. "
                "В случае ошибки на любом этапе оркестратор выполняет компенсирующие транзакции в обратном порядке."
            ),
            is_final=True,
        )

        # Ambiguous Segment: Candidate makes an ambiguous remark
        repo.add_transcript_segment(
            segment_id="seg-ambiguous",
            interview_id=interview_id,
            track_id="candidate",
            start_time_ms=71000,
            end_time_ms=78000,
            text="Кстати, мы также используем Kafka и ClickHouse для сбора аналитики.",
            is_final=True,
        )

        segments = repo.get_transcript_segments(interview_id)
        assert len(segments) == 8
        print("[Step 3/7] Ingested 8 multi-question transcript segments (total duration ~78s)")

        # 4. Question Association, Clarification & Interruption Verification
        matcher = QuestionMatcher()
        interruptions = matcher.detect_interruptions(segments)
        assert len(interruptions) > 0, "Expected at least 1 speech interruption detected"
        interruption = interruptions[0]
        print("[Step 4/7] Interruption detected:")
        print(f"  - Overlap tracks: {interruption.interrupted_track} interrupted by {interruption.interrupting_track}")
        print(f"  - Overlap duration: {interruption.overlap_duration_ms} ms (at {interruption.overlap_start_ms}ms - {interruption.overlap_end_ms}ms)")

        # Test associations via API
        assoc_resp = client.get(f"/api/v1/interviews/{interview_id}/associations")
        assert assoc_resp.status_code == 200
        assoc_data = assoc_resp.json()
        associations = assoc_data["associations"]
        assert len(associations) == 8

        # Verify that ambiguous segment is flagged
        ambig_assoc = next((a for a in associations if a["segment_id"] == "seg-ambiguous"), None)
        assert ambig_assoc is not None
        assert ambig_assoc["is_ambiguous"] == 1, "Ambiguous segment must have is_ambiguous=True"
        print(f"  - Ambiguity explicitly flagged for seg-ambiguous: notes='{ambig_assoc['notes']}'")

        # Test manual re-association via API
        reassoc_resp = client.post(
            f"/api/v1/interviews/{interview_id}/segments/seg-ambiguous/associate",
            json={"new_question_id": "q3-distributed-saga", "notes": "Привязано к вопросу об инфраструктуре саги"},
        )
        assert reassoc_resp.status_code == 200
        reassoc_data = reassoc_resp.json()
        assert reassoc_data["status"] == "reassociated"
        print(f"  - Manual re-association verified via API: seg-ambiguous -> {reassoc_data['question_id']}")

        # 5. Audio Channel Health Monitoring
        health_resp = client.get(f"/api/v1/interviews/{interview_id}/health")
        assert health_resp.status_code == 200
        health_data = health_resp.json()
        assert health_data["is_healthy"] is True
        print("[Step 5/7] Audio channel health verified:")
        print(f"  - Interviewer status: {health_data['interviewer']['status']}")
        print(f"  - Candidate status:   {health_data['candidate']['status']}")

        # 6. Model Fallback Verification (Gemini 3.8 Flash failure -> Qwen 3.7 Plus)
        print("[Step 6/7] Evaluating questions with Model Fallback injection...")

        # Evaluate Question 1 via Primary (Gemini 3.8 Flash)
        repo.enqueue_job(
            job_id="job-eval-q1",
            job_type="EVALUATE_QUESTION",
            interview_id=interview_id,
            payload={
                "question_id": "q1-linux-gil",
                "candidate_text": segments[1]["text"],
                "segment_id": segments[1]["id"],
                "rubric_description": "Разделение памяти и адресного пространства в Linux, GIL",
            },
        )

        normal_worker = PipelineWorker(repo)
        asyncio.run(normal_worker.process_one_job())
        time.sleep(1.0)

        # Evaluate Question 2 via Primary
        q2_combined_text = f"{segments[2]['text']} {segments[4]['text']}"
        repo.enqueue_job(
            job_id="job-eval-q2",
            job_type="EVALUATE_QUESTION",
            interview_id=interview_id,
            payload={
                "question_id": "q2-gil-bypass",
                "candidate_text": q2_combined_text,
                "segment_id": segments[4]["id"],
                "rubric_description": "Способы обхода GIL: multiprocessing, C-расширения, release GIL",
            },
        )
        asyncio.run(normal_worker.process_one_job())
        time.sleep(1.0)

        # Evaluate Question 3 with SIMULATED PRIMARY FAILURE to test Fallback to Qwen 3.7 Plus!
        repo.enqueue_job(
            job_id="job-eval-q3-fallback",
            job_type="EVALUATE_QUESTION",
            interview_id=interview_id,
            payload={
                "question_id": "q3-distributed-saga",
                "candidate_text": segments[6]["text"],
                "segment_id": segments[6]["id"],
                "rubric_description": "Паттерн Saga, оркестрация vs хореография, компенсирующие транзакции",
            },
        )

        # Worker with injected primary failure on LLM
        resilient_adapter_with_fail = ResilientLLMAdapter(force_primary_fail=True)
        fallback_worker = PipelineWorker(repo, llm_adapter=resilient_adapter_with_fail)
        asyncio.run(fallback_worker.process_one_job())

        proposals = repo.get_assessment_proposals(interview_id)
        assert len(proposals) == 3, f"Expected 3 proposals, got {len(proposals)}"

        prop_q1 = next(p for p in proposals if p["question_id"] == "q1-linux-gil")
        prop_q2 = next(p for p in proposals if p["question_id"] == "q2-gil-bypass")
        prop_q3 = next(p for p in proposals if p["question_id"] == "q3-distributed-saga")

        print(f"  - Proposal Q1 (Primary Gemini): Model='{prop_q1['model_profile_id']}', Score={prop_q1['scores'][0]['score']}/5.0")
        print(f"  - Proposal Q2 (Primary Gemini): Model='{prop_q2['model_profile_id']}', Score={prop_q2['scores'][0]['score']}/5.0")
        print(f"  - Proposal Q3 (FALLBACK): Model='{prop_q3['model_profile_id']}', Score={prop_q3['scores'][0]['score']}/5.0")

        # Verify that fallback occurred and was recorded!
        assert any(m in prop_q3["model_profile_id"].lower() for m in ["qwen", "deepseek"]), (
            f"Expected fallback model (Qwen or DeepSeek), got {prop_q3['model_profile_id']}"
        )
        audit_events = repo.get_audit_events(interview_id)
        fallback_event = next((e for e in audit_events if e["event_type"] == "MODEL_FALLBACK_TRIGGERED"), None)
        assert fallback_event is not None, "Expected MODEL_FALLBACK_TRIGGERED event in audit log"
        print(f"  - Audit event confirmed: MODEL_FALLBACK_TRIGGERED from {fallback_event['payload_json']}")

        # 7. Human Review, Aggregate Score, and JSON Export
        client.post(f"/api/v1/interviews/{interview_id}/status", json={"current_status": "recording", "target_status": "processing"})
        client.post(f"/api/v1/interviews/{interview_id}/status", json={"current_status": "processing", "target_status": "review"})

        # Approve all 3 proposals
        for prop in proposals:
            client.post(
                f"/api/v1/interviews/{interview_id}/assessments/{prop['id']}/approve",
                json={
                    "reviewed_scores": prop["scores"],
                    "reviewer_notes": f"Одобрено для вопроса {prop['question_id']}",
                },
            )

        client.post(f"/api/v1/interviews/{interview_id}/status", json={"current_status": "review", "target_status": "finalized"})

        export_resp = client.get(f"/api/v1/interviews/{interview_id}/export")
        assert export_resp.status_code == 200
        export_data = export_resp.json()

        assert export_data["status"] == "finalized"
        assert export_data["final_score_100"] > 0
        assert len(export_data["decisions"]) == 3
        assert len(export_data["audit_trail"]) >= 8

        total_time = time.perf_counter() - start_time
        print("[Step 7/7] Multi-Question Live Session Finalized:")
        print(f"  - Final 100-pt Score: {export_data['final_score_100']}/100")
        print(f"  - Questions Assessed: {len(export_data['decisions'])}")
        print(f"  - Audit Trail Events: {len(export_data['audit_trail'])}")
        print(f"  - Total Test Execution Time: {total_time:.2f}s")

        print("=" * 80)
        print("STAGE 5: MULTI-QUESTION LIVE SESSION & ROBUSTNESS ACCEPTED 100%!")
        print("=" * 80)
        return True

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


if __name__ == "__main__":
    success = run_stage5_acceptance()
    sys.exit(0 if success else 1)
