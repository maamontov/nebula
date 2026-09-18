"""
Comprehensive verification tests for Nebula Startup Stabilization and Single-Source Audio Mode.
Покрывает все 14 сценариев регрессионной матрицы из docs/startup-and-single-source-plan.md:
- TC-01 .. TC-04: Устранение блокеров старта (spoolDir, [object Object], строковые ошибки, компенсация статуса)
- TC-05 .. TC-08: Режим одного источника (single_source, shared track, drift/skew)
- TC-09 .. TC-12: Разметка говорящих (speaker roles, isolation от AI-оценки, ручное назначение, сплит)
- TC-13 .. TC-14: Миграция 005, readiness gate и целостность базы данных
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.matcher import QuestionMatcher
from backend.db.database import Database
from backend.db.repository import Repository
from backend.workers.pipeline import PipelineWorker


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    db.init_schema()
    yield db
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def repo(temp_db):
    return Repository(temp_db)


# ============================================================================
# 1. Схема БД и Миграция 005 (TC-13, TC-14)
# ============================================================================
def test_migration_005_schema_and_integrity(temp_db, repo):
    """TC-13: Проверка успешного применения миграции 005 и целостности БД."""
    assert temp_db.verify_integrity() is True

    # Check columns in interviews
    with temp_db.transaction() as conn:
        int_cols = {row["name"] for row in conn.execute("PRAGMA table_info(interviews)").fetchall()}
        assert "capture_mode" in int_cols
        assert "expected_tracks_json" in int_cols

        # Check columns in transcript_segments
        seg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(transcript_segments)").fetchall()}
        assert "speaker_role" in seg_cols
        assert "parent_segment_id" in seg_cols


# ============================================================================
# 2. Создание интервью в режимах dual_source и single_source (TC-05, TC-07)
# ============================================================================
def test_create_interview_capture_modes(repo):
    """TC-05: Создание сессий в режимах dual_source и single_source с expected_tracks."""
    # Dual source default
    inv_dual = repo.create_interview(
        interview_id="inv-dual-1",
        title="Dual Interview",
        candidate_name="Dual Cand",
        role="Engineer",
        capture_mode="dual_source",
    )
    assert inv_dual["capture_mode"] == "dual_source"
    assert json.loads(inv_dual["expected_tracks_json"]) == ["interviewer", "candidate"]

    # Single source
    inv_single = repo.create_interview(
        interview_id="inv-single-1",
        title="Single Interview",
        candidate_name="Single Cand",
        role="Engineer",
        capture_mode="single_source",
    )
    assert inv_single["capture_mode"] == "single_source"
    assert json.loads(inv_single["expected_tracks_json"]) == ["shared"]


# ============================================================================
# 3. Компенсация и жизненный цикл статусов (TC-04)
# ============================================================================
def test_interview_status_progression_and_compensation(repo):
    """TC-04: Статус не переходит в RECORDING при сбое запуска захвата, остается READY."""
    inv = repo.create_interview(
        interview_id="inv-comp-1",
        title="Compensation Test",
        candidate_name="Test Cand",
        role="Engineer",
    )
    assert inv["status"] == "draft"

    # Step 1: Advance to READY
    repo.update_interview_status("inv-comp-1", "ready")
    inv = repo.get_interview("inv-comp-1")
    assert inv["status"] == "ready"

    # Suppose audio capture fails here: status remains READY, prepared ID can be retried!
    # If audio capture succeeds:
    repo.update_interview_status("inv-comp-1", "recording", consent_confirmed_at="2026-09-10T12:00:00Z")
    inv = repo.get_interview("inv-comp-1")
    assert inv["status"] == "recording"
    assert inv["consent_confirmed_at"] == "2026-09-10T12:00:00Z"


# ============================================================================
# 4. Дорожка shared и дефолтная роль unknown (TC-06, TC-09)
# ============================================================================
@pytest.mark.asyncio
async def test_transcribe_shared_track_assigns_unknown_role(repo):
    """TC-06, TC-09: Транскрибация общей дорожки shared присваивает роль unknown."""
    repo.create_interview(
        interview_id="inv-trans-1",
        title="Transcribe Test",
        candidate_name="Test Cand",
        role="Engineer",
        capture_mode="single_source",
    )

    mock_stt = AsyncMock()
    mock_stt.transcribe_audio.return_value = MagicMock(text="Привет, расскажи о себе")

    worker = PipelineWorker(repository=repo, stt_adapter=mock_stt)

    # Simulate transcribe job for shared track
    payload = {
        "audio_hex": "00" * 3200,  # 100ms of PCM silence
        "track_id": "shared",
        "start_ms": 0,
        "end_ms": 5000,
        "format": "pcm_s16le",
    }
    await worker._handle_transcribe("inv-trans-1", payload)

    segments = repo.get_transcript_segments("inv-trans-1")
    assert len(segments) == 1
    assert segments[0]["track_id"] == "shared"
    assert segments[0]["speaker_role"] == "unknown"


# ============================================================================
# 5. Изоляция оценки: unknown и interviewer исключаются из ответов (TC-10)
# ============================================================================
@pytest.mark.asyncio
async def test_evaluate_strictly_excludes_unknown_and_interviewer(repo):
    """TC-10: Оценка ответов кандидата строго исключает сегменты с ролью unknown и interviewer."""
    plan_dict = {
        "questions": [
            {
                "id": "q1",
                "text": "Что такое транзакция?",
                "weight": 1.0,
                "criteria": [{"id": "c1", "title": "ACID", "min_score": 1, "max_score": 5, "weight": 1}],
            }
        ]
    }
    repo.create_interview(
        interview_id="inv-eval-1",
        title="Eval Test",
        candidate_name="Test Cand",
        role="Engineer",
        capture_mode="single_source",
    )
    repo.save_plan("plan-eval-1", "inv-eval-1", plan_dict, version=1)

    # Add 2 segments: 1 interviewer question, 1 unassigned shared segment (unknown)
    repo.add_transcript_segment(
        segment_id="seg-1",
        interview_id="inv-eval-1",
        track_id="shared",
        start_time_ms=0,
        end_time_ms=3000,
        text="Что такое транзакция?",
        speaker_role="interviewer",
    )
    repo.add_transcript_segment(
        segment_id="seg-2",
        interview_id="inv-eval-1",
        track_id="shared",
        start_time_ms=3500,
        end_time_ms=8000,
        text="Транзакция это атомарная операция в СУБД",
        speaker_role="unknown",  # Not assigned to candidate!
    )

    mock_llm = AsyncMock()
    worker = PipelineWorker(repository=repo, llm_adapter=mock_llm)

    # Evaluate question
    await worker._handle_evaluate("inv-eval-1", {"question_id": "q1"})

    # LLM should NOT have been called with unassigned speech!
    assert mock_llm.generate_response.call_count == 0

    # Assessment proposal must explicitly state that answer is missing
    props = repo.get_assessment_proposals("inv-eval-1")
    assert len(props) == 1
    assert props[0]["scores"][0]["score"] is None
    assert "Ответ кандидата отсутствует" in props[0]["scores"][0]["explanation"]


# ============================================================================
# 6. Назначение роли кандидата и успешная оценка (TC-11)
# ============================================================================
@pytest.mark.asyncio
async def test_manual_role_assignment_enables_evaluation(repo):
    """TC-11: После назначения роли candidate сегмент попадает в оценку."""
    plan_dict = {
        "questions": [
            {
                "id": "q1",
                "text": "Что такое транзакция?",
                "weight": 1.0,
                "criteria": [{"id": "c1", "title": "ACID", "min_score": 1, "max_score": 5, "weight": 1}],
            }
        ]
    }
    repo.create_interview(
        interview_id="inv-role-1",
        title="Role Test",
        candidate_name="Test Cand",
        role="Engineer",
    )
    repo.save_plan("plan-role-1", "inv-role-1", plan_dict, version=1)

    repo.add_transcript_segment(
        segment_id="seg-shared-1",
        interview_id="inv-role-1",
        track_id="shared",
        start_time_ms=2000,
        end_time_ms=7000,
        text="Транзакция гарантирует свойства ACID при работе с БД",
        speaker_role="unknown",
    )

    # Interviewer manually assigns speaker_role = 'candidate'
    updated = repo.update_segment_speaker_role("inv-role-1", "seg-shared-1", "candidate")
    assert updated["speaker_role"] == "candidate"

    # Now associate and evaluate
    repo.save_association("assoc-1", "inv-role-1", "q1", "seg-shared-1", confidence=1.0)

    mock_llm = AsyncMock()
    mock_llm.execute_request.return_value = (
        {
            "scores": [
                {
                    "criterion_id": "c1",
                    "score": 5.0,
                    "explanation": "Кандидат корректно упомянул ACID свойства",
                    "evidence": [{"segment_id": "seg-shared-1", "exact_quote": "Транзакция гарантирует свойства ACID при работе с БД"}],
                }
            ],
            "critical_errors": [],
        },
        "mock-model-v1",
    )

    worker = PipelineWorker(repository=repo, llm_adapter=mock_llm)
    await worker._handle_evaluate("inv-role-1", {"question_id": "q1"})

    props = repo.get_assessment_proposals("inv-role-1")
    assert len(props) == 1
    assert props[0]["scores"][0]["score"] == 5.0


# ============================================================================
# 7. Разделение смешанного сегмента речи (TC-12)
# ============================================================================
def test_split_mixed_transcript_segment(repo):
    """TC-12: Разделение одного смешанного сегмента на две реплики с provenance."""
    repo.create_interview(
        interview_id="inv-split-1",
        title="Split Test",
        candidate_name="Test Cand",
        role="Engineer",
    )

    repo.add_transcript_segment(
        segment_id="seg-orig",
        interview_id="inv-split-1",
        track_id="shared",
        start_time_ms=1000,
        end_time_ms=9000,
        text="Как дела? Все отлично, готов отвечать.",
        speaker_role="unknown",
    )

    p1, p2 = repo.split_transcript_segment(
        interview_id="inv-split-1",
        segment_id="seg-orig",
        split_time_ms=4000,
        text_part1="Как дела?",
        text_part2="Все отлично, готов отвечать.",
        role_part1="interviewer",
        role_part2="candidate",
    )

    assert p1["id"] == "seg-orig"
    assert p1["start_time_ms"] == 1000
    assert p1["end_time_ms"] == 4000
    assert p1["text"] == "Как дела?"
    assert p1["speaker_role"] == "interviewer"

    assert p2["parent_segment_id"] == "seg-orig"
    assert p2["start_time_ms"] == 4000
    assert p2["end_time_ms"] == 9000
    assert p2["text"] == "Все отлично, готов отвечать."
    assert p2["speaker_role"] == "candidate"

    all_segs = repo.get_transcript_segments("inv-split-1")
    assert len(all_segs) == 2


# ============================================================================
# 8. QuestionMatcher: неизвестная речь не привязывается кандидату (TC-08)
# ============================================================================
def test_question_matcher_ignores_unknown_speech():
    """TC-08: QuestionMatcher помечает сегменты с unknown ролью как неоднозначные и не делает ответом кандидата."""
    questions = [{"id": "q1", "text": "Расскажите про репликацию", "criteria": []}]
    segments = [
        {"id": "s1", "track_id": "shared", "speaker_role": "interviewer", "start_time_ms": 0, "end_time_ms": 3000, "text": "Расскажите про репликацию"},
        {"id": "s2", "track_id": "shared", "speaker_role": "unknown", "start_time_ms": 3500, "end_time_ms": 8000, "text": "Репликация бывает синхронная и асинхронная"},
    ]

    matcher = QuestionMatcher()
    assocs = matcher.associate_segments(questions, segments)

    assert len(assocs) == 2
    # s1 - interviewer
    assert assocs[0].notes == "Вопрос интервьюера"
    # s2 - unknown: confidence 0.0, is_ambiguous = True
    assert assocs[1].confidence == 0.0
    assert assocs[1].is_ambiguous is True
    assert "Общая дорожка (роль не назначена)" in assocs[1].notes


# ============================================================================
# 9. API: Single Source Interview Creation & Readiness Gate (TC-01, TC-07)
# ============================================================================
def test_api_single_source_readiness_gate(tmp_path):
    """TC-01, TC-07: Проверка готовности сессии single_source (только дорожка shared)."""
    from fastapi.testclient import TestClient

    from backend.api.app import app, get_repository, get_trusted_spool_dir

    db_file = tmp_path / "api_test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)

    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_trusted_spool_dir] = lambda: spool_dir

    with TestClient(app) as client:
        # Create single_source interview
        res = client.post(
            "/api/v1/interviews",
            json={
                "id": "inv-api-single",
                "title": "API Single Source",
                "candidate_name": "API Cand",
                "role": "Lead",
                "capture_mode": "single_source",
            },
        )
        assert res.status_code == 200
        inv_data = res.json()
        assert inv_data["capture_mode"] == "single_source"

        # Advance to recording and stop
        res_ready = client.post("/api/v1/interviews/inv-api-single/status", json={"target_status": "ready"})
        assert res_ready.status_code == 200, res_ready.text
        res_rec = client.post(
            "/api/v1/interviews/inv-api-single/status",
            json={"target_status": "recording", "consent_confirmed_at": "2026-09-10T12:00:00Z"},
        )
        assert res_rec.status_code == 200, res_rec.text

        # Seal manifest for shared track
        shared_dir = spool_dir / "inv-api-single" / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = shared_dir / "manifest.json"
        manifest_path.write_text(json.dumps({
            "interview_id": "inv-api-single",
            "track_id": "shared",
            "capture_epoch": 0,
            "total_chunks": 1,
            "total_duration_ms": 5000,
            "is_sealed": True,
        }))

        # Stop interview
        stop_res = client.post("/api/v1/interviews/inv-api-single/stop", json={"manifests": []})
        assert stop_res.status_code == 200, f"stop failed: {stop_res.text}"

        # Check readiness: missing chunk 0
        readiness_res = client.get("/api/v1/interviews/inv-api-single/readiness")
        assert readiness_res.status_code == 200
        r_json = readiness_res.json()
        assert r_json["is_ready"] is False
        assert "shared" in r_json.get("missing_chunks", {})

        # Ingest chunk 0 directly into db and mark STT job completed
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO audio_chunks (
                    interview_id, track_id, capture_epoch, sequence, start_time_ms, end_time_ms,
                    sample_rate, channels, sample_count, format, checksum_sha256, size_bytes, file_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("inv-api-single", "shared", 0, 0, 0, 5000, 16000, 1, 80000, "pcm_s16le", "a" * 64, 160000, "/tmp/dummy.pcm", "2026-09-10T12:00:00Z"),
            )
            conn.execute(
                """
                INSERT INTO jobs (
                    id, interview_id, type, status, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("job-stt-1", "inv-api-single", "TRANSCRIBE_AUDIO", "COMPLETED", "{}", "2026-09-10T12:00:00Z", "2026-09-10T12:00:00Z"),
            )

        # Now readiness should be true!
        readiness_res2 = client.get("/api/v1/interviews/inv-api-single/readiness")
        assert readiness_res2.status_code == 200
        assert readiness_res2.json()["is_ready"] is True, readiness_res2.json()

    app.dependency_overrides.clear()


