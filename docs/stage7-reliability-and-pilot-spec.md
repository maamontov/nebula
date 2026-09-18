# Спецификация: Этап 7 — Надёжность, защита приватности и пилотная готовность
# Specification: Stage 7 — Reliability, Privacy Lifecycle & Pilot Readiness

---

## 1. Обзор архитектуры / Architecture Overview

### Русский
Этап 7 завершает формирование производственного контура Nebula, обеспечивая бескомпромиссную отказоустойчивость при авариях воркеров, соблюдение стандартов приватности данных (GDPR / 152-ФЗ) с полным каскадным уничтожением сессий и аудио-спула, защиту от воскрешения удаленных данных («Late Worker Race Condition»), а также горячее резервное копирование SQLite WAL без остановки системы и блокировок читателей/писателей.

### English
Stage 7 finalizes the production-ready foundation of Nebula, providing uncompromising fault tolerance during worker crashes, strict compliance with privacy standards (GDPR / Data Protection regulations) via full cascading deletion of sessions and physical audio spools, protection against ghost data resurrection ("Late Worker Race Condition"), and online hot SQLite WAL backups without system downtime or reader/writer blocking.

---

## 2. Ключевые инварианты и механизмы отказоустойчивости / Key Invariants & Reliability Mechanisms

### Русский
1. **Аренда задач и восстановление после падений (Durable Lease & Crash Recovery)**:
   - Каждая фоновая задача (транскрибация, оценка ответа, генерация резюме) при переходе в статус `PROCESSING` получает временную блокировку аренды (`locked_until = now + duration`).
   - При внезапной гибели воркера (crash, OOM, зависание сети) аренда истекает.
   - Механизм `reclaim_expired_jobs()` и вызов `claim_next_job()` атомарно обнаруживают просроченную аренду и передают задачу свободному воркеру для повторной попытки в рамках лимита `max_attempts`.
   - Идемпотентность гарантирует отсутствие дублирования записей в БД при повторном исполнении.

2. **Защита приватности и каскадное удаление (Privacy Lifecycle & Cascade Deletion)**:
   - По вызову `DELETE /api/v1/interviews/{id}` запускается атомарная транзакция SQLite WAL.
   - Благодаря `PRAGMA foreign_keys = ON;` удаление родительской записи в таблице `interviews` немедленно каскадируется на все дочерние таблицы (`transcript_segments`, `transcript_revisions`, `assessment_proposals`, `human_assessments`, `summary_proposals`, `report_revisions`, `question_associations`, `interview_plans`).
   - Задачи очереди (`jobs`) и журнал аудита (`audit_events`) удаляются в той же транзакции.
   - Каталог физического аудио-спула на диске (`spool/{interview_id}`) безвозвратно уничтожается через `shutil.rmtree`.

3. **Защита от воскрешения удаленных данных (Anti-Resurrection Late Worker Invariant)**:
   - Воркер, выполняющий долгий внешний вызов к STT или LLM, осуществляет двойную валидацию существования сессии:
     1. Перед началом обработки задачи.
     2. Непосредственно перед сохранением результата в БД.
   - Если пользователь или администратор удалил интервью во время работы воркера, результат гарантированно сбрасывается, не создавая висячих («зомби») записей и не вызывая ошибок нарушения внешних ключей.

4. **Горячее резервирование SQLite WAL (Hot Online Backup)**:
   - Резервное копирование выполняется с использованием встроенного SQLite Online Backup API (`source_conn.backup(dest_conn)`).
   - Это гарантирует создание целостного, побайтово согласованного снапшота базы данных даже во время активной записи в WAL-журнал.
   - После создания бэкапа автоматически выполняется верификация целостности (`PRAGMA integrity_check == 'ok'`).

### English
1. **Durable Lease & Crash Recovery**:
   - Every background job (transcription, evaluation, executive summary) acquires a timed lease (`locked_until = now + duration`) upon transitioning to `PROCESSING`.
   - If a worker crashes or hangs abruptly, the lease expires.
   - `reclaim_expired_jobs()` and `claim_next_job()` atomically detect expired leases and reassign the job to an available worker up to `max_attempts`.
   - Complete idempotency ensures no duplicate segments or proposals are created upon retry.

2. **Privacy Lifecycle & Cascade Deletion**:
   - Invoking `DELETE /api/v1/interviews/{id}` triggers an atomic SQLite WAL transaction.
   - With `PRAGMA foreign_keys = ON;`, deleting the parent row in `interviews` instantly cascades across all dependent tables (`transcript_segments`, `transcript_revisions`, `assessment_proposals`, `human_assessments`, `summary_proposals`, `report_revisions`, `question_associations`, `interview_plans`).
   - Queued tasks (`jobs`) and audit trail (`audit_events`) are purged within the same transaction.
   - The on-disk physical audio spool directory (`spool/{interview_id}`) is purged using `shutil.rmtree`.

3. **Late Worker Anti-Resurrection Invariant**:
   - Workers executing long-running external STT/LLM requests perform a double existence check:
     1. Before initiating execution.
     2. Immediately prior to committing results to the database.
   - If the interview was deleted while the worker was processing, results are safely discarded without creating zombie records or foreign key constraint violations.

4. **Hot Online SQLite WAL Backup**:
   - Backups are performed using the native SQLite Online Backup API (`source_conn.backup(dest_conn)`).
   - Guarantees byte-level consistency and zero locks on active WAL readers/writers.
   - Every snapshot is verified via `PRAGMA integrity_check == 'ok'`.

---

## 3. Спецификация API / API Specification

### 3.1. Удаление сессии / Delete Interview
```http
DELETE /api/v1/interviews/{interview_id}?spool_dir={path}
```
**Response (200 OK):**
```json
{
  "status": "deleted",
  "interview_id": "inv-12345",
  "success": true
}
```

### 3.2. Создание горячего бэкапа / Hot Database Backup
```http
POST /api/v1/system/backup
Content-Type: application/json

{
  "target_path": "backups/nebula_20260910.db"
}
```
**Response (200 OK):**
```json
{
  "status": "ok",
  "target_path": "backups/nebula_20260910.db"
}
```

### 3.3. Проверка целостности БД / Check Database Integrity
```http
GET /api/v1/system/integrity
```
**Response (200 OK):**
```json
{
  "status": "ok",
  "integrity_ok": true
}
```

---

## 4. Верификация готовности к пилоту / Acceptance Verification

Сквозная проверка реализована в `evals/run_stage7_reliability_and_pilot.py` и подтверждает 100% покрытие:
1. **Имитация аварии воркера**: Перехват и успешное завершение зависшего задания вторым воркером по истечении аренды.
2. **Горячий бэкап**: Создание снапшота активной базы и успешный проход `PRAGMA integrity_check`.
3. **Защита приватности**: Полное удаление сессии из всех 6 таблиц БД и физическая зачистка каталога спула с аудиофайлами.
4. **Защита от воскрешения**: Отсутствие зомби-записей при завершении задания удаленной сессии.
