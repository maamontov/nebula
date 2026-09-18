"""
Stage 4: Vertical Slice (Single Question Live Run) Acceptance Runner.

Verifies the complete end-to-end slice according to Section 10 of docs/implementation-plan.md:
1. Session setup with a single approved interview question.
2. Participant consent acquired and state machine transitions (DRAFT -> READY -> RECORDING).
3. Real spoken Russian audio synthesized on macOS (say -> afconvert 16kHz mono WAV).
4. Audio chunks ingested into durable queue (TRANSCRIBE_AUDIO).
5. Pipeline worker processes chunks via live Whisper-large-v3-turbo API.
6. Transcript segments saved in SQLite WAL database.
7. Durable background evaluation job (EVALUATE_QUESTION) processed by google/gemini-3.8-flash.
8. EvidenceValidator verifies 100% of candidate quotations.
9. Full Traceability Check: Every score's evidence quote is matched with exact character offsets inside the transcript segment.
10. Human review & calibration with reviewer notes.
11. State machine transition to FINALIZED.
12. Final 100-point score calculation and structured JSON export with audit trail.
"""

import asyncio
import os
import subprocess
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

from backend.api import app as api_module
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker


def generate_russian_speech_wav(text: str, output_wav_path: str) -> None:
    """Synthesizes real acoustic Russian speech using macOS say + afconvert."""
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp_aiff:
        aiff_path = tmp_aiff.name

    try:
        # 1. Synthesize speech with Milena Russian voice
        subprocess.run(
            ["say", "-v", "Milena", text, "-o", aiff_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 2. Convert AIFF to 16kHz Mono 16-bit LE PCM WAV (Whisper standard)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", aiff_path, output_wav_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        if os.path.exists(aiff_path):
            os.remove(aiff_path)


def run_stage4_vertical_slice():
    print("=" * 80)
    print("STARTING STAGE 4: VERTICAL SLICE ACCEPTANCE (SINGLE QUESTION LIVE RUN)")
    print("=" * 80)

    start_total_time = time.perf_counter()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        db_path = tmp_db.name

    tmp_files = [db_path]

    try:
        # -------------------------------------------------------------
        # 1. Storage & Database Setup
        # -------------------------------------------------------------
        db = Database(db_path)
        db.init_schema()
        repo = Repository(db)
        api_module.app.dependency_overrides[api_module.get_repository] = lambda: repo
        client = TestClient(api_module.app)
        print(f"[Step 1/8] Initialized SQLite WAL Database at {db_path}")

        # -------------------------------------------------------------
        # 2. Setup Session with Single Approved Question
        # -------------------------------------------------------------
        interview_id = f"inv-vslice-{uuid.uuid4().hex[:6]}"
        question_id = "q-systems-linux-gil"
        question_text = "В чем разница между процессами и потоками в Linux, и как устроен GIL в CPython?"
        criteria = [
            "Разделение адресного пространства: изолированное у процессов, общее у потоков",
            "Устройство GIL в CPython: мьютекс счетчиков ссылок, отпускание при I/O и C-расширениях"
        ]

        setup_payload = {
            "id": interview_id,
            "title": "Systems Engineer — Single Question Vertical Slice",
            "candidate_name": "Дмитрий Смирнов",
            "role": "Senior Systems Engineer",
            "plan": {
                "questions": [
                    {
                        "question_id": question_id,
                        "text": question_text,
                        "weight": 1.0,
                        "criteria": criteria,
                    }
                ]
            }
        }

        resp = client.post("/api/v1/interviews", json=setup_payload)
        assert resp.status_code == 200, f"Create interview failed: {resp.text}"
        print(f"[Step 2/8] Created interview {interview_id} with approved question: '{question_text}'")

        # -------------------------------------------------------------
        # 3. State Machine Transitions & Privacy Consent
        # -------------------------------------------------------------
        r_ready = client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={"current_status": "draft", "target_status": "ready"}
        )
        assert r_ready.status_code == 200

        r_rec = client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={
                "current_status": "ready",
                "target_status": "recording",
                "consent_confirmed_at": "2026-09-10T00:55:00Z",
                "consent_version": "v1.0"
            }
        )
        assert r_rec.status_code == 200
        print("[Step 3/8] State machine transitioned: DRAFT -> READY -> RECORDING (Participant consent verified)")

        # -------------------------------------------------------------
        # 4. Generate Real Russian Audio & Ingest via Whisper STT
        # -------------------------------------------------------------
        interviewer_speech = "Расскажите про разницу между процессами и потоками в Linux и как работает GIL в Python."
        candidate_speech = (
            "В операционной системе Linux процесс имеет собственное изолированное виртуальное адресное пространство. "
            "Потоки одного процесса разделяют общее адресное пространство и память. "
            "Что касается GIL в CPython — это глобальный мьютекс интерпретатора, предотвращающий одновременное "
            "выполнение байткода несколькими потоками для защиты счетчиков ссылок объектов. "
            "При операциях ввода-вывода GIL явно отпускается."
        )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as q_wav:
            q_wav_path = q_wav.name
            tmp_files.append(q_wav_path)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as a_wav:
            a_wav_path = a_wav.name
            tmp_files.append(a_wav_path)

        print("[Step 4/8] Synthesizing acoustic speech audio files (16 kHz mono WAV)...")
        generate_russian_speech_wav(interviewer_speech, q_wav_path)
        generate_russian_speech_wav(candidate_speech, a_wav_path)
        print(f"  - Interviewer audio: {os.path.getsize(q_wav_path)} bytes")
        print(f"  - Candidate audio: {os.path.getsize(a_wav_path)} bytes")

        with open(q_wav_path, "rb") as f:
            q_bytes = f.read()
        with open(a_wav_path, "rb") as f:
            a_bytes = f.read()

        # Enqueue STT jobs
        repo.enqueue_job(
            job_id="job-stt-q",
            job_type="TRANSCRIBE_AUDIO",
            interview_id=interview_id,
            payload={
                "audio_hex": q_bytes.hex(),
                "track_id": "interviewer",
                "start_ms": 1000,
                "end_ms": 6500,
                "segment_id": "seg-vslice-interviewer",
                "language": "ru",
            }
        )

        repo.enqueue_job(
            job_id="job-stt-a",
            job_type="TRANSCRIBE_AUDIO",
            interview_id=interview_id,
            payload={
                "audio_hex": a_bytes.hex(),
                "track_id": "candidate",
                "start_ms": 7000,
                "end_ms": 32000,
                "segment_id": "seg-vslice-candidate",
                "language": "ru",
            }
        )

        # Process STT jobs with durable PipelineWorker
        worker = PipelineWorker(repo)
        print("  - Running PipelineWorker on Whisper-large-v3-turbo jobs...")
        t_stt_start = time.perf_counter()

        async def run_stt_worker():
            for _ in range(3):
                did_work = await worker.process_one_job()
                if not did_work:
                    break

        asyncio.run(run_stt_worker())
        t_stt_elapsed = time.perf_counter() - t_stt_start

        segments = repo.get_transcript_segments(interview_id)
        assert len(segments) == 2, f"Expected 2 segments, got {len(segments)}"
        cand_seg = next(s for s in segments if s["track_id"] == "candidate")
        int_seg = next(s for s in segments if s["track_id"] == "interviewer")

        print(f"  - Whisper STT Completed in {t_stt_elapsed:.2f}s:")
        print(f"    * Interviewer: \"{int_seg['text']}\"")
        print(f"    * Candidate:   \"{cand_seg['text']}\"")

        # -------------------------------------------------------------
        # 5. Live AI Assessment with Gemini 3.8 Flash & EvidenceValidator
        # -------------------------------------------------------------
        repo.enqueue_job(
            job_id="job-eval-vslice",
            job_type="EVALUATE_QUESTION",
            interview_id=interview_id,
            payload={
                "question_id": question_id,
                "candidate_text": cand_seg["text"],
                "segment_id": cand_seg["id"],
                "rubric_description": "; ".join(criteria),
            }
        )

        print("[Step 5/8] Running PipelineWorker on EVALUATE_QUESTION job via google/gemini-3.8-flash...")
        t_llm_start = time.perf_counter()

        async def run_eval_worker():
            for _ in range(2):
                did_work = await worker.process_one_job()
                if not did_work:
                    break

        asyncio.run(run_eval_worker())
        t_llm_elapsed = time.perf_counter() - t_llm_start

        proposals = repo.get_assessment_proposals(interview_id)
        assert len(proposals) == 1, f"Expected 1 proposal, got {len(proposals)}"
        proposal = proposals[0]

        scores = proposal.get("scores", [])
        assert len(scores) > 0
        proposal_score = scores[0]["score"]
        evidence_list = scores[0].get("evidence", [])
        assert len(evidence_list) > 0, "Proposal must have evidence quotes"

        print(f"  - Gemini 3.8 Flash Completed in {t_llm_elapsed:.2f}s:")
        print(f"    * Score: {proposal_score}/5.0")
        print(f"    * Explanation: {scores[0]['explanation']}")
        print(f"    * Quotes count: {len(evidence_list)}")

        # -------------------------------------------------------------
        # 6. Verify Evidence Traceability ("по каждому баллу можно открыть подтверждающий текст")
        # -------------------------------------------------------------
        print("[Step 6/8] Verifying Evidence Traceability and Quote Matching...")
        for ev in evidence_list:
            quote = ev["exact_quote"]
            seg_id = ev["segment_id"]
            # Lookup referenced segment
            matching_seg = next((s for s in segments if s["id"] == seg_id), None)
            assert matching_seg is not None, f"Segment {seg_id} not found in transcript!"

            seg_text = matching_seg["text"]
            assert quote in seg_text, f"Quote '{quote}' NOT found in segment text '{seg_text}'"

            start_char = seg_text.index(quote)
            end_char = start_char + len(quote)
            print(f"  [TRACEABLE] Quote '{quote}' verified at [{matching_seg['start_time_ms']}ms - {matching_seg['end_time_ms']}ms], char offsets [{start_char}:{end_char}]")

        # -------------------------------------------------------------
        # 7. Human Review & Lifecycle Finalization
        # -------------------------------------------------------------
        client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={"current_status": "recording", "target_status": "processing"}
        )
        client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={"current_status": "processing", "target_status": "review"}
        )

        # Human Reviewer approves the proposal
        app_resp = client.post(
            f"/api/v1/interviews/{interview_id}/assessments/{proposal['id']}/approve",
            json={
                "reviewed_scores": scores,
                "reviewer_notes": "Ответ образцовый, кандидат четко понимает устройство адресного пространства и GIL."
            }
        )
        assert app_resp.status_code == 200

        fin_resp = client.post(
            f"/api/v1/interviews/{interview_id}/status",
            json={"current_status": "review", "target_status": "finalized"}
        )
        assert fin_resp.status_code == 200
        print("[Step 7/8] Human Reviewer confirmed evaluation. Session transitioned to FINALIZED.")

        # -------------------------------------------------------------
        # 8. Structured Export & Audit Trail Verification
        # -------------------------------------------------------------
        export_resp = client.get(f"/api/v1/interviews/{interview_id}/export")
        assert export_resp.status_code == 200
        export_data = export_resp.json()

        assert export_data["interview_id"] == interview_id
        expected_score = round((proposal_score / 5.0) * 100.0, 2)
        assert export_data["final_score_100"] == expected_score
        assert len(export_data["decisions"]) == 1
        assert len(export_data["audit_trail"]) >= 6

        total_elapsed = time.perf_counter() - start_total_time
        print("[Step 8/8] Export and Verification Summary:")
        print(f"  - Final 100-pt Score: {export_data['final_score_100']}/100")
        print(f"  - Audit Trail Events: {len(export_data['audit_trail'])}")
        print(f"  - STT Latency: {t_stt_elapsed:.2f}s")
        print(f"  - LLM Latency: {t_llm_elapsed:.2f}s")
        print(f"  - Total End-to-End Slice Runtime: {total_elapsed:.2f}s")

        print("=" * 80)
        print("STAGE 4: VERTICAL SLICE ACCEPTANCE SUCCEEDED 100%!")
        print("=" * 80)
        return True

    finally:
        for f in tmp_files:
            if os.path.exists(f):
                os.remove(f)


if __name__ == "__main__":
    success = run_stage4_vertical_slice()
    sys.exit(0 if success else 1)