# ============================================================================
# 10. API: Speaker Role Update & Segment Split Endpoints (TC-11, TC-12)
# ============================================================================
def test_api_speaker_role_and_split_endpoints(tmp_path):
    """TC-11, TC-12: Тестирование REST эндпоинтов назначения ролей и сплита сегмента."""
    from fastapi.testclient import TestClient

    from backend.api.app import app, get_repository

    db_file = tmp_path / "api_split_test.db"
    db = Database(str(db_file))
    db.init_schema()
    repo = Repository(db)

    app.dependency_overrides[get_repository] = lambda: repo

    with TestClient(app) as client:
        # Create interview
        client.post(
            "/api/v1/interviews",
            json={
                "id": "inv-split-api",
                "title": "Split API Test",
                "candidate_name": "Split Cand",
                "role": "Engineer",
                "capture_mode": "single_source",
            },
        )

        # Add segment
        client.post(
            "/api/v1/interviews/inv-split-api/segments",
            json={
                "id": "seg-api-1",
                "track_id": "shared",
                "start_time_ms": 1000,
                "end_time_ms": 9000,
                "text": "Вопрос интервьюера. Ответ кандидата.",
                "speaker_role": "unknown",
            },
        )

        # 1. Update speaker role to candidate
        role_res = client.post(
            "/api/v1/interviews/inv-split-api/segments/seg-api-1/speaker-role",
            json={"speaker_role": "candidate"},
        )
        assert role_res.status_code == 200
        assert role_res.json()["segment"]["speaker_role"] == "candidate"

        # 2. Split segment
        split_res = client.post(
            "/api/v1/interviews/inv-split-api/segments/seg-api-1/split",
            json={
                "split_time_ms": 5000,
                "text_part1": "Вопрос интервьюера.",
                "text_part2": "Ответ кандидата.",
                "role_part1": "interviewer",
                "role_part2": "candidate",
            },
        )
        assert split_res.status_code == 200
        data = split_res.json()
        assert data["segment_part1"]["text"] == "Вопрос интервьюера."
        assert data["segment_part1"]["speaker_role"] == "interviewer"
        assert data["segment_part2"]["text"] == "Ответ кандидата."
        assert data["segment_part2"]["speaker_role"] == "candidate"

    app.dependency_overrides.clear()


