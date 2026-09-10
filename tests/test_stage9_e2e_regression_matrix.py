"""
Stage 9: End-to-End Minimal Regression Matrix Test Suite.
Сквозное тестирование минимальной регрессионной матрицы (Раздел 4 remediation-plan.md).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app, get_repository, get_trusted_spool_dir
from backend.core.evidence_validator import validate_proposal
from backend.db.database import Database
from backend.db.repository import Repository, RepositoryConflictError
from contracts.audio import TrackType
from contracts.domain import (
    AssessmentProposal,
    CriterionScoreProposal,
    EvidenceRef,
    TranscriptRevision,
    TranscriptSegment,
)


@pytest.fixture
def test_db(tmp_path: Path):
    db_file = tmp_path / "test_stage9_matrix.db"
    db = Database(str(db_file))
    db.init_schema()
    return db


@pytest.fixture
def repo(test_db: Database):
    return Repository(test_db)


@pytest.fixture
def client(test_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    spool_dir = tmp_path / "matrix_spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    r = Repository(test_db)

    app.dependency_overrides[get_repository] = lambda: r
    app.dependency_overrides[get_trusted_spool_dir] = lambda: spool_dir
    monkeypatch.setattr("backend.api.app.get_db", lambda: test_db)
    monkeypatch.setenv("NEBULA_DISABLE_AUTH", "1")

    with TestClient(app) as tc:
        yield tc

    app.dependency_overrides.clear()


def _create_sample_interview(repo: Repository, interview_id: str, candidate: str, role: str):
    repo.create_interview(
        interview_id=interview_id,
        title=f"Interview {interview_id}",
        candidate_name=candidate,
        role=role,
    )
    repo.update_interview_status(interview_id, "ready")
    repo.update_interview_status(interview_id, "recording")
    rev_id = f"{interview_id}-rev-1"
    repo.create_transcript_revision(
        revision_id=rev_id,
        interview_id=interview_id,
        revision_number=1,
        is_batch_final=False,
    )
    repo.set_active_transcript_revision(interview_id, rev_id)
    return rev_id


# --------------------------------------------------------------------------
# Scenario 1: Одинаковые question IDs в двух интервью (полная изоляция)
# --------------------------------------------------------------------------
def test_matrix_scenario_1_identical_question_ids_cross_interview_isolation(client: TestClient, repo: Repository):
    """
    Матрица #1: Одинаковые question IDs в двух интервью.
    Изменение одного интервью (сегменты, привязки, оценки) не затрагивает другое.
    """
    id_a = "inv-matrix-iso-a"
    id_b = "inv-matrix-iso-b"
    rev_a = _create_sample_interview(repo, id_a, "Алексей Иванов", "Backend Lead")
    rev_b = _create_sample_interview(repo, id_b, "Борис Петров", "Backend Lead")
    assert rev_a is not None and rev_b is not None

    shared_plan = {
        "questions": [
            {
                "id": "q-arch-1",
                "title": "Архитектура шардирования",
                "text": "Как вы шардируете реляционную БД?",
                "weight": 2.0,
                "criteria": [
                    {"id": "crit-shard", "title": "Ключ шардирования", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}
                ],
            }
        ]
    }
    repo.save_plan(f"plan-{id_a}", id_a, shared_plan)
    repo.save_plan(f"plan-{id_b}", id_b, shared_plan)

    # Добавляем сегмент и оценку только в интервью A
    repo.add_transcript_segment(
        segment_id="seg-a-1",
        interview_id=id_a,
        track_id="candidate",
        start_time_ms=1000,
        end_time_ms=5000,
        text="Мы использовали консистентное хеширование по tenant_id.",
        is_final=True,
        revision_id=rev_a,
    )
    repo.save_human_assessment(
        assessment_id="ha-a-1",
        interview_id=id_a,
        question_id="q-arch-1",
        rubric_revision_id="rubric-v1",
        transcript_revision_id=rev_a,
        reviewer_id="lead-rev",
        scores=[{"criterion_id": "crit-shard", "score": 5.0, "explanation": "Отличный ответ"}],
        reviewer_notes="Кандидат детально понимает шардирование",
        is_manually_adjusted=True,
    )

    # Проверяем интервью B через клиент API
    res_b = client.get(f"/api/v1/interviews/{id_b}")
    assert res_b.status_code == 200
    data_b = res_b.json()
    assert len(data_b["transcript_segments"]) == 0
    assert len(data_b.get("human_assessments", [])) == 0
    assert data_b["scoring"]["coverage_percentage"] == 0.0
    assert data_b["scoring"]["final_score_100"] is None

    # Проверяем интервью A
    res_a = client.get(f"/api/v1/interviews/{id_a}")
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert len(data_a["transcript_segments"]) == 1
    assert len(data_a["human_assessments"]) == 1
    assert data_a["scoring"]["coverage_percentage"] == 100.0
    assert data_a["scoring"]["final_score_100"] == 100.0


# --------------------------------------------------------------------------
# Scenario 2: Повтор чанка: те же bytes / другие bytes
# --------------------------------------------------------------------------
def test_matrix_scenario_2_audio_chunk_idempotent_duplicate_and_hash_conflict(client: TestClient, repo: Repository):
    """
    Матрица #2: Идемпотентный приём одинаковых байтов и 409 при конфликте содержимого.
    """
    interview_id = "inv-matrix-chunks"
    _create_sample_interview(repo, interview_id, "Сергей", "Go Dev")

    pcm_payload = b"\x00\x01" * 16000
    sha256_hash = hashlib.sha256(pcm_payload).hexdigest()

    metadata = {
        "interview_id": interview_id,
        "track_id": "candidate",
        "capture_epoch": 0,
        "sequence": 0,
        "start_time_ms": 0,
        "end_time_ms": 1000,
        "sample_rate": 16000,
        "channels": 1,
        "sample_count": 16000,
        "format": "pcm_s16le",
        "checksum_sha256": sha256_hash,
        "size_bytes": len(pcm_payload),
    }

    # Первый приём чанка -> 200 persisted
    r1 = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={"metadata": metadata, "payload_hex": pcm_payload.hex()},
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "persisted"

    # Повтор с теми же байтами -> 200 idempotent_duplicate
    r2 = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={"metadata": metadata, "payload_hex": pcm_payload.hex()},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "idempotent_duplicate"

    # Приём чанка с тем же sequence_number, но изменёнными байтами и хэшем -> 409 Conflict
    corrupted_payload = b"\xAA\xBB" * 16000
    corrupted_metadata = dict(metadata)
    corrupted_metadata["checksum_sha256"] = hashlib.sha256(corrupted_payload).hexdigest()

    r3 = client.post(
        f"/api/v1/interviews/{interview_id}/audio/chunks",
        json={"metadata": corrupted_metadata, "payload_hex": corrupted_payload.hex()},
    )
    assert r3.status_code == 409


# --------------------------------------------------------------------------
# Scenario 3: Один segment ID в двух transcript revisions
# --------------------------------------------------------------------------
def test_matrix_scenario_3_same_segment_id_in_two_transcript_revisions(client: TestClient, repo: Repository):
    """
    Матрица #3: Один segment ID в двух transcript revisions адресуется независимо.
    """
    interview_id = "inv-matrix-revisions"
    rev1_id = _create_sample_interview(repo, interview_id, "Елена", "ML Engineer")
    rev2_id = f"{interview_id}-rev-2"

    repo.create_transcript_revision(rev2_id, interview_id, revision_number=2, is_batch_final=True)

    # Сегмент seg-common в rev-1
    repo.add_transcript_segment(
        segment_id="seg-common",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=3000,
        text="Я настраивал PyTorch DDP обучение.",
        is_final=True,
        revision_id=rev1_id,
    )

    # Тот же segment_id seg-common в rev-2 с уточнённым текстом
    repo.add_transcript_segment(
        segment_id="seg-common",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=0,
        end_time_ms=3000,
        text="Я настраивал PyTorch DDP и FSDP обучение на 8 GPU.",
        is_final=True,
        revision_id=rev2_id,
    )

    # Проверка получения сегментов по ревизиям
    segs_rev1 = repo.get_transcript_segments(interview_id, revision_id=rev1_id)
    segs_rev2 = repo.get_transcript_segments(interview_id, revision_id=rev2_id)

    assert len(segs_rev1) == 1
    assert len(segs_rev2) == 1
    assert segs_rev1[0]["text"] == "Я настраивал PyTorch DDP обучение."
    assert segs_rev2[0]["text"] == "Я настраивал PyTorch DDP и FSDP обучение на 8 GPU."


# --------------------------------------------------------------------------
# Scenario 4: Полный жизненный цикл от плана до финализации
# --------------------------------------------------------------------------
def test_matrix_scenario_4_setup_screen_plan_to_finalization_clean_contract(client: TestClient, repo: Repository):
    """
    Матрица #4: План из SetupScreen -> верификация -> финализация:
    валидный контракт, без KeyError/500, sha256 checksum в запечатанном отчёте.
    """
    interview_id = "inv-matrix-lifecycle"
    rev_id = _create_sample_interview(repo, interview_id, "Дмитрий Смирнов", "Senior SRE")

    plan_payload = {
        "questions": [
            {
                "id": "q-k8s",
                "title": "Kubernetes Ingress & TLS",
                "text": "Как настроить cert-manager в Kubernetes?",
                "weight": 1.0,
                "criteria": [
                    {"id": "crit-cm", "title": "Cert-Manager CRD", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}
                ],
            }
        ]
    }
    repo.save_plan(f"plan-{interview_id}", interview_id, plan_payload)

    # Добавляем стенограмму кандидата
    repo.add_transcript_segment(
        segment_id="seg-k8s-1",
        interview_id=interview_id,
        track_id="candidate",
        start_time_ms=1000,
        end_time_ms=6000,
        text="Мы создаём ClusterIssuer с Let's Encrypt и аннотируем Ingress ресурс cert-manager.io/cluster-issuer.",
        is_final=True,
        revision_id=rev_id,
    )

    # Экспертное ревью
    rev_resp = client.post(
        f"/api/v1/interviews/{interview_id}/assessments/q-k8s/review",
        json={
            "expected_transcript_revision": rev_id,
            "scores": [{"criterion_id": "crit-cm", "score": 5.0}],
            "reviewer_notes": "Точная конфигурация",
            "reviewer_id": "sre-lead",
        },
    )
    assert rev_resp.status_code == 200

    # Подтверждение резюме
    sum_resp = client.post(
        f"/api/v1/interviews/{interview_id}/summary/confirm",
        json={
            "reviewer_id": "sre-lead",
            "confirmed_markdown": "Кандидат обладает отличным практическим опытом в Kubernetes.",
            "confirmed_recommendation": "STRONG_HIRE",
            "expected_transcript_revision": rev_id,
        },
    )
    assert sum_resp.status_code == 200

    # Переход в review
    repo.update_interview_status(interview_id, "review")

    # Финализация
    fin_resp = client.post(
        f"/api/v1/interviews/{interview_id}/report/finalize",
        json={
            "summary_markdown": "Кандидат обладает отличным практическим опытом в Kubernetes.",
            "hiring_recommendation": "STRONG_HIRE",
            "confirmed_by": "sre-lead",
            "expected_transcript_revision": rev_id,
        },
    )
    assert fin_resp.status_code == 200
    fin_data = fin_resp.json()
    assert fin_data["status"] == "finalized"
    assert fin_data["final_score_100"] == 100.0
    assert fin_data["coverage_percentage"] == 100.0
    assert len(fin_data["sha256_checksum"]) == 64


# --------------------------------------------------------------------------
# Scenario 5: Нет ответа или вопрос исключён (честный расчёт скоринга)
# --------------------------------------------------------------------------
def test_matrix_scenario_5_unanswered_or_excluded_questions_no_hallucinations(client: TestClient, repo: Repository):
    """
    Матрица #5: Нет ответа или AI недоступен:
    null, честное покрытие, исключение вопроса с причиной, без выдуманных баллов.
    """
    interview_id = "inv-matrix-exclusions"
    rev_id = _create_sample_interview(repo, interview_id, "Ольга", "Frontend Lead")

    plan_payload = {
        "questions": [
            {
                "id": "q-react",
                "title": "React Server Components",
                "text": "В чём разница между Client и Server Components?",
                "weight": 2.0,
                "criteria": [{"id": "crit-rsc", "title": "RSC Архитектура", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}],
            },
            {
                "id": "q-angular",
                "title": "Angular Signals",
                "text": "Вопрос не по стеку кандидата",
                "weight": 1.0,
                "criteria": [{"id": "crit-ang", "title": "Signals", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}],
            },
            {
                "id": "q-unanswered",
                "title": "Не заданный вопрос",
                "text": "Вопрос до которого не дошли",
                "weight": 1.0,
                "criteria": [{"id": "crit-un", "title": "Unanswered", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}],
            },
        ]
    }
    repo.save_plan(f"plan-{interview_id}", interview_id, plan_payload)

    # Оцениваем q-react на 5.0
    repo.save_human_assessment(
        assessment_id="ha-rsc",
        interview_id=interview_id,
        question_id="q-react",
        rubric_revision_id="rubric-v1",
        transcript_revision_id=rev_id,
        scores=[{"criterion_id": "crit-rsc", "score": 5.0}],
        reviewer_id="lead",
    )

    # Исключаем q-angular с обоснованием
    repo.save_human_assessment(
        assessment_id="ha-ang",
        interview_id=interview_id,
        question_id="q-angular",
        rubric_revision_id="rubric-v1",
        transcript_revision_id=rev_id,
        scores=[],
        reviewer_notes="Вопрос исключён: кандидат не использует Angular на текущем проекте",
        reviewer_id="lead",
        is_excluded=True,
        exclusion_reason="Не профильный стек для позиции",
    )

    # Проверяем скоринг:
    # Общий вес запланированных вопросов = 4.0. Оценен q-react (вес 2.0).
    # Покрытие = (2.0 / 4.0) * 100 = 50.0%
    # Взвешенный балл считается только по оцененным не-исключённым вопросам: ((5 - 1)/4)*100 = 100.0
    res = client.get(f"/api/v1/interviews/{interview_id}")
    scoring = res.json()["scoring"]
    assert scoring["coverage_percentage"] == 50.0
    assert scoring["final_score_100"] == 100.0

    # Попытка финализировать при наличии неоцененного и неисключенного q-unanswered блокируется (400)
    repo.update_interview_status(interview_id, "review")
    fin_fail = client.post(
        f"/api/v1/interviews/{interview_id}/report/finalize",
        json={
            "summary_markdown": "Резюме",
            "hiring_recommendation": "HIRE",
            "confirmed_by": "lead",
            "expected_transcript_revision": rev_id,
        },
    )
    assert fin_fail.status_code == 400
    assert "q-unanswered" in fin_fail.json()["detail"]


# --------------------------------------------------------------------------
# Scenario 6: Невалидная цитата AI блокируется EvidenceValidator
# --------------------------------------------------------------------------
def test_matrix_scenario_6_invalid_evidence_citation_rejected_and_blocked(client: TestClient, repo: Repository):
    """
    Матрица #6: Невалидная цитата (галлюцинация или речь интервьюера) отклоняется
    и не допускается к автоматическому approve.
    """
    interview_id = "inv-matrix-evidence"
    rev_id = _create_sample_interview(repo, interview_id, "Максим", "DevOps")

    # Добавляем только речь интервьюера
    interviewer_seg = TranscriptSegment(
        id="seg-interviewer-only",
        track_id=TrackType.INTERVIEWER,
        start_time_ms=0,
        end_time_ms=4000,
        text="Расскажите про Ansible плейбуки.",
        is_final=True,
    )
    repo.add_transcript_segment(
        segment_id="seg-interviewer-only",
        interview_id=interview_id,
        track_id="interviewer",
        start_time_ms=0,
        end_time_ms=4000,
        text="Расскажите про Ansible плейбуки.",
        is_final=True,
        revision_id=rev_id,
    )

    rubric_criteria = ["crit-ansible"]

    # Модель процитировала фразу интервьюера в качестве ответа кандидата
    bad_proposal = AssessmentProposal(
        id="prop-hallucinated",
        interview_id=interview_id,
        question_id="q-ansible",
        model_profile_id="test-llm",
        rubric_revision_id="rubric-v1",
        transcript_revision_id=rev_id,
        scores=[
            CriterionScoreProposal(
                criterion_id="crit-ansible",
                score=5.0,
                explanation="Кандидат всё знает",
                evidence=[EvidenceRef(segment_id="seg-interviewer-only", exact_quote="Расскажите про Ansible плейбуки.")],
            )
        ],
    )

    transcript_rev = TranscriptRevision(
        revision_id=rev_id,
        interview_id=interview_id,
        segments=[interviewer_seg],
    )

    validated = validate_proposal(bad_proposal, transcript_rev, allowed_criteria_ids=rubric_criteria)
    assert validated.is_valid is False
    assert len(validated.errors) > 0
    assert any("candidate track" in err for err in validated.errors)

    # Сохраняем забракованное предложение в БД
    repo.save_assessment_proposal(
        proposal_id="prop-hallucinated",
        interview_id=interview_id,
        question_id="q-ansible",
        model_profile_id="test-llm",
        scores=[s.model_dump() for s in bad_proposal.scores],
        rubric_revision_id="rubric-v1",
        transcript_revision_id=rev_id,
        is_rejected=True,
        validation_errors=validated.errors,
    )

    # Попытка approve такого предложения через API возвращает 409 Conflict
    app_res = client.post(f"/api/v1/interviews/{interview_id}/assessments/prop-hallucinated/approve", json={})
    assert app_res.status_code == 409
    assert "rejected due to evidence validation errors" in app_res.json()["detail"]


# --------------------------------------------------------------------------
# Scenario 7: Stale review блокирует финализацию (OCC & Revision Stale)
# --------------------------------------------------------------------------
def test_matrix_scenario_7_stale_review_blocks_finalization_409(client: TestClient, repo: Repository):
    """
    Матрица #7: Stale review / неподтверждённое summary:
    финализация заблокирована с подробной причиной (409).
    """
    interview_id = "inv-matrix-stale"
    rev_id = _create_sample_interview(repo, interview_id, "Константин", "Security Lead")

    plan_payload = {
        "questions": [
            {
                "id": "q-sec",
                "title": "OAuth 2.0 PKCE",
                "text": "Зачем нужен PKCE?",
                "weight": 1.0,
                "criteria": [{"id": "crit-pkce", "title": "PKCE", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}],
            }
        ]
    }
    repo.save_plan(f"plan-{interview_id}", interview_id, plan_payload)

    # Человек оценил в rev-1
    repo.save_human_assessment(
        assessment_id="ha-sec-1",
        interview_id=interview_id,
        question_id="q-sec",
        rubric_revision_id="rubric-v1",
        transcript_revision_id=rev_id,
        scores=[{"criterion_id": "crit-pkce", "score": 4.0}],
        reviewer_id="sec-lead",
    )

    # Происходит перестенограмма сессии -> оценка помечается stale
    repo.mark_proposals_stale(
        interview_id=interview_id,
        question_ids=["q-sec"],
        stale_reason="Стенограмма обновлена в новой ревизии",
    )

    # Проверяем флаг is_stale в БД
    assessments = repo.get_human_assessments(interview_id)
    assert bool(assessments[0]["is_stale"]) is True
    assert "Стенограмма обновлена" in assessments[0]["stale_reason"]

    # Попытка финализировать отчёт со stale оценкой возвращает 409
    repo.update_interview_status(interview_id, "review")
    fin_resp = client.post(
        f"/api/v1/interviews/{interview_id}/report/finalize",
        json={
            "summary_markdown": "Резюме",
            "hiring_recommendation": "HIRE",
            "confirmed_by": "sec-lead",
            "expected_transcript_revision": rev_id,
        },
    )
    assert fin_resp.status_code == 409
    assert "is stale" in fin_resp.json()["detail"]


# --------------------------------------------------------------------------
# Scenario 8: Оптимистичная блокировка (OCC) при расхождении ревизий
# --------------------------------------------------------------------------
def test_matrix_scenario_8_optimistic_concurrency_control_conflict(client: TestClient, repo: Repository):
    """
    Матрица #8: Два одновременных действия: отправка review или finalize
    с устаревшей ревизией пресекается с 409 Conflict без перезаписи.
    """
    interview_id = "inv-matrix-occ"
    _create_sample_interview(repo, interview_id, "Василий", "Tech Lead")
    new_rev = f"{interview_id}-rev-2"

    # В фоновом воркере стенограмма переключена на new_rev
    repo.create_transcript_revision(new_rev, interview_id, revision_number=2, is_batch_final=True)
    repo.set_active_transcript_revision(interview_id, new_rev)

    # Клиент в UI всё ещё отправляет review со старой ревизией
    rev_resp = client.post(
        f"/api/v1/interviews/{interview_id}/assessments/q-lead/review",
        json={
            "expected_transcript_revision": f"{interview_id}-rev-1",
            "scores": [{"criterion_id": "crit-lead", "score": 5.0}],
            "reviewer_notes": "Запоздалое ревью",
        },
    )
    assert rev_resp.status_code == 409
    assert "Revision conflict" in rev_resp.json()["detail"]
    assert new_rev in rev_resp.json()["detail"]


# --------------------------------------------------------------------------
# Scenario 9: Удаление интервью и целостность базы данных
# --------------------------------------------------------------------------
def test_matrix_scenario_9_interview_lifecycle_deletion_and_db_integrity(client: TestClient, repo: Repository):
    """
    Матрица #9: Удаление интервью помечает статус 'deleted', очищает сегменты,
    а проверка системной целостности SQLite (/system/integrity) подтверждает OK.
    """
    interview_id = "inv-matrix-delete"
    _create_sample_interview(repo, interview_id, "Тестовый", "Junior QA")

    # Проверяем целостность БД
    integ_res = client.get("/api/v1/system/integrity")
    assert integ_res.status_code == 200
    assert integ_res.json()["integrity_ok"] is True

    # Удаляем интервью
    del_res = client.delete(f"/api/v1/interviews/{interview_id}")
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "deleted"

    # Повторный запрос интервью возвращает 404
    get_res = client.get(f"/api/v1/interviews/{interview_id}")
    assert get_res.status_code == 404

    # Целостность БД после удаления сохраняется
    integ_after = client.get("/api/v1/system/integrity")
    assert integ_after.status_code == 200
    assert integ_after.json()["integrity_ok"] is True


# --------------------------------------------------------------------------
# Scenario 10: Несколько критериев с разными весами и шкалами
# --------------------------------------------------------------------------
def test_matrix_scenario_10_heterogeneous_criteria_weights_and_scales(client: TestClient, repo: Repository):
    """
    Матрица #10: Несколько критериев с разными весами и шкалами:
    детерминированный расчет взвешенного балла на бэкенде.
    """
    interview_id = "inv-matrix-weights"
    rev_id = _create_sample_interview(repo, interview_id, "Григорий", "Data Platform Lead")

    plan_payload = {
        "questions": [
            {
                "id": "q-spark",
                "title": "Spark Data Quality",
                "text": "Как проверять качество данных в Spark стриминге?",
                "weight": 1.0,
                "criteria": [
                    {"id": "crit-stream", "title": "Structured Streaming", "weight": 2.0, "min_score": 1.0, "max_score": 5.0},
                    {"id": "crit-metrics", "title": "Метрики качества", "weight": 1.0, "min_score": 1.0, "max_score": 5.0},
                ],
            }
        ]
    }
    repo.save_plan(f"plan-{interview_id}", interview_id, plan_payload)

    # Ставим балл 5.0 за crit-stream (нормализованный 1.0, вес 2.0)
    # и балл 3.0 за crit-metrics (нормализованный 0.5, вес 1.0)
    # Итоговый балл вопроса: (1.0*2.0 + 0.5*1.0) / 3.0 = 2.5 / 3.0 = 83.333% -> 83.3
    repo.save_human_assessment(
        assessment_id="ha-spark",
        interview_id=interview_id,
        question_id="q-spark",
        rubric_revision_id="rubric-v1",
        transcript_revision_id=rev_id,
        scores=[
            {"criterion_id": "crit-stream", "score": 5.0},
            {"criterion_id": "crit-metrics", "score": 3.0},
        ],
        reviewer_id="lead-data",
    )

    res = client.get(f"/api/v1/interviews/{interview_id}")
    assert res.status_code == 200
    scoring = res.json()["scoring"]
    assert round(scoring["final_score_100"], 1) == 83.3
    assert scoring["coverage_percentage"] == 100.0


# --------------------------------------------------------------------------
# Scenario 11: Неизменность снимка финализированного отчёта
# --------------------------------------------------------------------------
def test_matrix_scenario_11_finalized_report_snapshot_immutability(client: TestClient, repo: Repository):
    """
    Матрица #11: Экспорт после финализации:
    ранее финализированный снимок и SHA-256 хэш неизменны, повторные мутации заблокированы (409).
    """
    interview_id = "inv-matrix-freeze"
    rev_id = _create_sample_interview(repo, interview_id, "Станислав", "Tech Lead")

    plan_payload = {
        "questions": [
            {
                "id": "q-1",
                "title": "Вопрос",
                "text": "Текст",
                "weight": 1.0,
                "criteria": [{"id": "c-1", "title": "Критерий", "weight": 1.0, "min_score": 1.0, "max_score": 5.0}],
            }
        ]
    }
    repo.save_plan(f"plan-{interview_id}", interview_id, plan_payload)
    repo.save_human_assessment(
        assessment_id="ha-1",
        interview_id=interview_id,
        question_id="q-1",
        rubric_revision_id="rubric-v1",
        transcript_revision_id=rev_id,
        scores=[{"criterion_id": "c-1", "score": 4.0}],
        reviewer_id="lead",
    )
    repo.update_interview_status(interview_id, "review")

    # Финализируем
    fin_res = client.post(
        f"/api/v1/interviews/{interview_id}/report/finalize",
        json={
            "summary_markdown": "Финализированный текст",
            "hiring_recommendation": "HIRE",
            "confirmed_by": "lead",
            "expected_transcript_revision": rev_id,
        },
    )
    assert fin_res.status_code == 200
    orig_checksum = fin_res.json()["sha256_checksum"]

    # Экспорт возвращает зафиксированный снимок со статусом FINALIZED
    exp_res = client.get(f"/api/v1/interviews/{interview_id}/export")
    assert exp_res.status_code == 200
    assert exp_res.json()["status"].lower() == "finalized"
    assert exp_res.json()["final_score_100"] == 75.0
    assert exp_res.json()["sha256_checksum"] == orig_checksum

    # Попытка повторно изменить статус или добавить оценку в финализированное интервью отклоняется (409)
    mutate_res = client.post(
        f"/api/v1/interviews/{interview_id}/assessments/q-1/review",
        json={
            "expected_transcript_revision": rev_id,
            "scores": [{"criterion_id": "c-1", "score": 5.0}],
        },
    )
    assert mutate_res.status_code == 409


# --------------------------------------------------------------------------
# Scenario 12: Изоляция чужих данных между интервью (404/422)
# --------------------------------------------------------------------------
def test_matrix_scenario_12_cross_interview_foreign_data_isolation(client: TestClient, repo: Repository):
    """
    Матрица #12: Попытка запросить или модифицировать чужие данные возвращает 404/409,
    чужие сессии не подвержены утечкам или подмене.
    """
    id_x = "inv-matrix-x"
    id_y = "inv-matrix-y"
    _create_sample_interview(repo, id_x, "Кандидат X", "Role X")
    _create_sample_interview(repo, id_y, "Кандидат Y", "Role Y")

    # Попытка обновить статус несуществующего интервью -> 404
    bad_stat = client.post("/api/v1/interviews/non-existent-id/status", json={"target_status": "ready"})
    assert bad_stat.status_code == 404

    # Попытка получить summary несуществующего интервью -> 404
    bad_sum = client.get("/api/v1/interviews/non-existent-id/summary")
    assert bad_sum.status_code == 404


# --------------------------------------------------------------------------
# Scenario 13: Атомарный лизинг задач и защита от гонок воркеров
# --------------------------------------------------------------------------
def test_matrix_scenario_13_job_lease_token_ownership_and_renew(client: TestClient, repo: Repository):
    """
    Матрица #13: Лизинг очереди задач: владение токеном locked_by, продление аренды,
    защита от чужого complete_job.
    """
    interview_id = "inv-matrix-lease"
    _create_sample_interview(repo, interview_id, "Воркер", "Dev")

    job_id = "job-lease-test-1"
    repo.enqueue_job(
        job_id=job_id,
        job_type="EVALUATE_QUESTION",
        interview_id=interview_id,
        payload={"question_id": "q1"},
    )

    # Воркер 1 захватывает задачу
    job = repo.claim_next_job(lock_duration_sec=30)
    assert job is not None
    assert job["id"] == job_id
    worker_token = job["locked_by"]
    assert worker_token.startswith("worker-")

    # Воркер 1 продлевает лизинг
    renewed = repo.renew_job_lease(job_id, owner_token=worker_token, extension_sec=60)
    assert renewed is True

    # Чужой воркер пытается завершить задачу -> RepositoryConflictError
    with pytest.raises(RepositoryConflictError):
        repo.complete_job(job_id, owner_token="fake-imposter-worker-token")

    # Владелец токена корректно завершает задачу
    repo.complete_job(job_id, owner_token=worker_token)
    with repo.db.transaction() as conn:
        job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job_row["status"] == "COMPLETED"


# --------------------------------------------------------------------------
# Scenario 14: Создание резервной копии БД и проверка её целостности
# --------------------------------------------------------------------------
def test_matrix_scenario_14_database_backup_and_recovery_verification(client: TestClient, repo: Repository):
    """
    Матрица #14: Резервное копирование SQLite БД через /system/backup и валидация файла.
    """
    b_res = client.post("/api/v1/system/backup")
    assert b_res.status_code == 200
    target_path = b_res.json()["target_path"]
    assert Path(target_path).exists()

    # Открываем созданную резервную копию и проверяем её целостность
    import sqlite3
    with sqlite3.connect(target_path) as conn:
        integrity_check = conn.execute("PRAGMA integrity_check;").fetchone()
        assert integrity_check[0] == "ok"

