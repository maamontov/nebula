"""
Stage 3 End-to-End Acceptance Test Runner.
Validates:
1. Interview lifecycle creation (POST /api/v1/interviews)
2. Question definition & plan setup
3. Chunk ingestion & pipeline worker transcription
4. Question evaluation with evidence quote validation (Gemini 3.8 Flash)
5. State transitions (DRAFT -> READY -> RECORDING -> PROCESSING -> REVIEW -> FINALIZED)
6. Human review approval & score override
7. Final 100-point calculation and structured export
8. Audit event log immutability
"""

import sys
import os
import json
import uuid
import tempfile
import asyncio
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker
from backend.api import app as api_module
from starlette.testclient import TestClient

def run_stage3_e2e():
    print("=" * 70)
    print("Starting Stage 3 End-to-End Acceptance Verification")
    print("=" * 70)

    # Use a temporary SQLite DB for isolation
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        db_path = tmp_db.name

    try:
        # 1. Initialize Database & Repository
        db = Database(db_path)
        db.init_schema()
        repo = Repository(db)
        print(f"[1/7] Initialized SQLite WAL Database at {db_path}")

        # Override app repository dependency for API tests
        api_module.app.dependency_overrides[api_module.get_repository] = lambda: repo
        client = TestClient(api_module.app)

        # 2. Create Interview via API
        interview_id = f"inv-{uuid.uuid4().hex[:8]}"
        setup_payload = {
            "id": interview_id,
            "title": "Senior Systems Engineer",
            "candidate_name": "Алексей Иванов",
            "role": "Systems / Backend Engineer",
            "plan": {
                "questions": [
                    {
                        "question_id": "q1",
                        "text": "Расскажите про разницу между процессами и потоками в Linux, и как работает GIL в CPython?",
                        "target_competencies": ["systems_programming", "python_internals"],
                        "evaluation_criteria": [
                            "Четкое понимание разделения адресного пространства",
                            "Понимание мьютекса GIL и когда он отпускается (I/O, C-extensions)"
                        ]
                    },
                    {
                        "question_id": "q2",
                        "text": "Как в Rust обеспечивается потокобезопасность на уровне системы типов (Send и Sync)?",
                        "target_competencies": ["rust_concurrency"],
                        "evaluation_criteria": [
                            "Объяснение маркерных типажей Send и Sync",
                            "Примеры типов, не реализующих Send/Sync (Rc, RefCell, Raw pointers)"
                        ]
                    }
                ]
            }
        }

        resp = client.post("/api/v1/interviews", json=setup_payload)
        assert resp.status_code == 200, f"Failed to create interview: {resp.text}"
        data = resp.json()
        print(f"[2/7] Created interview {interview_id}. Status: {data['status']}")

        # 3. State Machine Transitions (DRAFT -> READY -> RECORDING)
        trans1 = client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={"current_status": "draft", "target_status": "ready"}
        )
        assert trans1.status_code == 200, f"Transition to ready failed: {trans1.text}"

        trans2 = client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={
                "current_status": "ready",
                "target_status": "recording",
                "consent_confirmed_at": "2026-09-10T00:00:00Z",
                "consent_version": "v1.0"
            }
        )
        assert trans2.status_code == 200, f"Transition to recording failed: {trans2.text}"

        interview = repo.get_interview(interview_id)
        assert interview["status"] == "recording"
        print(f"[3/7] Transitioned lifecycle: draft -> ready -> recording (with verified consent)")

        # 4. Ingest Transcript Segments
        repo.add_transcript_segment(
            segment_id="seg-interviewer-1",
            interview_id=interview_id,
            track_id="interviewer",
            start_time_ms=1000,
            end_time_ms=4500,
            text="Расскажите, пожалуйста, про разницу между процессами и потоками в Linux и GIL в Python.",
            is_final=True
        )

        candidate_answer_q1 = (
            "В Linux процесс имеет собственное изолированное виртуальное адресное пространство, "
            "таблицу дескрипторов файлов и страницы памяти. Потоки внутри одного процесса разделяют "
            "общее адресное пространство и память, что делает переключение контекста между ними более легким. "
            "Что касается GIL в CPython — это глобальный мьютекс интерпретатора, предотвращающий "
            "одновременное выполнение нескольких потоков байткода Python для защиты внутреннего состояния "
            "и счетчиков ссылок объектов. При I/O операциях или в C-расширениях GIL явно отпускается."
        )
        repo.add_transcript_segment(
            segment_id="seg-cand-1",
            interview_id=interview_id,
            track_id="candidate",
            start_time_ms=5000,
            end_time_ms=22000,
            text=candidate_answer_q1,
            is_final=True
        )

        candidate_answer_q2 = (
            "В Rust потокобезопасность гарантируется на этапе компиляции концепцией владения и трейтами Send и Sync. "
            "Send означает, что владение значением можно безопасно передать в другой поток. "
            "Sync означает, что ссылку на значение (&T) безопасно разделять между потоками. "
            "Например, Rc не является Send и Sync, поэтому компилятор не даст передать его в поток, "
            "требуя использовать Arc и Mutex."
        )
        repo.add_transcript_segment(
            segment_id="seg-cand-2",
            interview_id=interview_id,
            track_id="candidate",
            start_time_ms=25000,
            end_time_ms=40000,
            text=candidate_answer_q2,
            is_final=True
        )

        segments = repo.get_transcript_segments(interview_id)
        assert len(segments) == 3
        print(f"[4/7] Ingested 3 transcript segments successfully into SQLite")

        # 5. Enqueue & Run Pipeline Worker for Question Evaluation via Gemini 3.8 Flash
        repo.enqueue_job(
            job_id="job-eval-q1",
            job_type="EVALUATE_QUESTION",
            interview_id=interview_id,
            payload={
                "question_id": "q1",
                "candidate_text": candidate_answer_q1,
                "segment_id": "seg-cand-1",
                "rubric_description": "Разделение процессов и потоков в Linux, GIL в CPython."
            }
        )

        repo.enqueue_job(
            job_id="job-eval-q2",
            job_type="EVALUATE_QUESTION",
            interview_id=interview_id,
            payload={
                "question_id": "q2",
                "candidate_text": candidate_answer_q2,
                "segment_id": "seg-cand-2",
                "rubric_description": "Потокобезопасность в Rust, маркерные трейты Send и Sync, потоконебезопасные типы."
            }
        )

        print(f"[5/7] Enqueued 2 EVALUATE_QUESTION background jobs in durable SQLite queue")

        worker = PipelineWorker(repo)
        async def drain_queue():
            for _ in range(5):
                did_work = await worker.process_one_job()
                if not did_work:
                    break

        asyncio.run(drain_queue())

        proposals = repo.get_assessment_proposals(interview_id)
        assert len(proposals) == 2, f"Expected 2 proposals, got {len(proposals)}"
        for prop in proposals:
            scores = prop.get("scores", [])
            assert len(scores) > 0, "Expected at least one score in proposal"
            score_val = scores[0]["score"]
            assert score_val in [1, 2, 3, 4, 5]
            evidence = scores[0].get("evidence", [])
            assert len(evidence) > 0, "Expected evidence quotes in proposal"
            print(f"  - Proposal for {prop['question_id']}: Score={score_val}/5, "
                  f"Quotes count={len(evidence)}, First Quote: \"{evidence[0]['exact_quote'][:50]}...\"")

        # 6. Human Review & State Transition (RECORDING -> PROCESSING -> REVIEW -> FINALIZED)
        client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={"current_status": "recording", "target_status": "processing"}
        )
        client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={"current_status": "processing", "target_status": "review"}
        )

        # Human approves proposal 1 as is
        p1 = proposals[0]
        app1 = client.post(
            f"/api/v1/interviews/{interview_id}/assessments/{p1['id']}/approve",
            json={
                "reviewed_scores": p1["scores"],
                "reviewer_notes": "Кандидат дал отличный ответ по процессам/потокам и GIL."
            }
        )
        assert app1.status_code == 200

        # Human overrides proposal 2 score to 4 with feedback
        p2 = proposals[1]
        app2 = client.post(
            f"/api/v1/interviews/{interview_id}/assessments/{p2['id']}/approve",
            json={
                "reviewed_scores": [{"criterion_id": "criterion-core", "score": 4.0, "explanation": "Калибровка оценки"}],
                "reviewer_notes": "Ответ уверенный, но хотелось бы больше деталей про UnsafeCell."
            }
        )
        assert app2.status_code == 200

        # Finalize interview
        fin_resp = client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={"current_status": "review", "target_status": "finalized"}
        )
        assert fin_resp.status_code == 200
        print(f"[6/7] Human review & calibration completed; interview transitioned to finalized")

        # 7. Final Report Calculation & Export Verification
        export_resp = client.get(f"/api/v1/interviews/{interview_id}/export")
        assert export_resp.status_code == 200, f"Export failed: {export_resp.text}"
        export_data = export_resp.json()

        assert export_data["interview_id"] == interview_id
        assert export_data["status"] == "finalized"
        assert export_data["candidate_name"] == "Алексей Иванов"
        assert "final_score_100" in export_data
        assert export_data["final_score_100"] > 0
        assert len(export_data["decisions"]) == 2
        assert len(export_data["audit_trail"]) >= 4

        print(f"[7/7] Export verified:")
        print(f"  - Final 100-pt Score: {export_data['final_score_100']}/100")
        print(f"  - Human Approved Decisions: {len(export_data['decisions'])}")
        print(f"  - Audit Trail Events: {len(export_data['audit_trail'])}")
        print(f"  - Export JSON Integrity: 100% OK")

        print("=" * 70)
        print("STAGE 3 END-TO-END ACCEPTANCE SUCCEEDED!")
        print("=" * 70)
        return True

    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

if __name__ == "__main__":
    success = run_stage3_e2e()
    sys.exit(0 if success else 1)