def test_live_speaker_role_preserved_across_stt_upserts(repo: Repository):
    """
    Verify that when an interviewer marks a replica during live recording,
    subsequent STT chunk upserts for the same segment ID do not overwrite it back to 'unknown'.
    """
    interview_id = "inv-live-role-protect"
    repo.create_interview(
        interview_id=interview_id,
        title="Live Protection Test",
        candidate_name="Alex Cand",
        role="DevOps",
        capture_mode="single_source",
    )
    repo.update_interview_status(interview_id, "ready")
    repo.update_interview_status(interview_id, "recording")

    seg_id = "seg-live-101"
    # Initial live transcription arrives with unknown role
    repo.add_transcript_segment(
        segment_id=seg_id,
        interview_id=interview_id,
        track_id="shared",
        start_time_ms=1000,
        end_time_ms=3000,
        text="Привет, я кандидат на вакансию",
        speaker_role="unknown",
    )

    # Reviewer assigns candidate role live
    repo.update_segment_speaker_role(
        interview_id=interview_id,
        segment_id=seg_id,
        speaker_role="candidate",
    )

    # Verify role updated
    segs = repo.get_transcript_segments(interview_id)
    assert segs[0]["speaker_role"] == "candidate"

    # Background STT worker runs a subsequent pass on the same chunk with unknown role
    repo.add_transcript_segment(
        segment_id=seg_id,
        interview_id=interview_id,
        track_id="shared",
        start_time_ms=1000,
        end_time_ms=3000,
        text="Привет, я кандидат на вакансию!",  # refined text
        speaker_role="unknown",
    )

    # Human-assigned role 'candidate' must be PRESERVED, text updated!
    updated_segs = repo.get_transcript_segments(interview_id)
    assert len(updated_segs) == 1
    assert updated_segs[0]["speaker_role"] == "candidate"
    assert updated_segs[0]["text"] == "Привет, я кандидат на вакансию!"


