# Спецификация: Этап 6 — Финальный контур обработки и управление ревизиями
# Specification: Stage 6 — Final Processing Contour & Revision Lifecycle

---

## 1. Обзор архитектуры / Architecture Overview

### Русский
Финальный контур обработки в Nebula обеспечивает пакетную повторную транскрибацию (Batch-STT), обнаружение расхождений в стенограмме, расчет диффов ревизий, защиту ручных решений человека от перезаписи AI, генерацию итогового резюме кандидата (Executive Summary) и криптографическое опечатывание финального протокола собеседования.

### English
The Final Processing Contour in Nebula provides batch re-transcription (Batch-STT), transcript discrepancy detection, revision diff computation, protection of human manual decisions from AI overwrites, generation of an executive summary, and cryptographic sealing of the final interview protocol.

---

## 2. Ключевые инварианты и правила согласованности / Key Invariants & Consistency Rules

### Русский
1. **Неприкосновенность ручных правок (Human Decision Protection)**:
   - Если интервьюер или рецензент утвердил или изменил балл вопроса (`is_manually_adjusted = True`), пакетная обработка и повторный запуск LLM **никогда не перезаписывают** человеческое решение.
   - Оценки человека хранятся в таблице `human_assessments`, отделенной от рекомендаций AI `assessment_proposals`.
2. **Явная инвалидация устаревших оценок (`is_stale = True`)**:
   - При выявлении смысловых расхождений в стенограмме между версиями (`trans-rev-1` и `trans-rev-2`), предложения и оценки по затронутым вопросам помечаются флагом `is_stale = True` с обязательным указанием `stale_reason`.
   - Автоматический перенос подтверждений на изменившийся текст строго запрещен.
3. **Защита от конфликтов параллельных правок (HTTP 409 Conflict)**:
   - Клиент обязан передавать параметр `expected_transcript_revision`.
   - Если активная ревизия в БД изменилась (например, завершился пакетный проход), запрос отклоняется со статусом `HTTP 409 Conflict`, требуя от пользователя обновить экран и ознакомиться с диффом.
4. **Криптографическое опечатывание (`ReportRevision`)**:
   - Финальный отчет фиксирует канонический снимок данных: итоговый взвешенный 100-балльный результат, процент покрытия вопросов, формулу, утвержденное текстовое резюме и подпись рецензента.
   - Вычисляется контрольная сумма SHA-256 по детерминированному JSON-представлению.
   - Последующие изменения требуют создания новой ревизии (`rep-rev-2`).

### English
1. **Human Decision Protection**:
   - If an interviewer or reviewer confirmed or overridden a question score (`is_manually_adjusted = True`), batch processing and re-scoring **never overwrite** the human decision.
   - Human decisions are recorded in `human_assessments`, decoupled from AI `assessment_proposals`.
2. **Explicit Stale Invalidation (`is_stale = True`)**:
   - When textual discrepancies between transcript revisions are detected, affected assessments are flagged as `is_stale = True` with a documented `stale_reason`.
   - Silent migration of approval onto altered underlying text is strictly prohibited.
3. **Concurrent Revision Conflict Prevention (HTTP 409 Conflict)**:
   - Clients must provide `expected_transcript_revision`.
   - If active database revision differs, the request fails with `HTTP 409 Conflict`, requiring the reviewer to inspect the transcript diff.
4. **Cryptographic Sealing (`ReportRevision`)**:
   - Final report captures a canonical snapshot: deterministic 100-pt score, coverage %, confirmed executive summary, and reviewer signature.
   - A SHA-256 digest is computed over the deterministic canonical JSON.
   - Any post-finalization edit requires generating a new report revision (`rep-rev-2`).

---

## 3. Схема данных SQLite WAL / Data Schema

```sql
CREATE TABLE IF NOT EXISTS transcript_revisions (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL DEFAULT 1,
    is_batch_final INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_assessments (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    rubric_revision_id TEXT NOT NULL,
    transcript_revision_id TEXT NOT NULL,
    reviewer_id TEXT,
    scores_json TEXT NOT NULL,
    reviewer_notes TEXT,
    is_manually_adjusted INTEGER NOT NULL DEFAULT 0,
    is_stale INTEGER NOT NULL DEFAULT 0,
    stale_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_proposals (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    model_profile_id TEXT NOT NULL,
    summary_data_json TEXT NOT NULL,
    is_confirmed INTEGER NOT NULL DEFAULT 0,
    confirmed_by TEXT,
    confirmed_markdown TEXT,
    confirmed_recommendation TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS report_revisions (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL DEFAULT 1,
    final_score_100 REAL,
    coverage_percentage REAL NOT NULL,
    question_scores_json TEXT NOT NULL,
    summary_markdown TEXT NOT NULL,
    hiring_recommendation TEXT,
    confirmed_by TEXT,
    sha256_checksum TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

---

## 4. API Эндпойнты / API Endpoints

| Метод / Method | Путь / Path | Описание / Description |
| :--- | :--- | :--- |
| `POST` | `/api/v1/interviews/{id}/batch-retranscribe` | Запуск пакетной перетранскрибации / Enqueue batch STT pass |
| `GET` | `/api/v1/interviews/{id}/revisions/transcript/diff` | Расчет диффа ревизий стенограммы / Compute revision diff |
| `POST` | `/api/v1/interviews/{id}/assessments/{q_id}/review` | Подтверждение/правка оценки с защитой от 409 / Review assessment with 409 check |
| `GET` | `/api/v1/interviews/{id}/summary` | Получение предложения резюме / Get executive summary proposal |
| `POST` | `/api/v1/interviews/{id}/summary/confirm` | Утверждение резюме рецензентом / Confirm summary |
| `POST` | `/api/v1/interviews/{id}/report/finalize` | Опечатывание отчета и расчет SHA-256 / Finalize and seal report |
| `GET` | `/api/v1/interviews/{id}/export` | Выгрузка протокола с контрольной суммой / Export sealed protocol |