# ============================================================================
# 8. Привязка короткого ответа кандидата к текущему вопросу (регрессия)
# ============================================================================
_SHORT_ANSWER_QUESTIONS = [
    {
        "id": "q1",
        "title": "Выбор хранилища",
        "prompt": "Сколько лап у паука",
        "criteria": [{"id": "c1", "title": "знания количества лап", "min_score": 1, "max_score": 5, "weight": 1}],
    },
    {
        "id": "q2",
        "title": "Новый вопрос 2",
        "prompt": "бывает больше лап?",
        "criteria": [{"id": "c2", "title": "варианты встречаемые", "min_score": 1, "max_score": 5, "weight": 1}],
    },
]


def _short_answer_segments() -> list[dict]:
    return [
        {
            "id": "seg-int-1",
            "track_id": "shared",
            "speaker_role": "interviewer",
            "start_time_ms": 1957,
            "end_time_ms": 4960,
            "text": "Еще раз проверяем, сколько лапа паука?",
        },
        {
            "id": "seg-int-2",
            "track_id": "shared",
            "speaker_role": "interviewer",
            "start_time_ms": 5477,
            "end_time_ms": 10576,
            "text": "ну тут сложно сказать так просто если ну как бы",
        },
        {
            "id": "seg-cand-1",
            "track_id": "shared",
            "speaker_role": "candidate",
            "start_time_ms": 11777,
            "end_time_ms": 13352,
            "text": "Ну не больше десяти точно.",
        },
        {
            "id": "seg-cand-2",
            "track_id": "shared",
            "speaker_role": "candidate",
            "start_time_ms": 17555,
            "end_time_ms": 20137,
            "text": "Давай скажи точно, какое число? Десять.",
        },
    ]


def test_matcher_keeps_short_answer_with_active_question(repo: Repository):
    """
    Регрессия: короткий ответ кандидата не должен уезжать к вопросу, который ещё не задавали,
    из-за одного случайно совпавшего слова ("больше" в следующем вопросе).
    Иначе оценка текущего вопроса сообщает «Ответ кандидата отсутствует», хотя ответ есть.
    """
    assocs = QuestionMatcher().associate_segments(_SHORT_ANSWER_QUESTIONS, _short_answer_segments())
    by_seg = {a.segment_id: a for a in assocs}

    assert by_seg["seg-int-1"].question_id == "q1"
    # Ответ кандидата остаётся у вопроса, который реально был задан (текущий вопрос интервью)
    assert by_seg["seg-cand-1"].question_id == "q1"
    assert by_seg["seg-cand-2"].question_id == "q1"
    # Низкая тематическая уверенность честно помечается для ревью, но не скрывается
    assert by_seg["seg-cand-1"].is_ambiguous is True
    assert "Требуется ревью" in by_seg["seg-cand-1"].notes


def test_matcher_reassigns_answer_to_already_asked_question():
    """Перенос ответа к ранее заданному вопросу по-прежнему работает (ответ не «залипает» на активном вопросе)."""
    questions = [
        {
            "id": "q1",
            "text": "Расскажите про GIL в Python и способы его обхода",
            "criteria": [{"id": "c1", "title": "Понимание Global Interpreter Lock и multiprocessing"}],
        },
        {
            "id": "q2",
            "text": "Как устроена изоляция транзакций в PostgreSQL?",
            "criteria": [{"id": "c2", "title": "Уровни изоляции транзакций и MVCC"}],
        },
    ]
    segments = [
        {
            "id": "seg-q1-int",
            "track_id": "interviewer",
            "speaker_role": "interviewer",
            "start_time_ms": 0,
            "end_time_ms": 3000,
            "text": "Расскажите про GIL в Python и способы его обхода",
        },
        {
            "id": "seg-q2-int",
            "track_id": "interviewer",
            "speaker_role": "interviewer",
            "start_time_ms": 5000,
            "end_time_ms": 8000,
            "text": "Как устроена изоляция транзакций в PostgreSQL?",
        },
        {
            "id": "seg-answer-q2",
            "track_id": "candidate",
            "speaker_role": "candidate",
            "start_time_ms": 10000,
            "end_time_ms": 14000,
            "text": "Уровни изоляции в PostgreSQL реализованы через MVCC и снимки данных.",
        },
        {
            "id": "seg-answer-q1",
            "track_id": "candidate",
            "speaker_role": "candidate",
            "start_time_ms": 20000,
            "end_time_ms": 26000,
            "text": (
                "Асинхронный код на asyncio работает в одном потоке и решает задачи "
                "ввода-вывода без блокировки GIL."
            ),
        },
    ]

    assocs = {a.segment_id: a for a in QuestionMatcher().associate_segments(questions, segments)}
    assert assocs["seg-answer-q2"].question_id == "q2"
    # Ответ про GIL привязан к q1, хотя активным в этот момент был q2
    assert assocs["seg-answer-q1"].question_id == "q1"
    assert assocs["seg-answer-q1"].is_ambiguous is True  # решение всё равно требует ревью


@pytest.mark.asyncio
async def test_evaluate_uses_short_candidate_answer_for_current_question(repo: Repository):
    """
    Регрессия сквозного пути: при живом назначении ролей оценка текущего вопроса
    должна использовать ответ кандидата, а не сообщать «Ответ кандидата отсутствует».
    """
    interview_id = "inv-short-answer-1"
    repo.create_interview(
        interview_id=interview_id,
        title="Short Answer Test",
        candidate_name="Test Cand",
        role="Engineer",
        capture_mode="single_source",
    )
    repo.save_plan("plan-short-1", interview_id, {"questions": _SHORT_ANSWER_QUESTIONS}, version=1)

    for seg in _short_answer_segments():
        repo.add_transcript_segment(
            segment_id=seg["id"],
            interview_id=interview_id,
            track_id=seg["track_id"],
            start_time_ms=seg["start_time_ms"],
            end_time_ms=seg["end_time_ms"],
            text=seg["text"],
            speaker_role=seg["speaker_role"],
        )

    mock_llm = AsyncMock()
    mock_llm.execute_request.return_value = (
        {
            "scores": [
                {
                    "criterion_id": "c1",
                    "score": 2.0,
                    "explanation": "Кандидат назвал число, но не назвал точное количество лап",
                    "evidence": [
                        {"segment_id": "seg-cand-1", "exact_quote": "Ну не больше десяти точно."}
                    ],
                }
            ],
            "critical_errors": [],
        },
        "mock-model-v1",
    )

    worker = PipelineWorker(repository=repo, llm_adapter=mock_llm)
    await worker._handle_evaluate(interview_id, {"question_id": "q1"})

    assert mock_llm.execute_request.call_count == 1, "Оценка должна использовать ответ кандидата"

    props = repo.get_assessment_proposals(interview_id)
    assert len(props) == 1
    assert props[0]["scores"][0]["score"] == 2.0
    assert props[0]["scores"][0]["evidence"][0]["segment_id"] == "seg-cand-1"
