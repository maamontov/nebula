# Спецификация Этапа 3: Desktop Shell, Хранилище SQLite WAL и Фоновый Pipeline Worker
# Stage 3 Specification: Desktop Shell, SQLite WAL Storage, and Durable Pipeline Worker

---

## 1. Архитектура десктопного приложения / Desktop App Architecture

### Русский
Десктопное приложение Nebula построено на стеке:
- **Оболочка**: Tauri 2 (Rust).
- **Фронтенд**: React 19 + TypeScript + Vite + Tailwind CSS v4.
- **Взаимодействие**: Tauri IPC (`tauri::command` / `@tauri-apps/api/core`).
- **Связь с захватом звука**: Прямой вызов крейта воркспейса `crates/audio-capture` без промежуточных сокетов.

Экраны приложения:
1. **Экран подготовки (`SetupScreen`)**:
   - Метаданные собеседования (кандидат, вакансия, компетенции).
   - Выбор физических устройств (микрофон интервьюера и системный лупбек кандидата).
   - **Блокирующий чекбокс согласия на запись** (запись технически невозможна без подтверждения согласия).
2. **Экран активного интервью (`LiveSessionScreen`)**:
   - Монотонный таймер сессии.
   - Двойной VU-метр громкости с цветовой индикацией (зеленый / желтый / красный пики).
   - Трекер вопросов и статуса покрытия.
   - Раздельный поток транскрипта (Интервьюер / Кандидат).
   - Карточки предварительных оценок с точными дословными цитатами кандидата.
3. **Экран ревью и калибровки (`ReviewScreen`)**:
   - Инспекция предложенных LLM оценок и цитат.
   - Ручная корректировка баллов (шкала 1–5).
   - Заметки интервьюера.
   - Расчет итогового 100-балльного скора.
   - Экспорт структурированного JSON отчета.

### English
The Nebula desktop shell is built on the following stack:
- **Shell**: Tauri 2 (Rust).
- **Frontend**: React 19 + TypeScript + Vite + Tailwind CSS v4.
- **IPC Protocol**: Tauri IPC (`tauri::command` / `@tauri-apps/api/core`).
- **Native Audio Link**: Direct in-process binding with the workspace crate `crates/audio-capture`.

Application Screens:
1. **Setup Screen (`SetupScreen`)**:
   - Interview metadata (candidate name, role, competencies).
   - Physical audio device pickers (interviewer microphone and candidate loopback).
   - **Mandatory Consent Checkbox** (recording is hard-blocked without explicit participant consent).
2. **Live Session Screen (`LiveSessionScreen`)**:
   - Monotonic session timer.
   - Dual VU meters with dynamic level coloring (green / yellow / red clipping).
   - Question tracker and rubric coverage indicators.
   - Dual-track live transcript feed (Interviewer / Candidate).
   - Live assessment proposal cards with verbatim candidate quotes.
3. **Review & Calibration Screen (`ReviewScreen`)**:
   - Inspection of LLM-proposed scores and verified citations.
   - Human score overrides (1–5 scale).
   - Reviewer notes.
   - Aggregated 100-point score computation.
   - Structured JSON report export.

---

## 2. Локальное хранилище и Durable Очередь (SQLite WAL) / Local Storage & Durable Queue

### Русский
Все сущности и фоновые задачи сохраняются в локальной реляционной базе данных SQLite:
- **Режим WAL (Write-Ahead Logging)**: `PRAGMA journal_mode = WAL;` и `PRAGMA synchronous = NORMAL;` обеспечивают конкурентный доступ без взаимных блокировок читателей и писателей.
- **Внешние ключи**: `PRAGMA foreign_keys = ON;`.
- **Схема (`backend/db/schema.sql`)**:
  - `interviews`: метаданные сессии, текущий статус жизненного цикла, отметка согласия.
  - `interview_plans`: версии планов и рубрик с версионированием.
  - `transcript_segments`: сегменты распознанного аудио с привязкой к дорожкам и миллисекундам.
  - `assessment_proposals`: предложения оценок от модели с дословными цитатами и статусом ревью.
  - `jobs`: надежная персистентная очередь фоновых задач (STT транскрибация, оценка ответов) с арендой (`locked_until`), счетчиком попыток (`attempts / max_attempts`) и обработкой ошибок.
  - `audit_events`: иммутабельный журнал аудита всех ключевых действий (создание, переходы состояний, утверждение оценок).

### English
All entities and background asynchronous tasks are stored in local SQLite:
- **WAL Mode (Write-Ahead Logging)**: `PRAGMA journal_mode = WAL;` and `PRAGMA synchronous = NORMAL;` guarantee concurrent reads and non-blocking writes.
- **Foreign Keys**: `PRAGMA foreign_keys = ON;`.
- **Schema (`backend/db/schema.sql`)**:
  - `interviews`: session metadata, lifecycle status, consent timestamp and version.
  - `interview_plans`: versioned interview rubrics and questions.
  - `transcript_segments`: speech-to-text segments linked to speaker tracks and timestamps in ms.
  - `assessment_proposals`: LLM-generated scores with verified verbatim citations and human approval fields.
  - `jobs`: durable background task queue (`TRANSCRIBE_AUDIO`, `EVALUATE_QUESTION`) with lease locks (`locked_until`), retry counters (`attempts / max_attempts`), and error state tracking.
  - `audit_events`: immutable audit trail recording all critical transitions, creations, and overrides.

---

## 3. Фоновый конвейер обработки (Pipeline Worker) / Background Pipeline Worker

### Русский
Модуль [`backend/workers/pipeline.py`](file:///Users/wital/dev/nebula/backend/workers/pipeline.py):
1. Выполняет аренду задач из таблицы `jobs` с защитой от двойного исполнения (`locked_until = now + lease_sec`).
2. Обрабатывает:
   - `TRANSCRIBE_AUDIO`: транскрибация входящего чанка через `Whisper-large-v3-turbo`, сохранение сегмента в БД.
   - `EVALUATE_QUESTION`: структурированный запрос к `google/gemini-3.8-flash` (или резервной `qwen/qwen3.7-plus`), валидация дословных цитат через `EvidenceValidator`, сохранение предложения в `assessment_proposals`.
3. Записывает событие завершения или ошибки в `audit_events`.
4. В случае сбоя повторяет задачу с экспоненциальным бэкоффом до `max_attempts`.

### English
The [`backend/workers/pipeline.py`](file:///Users/wital/dev/nebula/backend/workers/pipeline.py) component:
1. Claims queued tasks using database lease locks (`locked_until = now + lease_sec`) preventing double execution.
2. Executes handlers:
   - `TRANSCRIBE_AUDIO`: audio transcription via `Whisper-large-v3-turbo`, writing segments into SQLite.
   - `EVALUATE_QUESTION`: structured evaluation via `google/gemini-3.8-flash` (or fallback `qwen/qwen3.7-plus`), quotation validation with `EvidenceValidator`, writing proposals to `assessment_proposals`.
3. Records completion or failure events to `audit_events`.
4. Automatically retries failed jobs up to `max_attempts`.

---

## 4. Результаты сквозной верификации (E2E Verification Results)

### Русский
Сквозной интеграционный тест [`evals/run_stage3_e2e.py`](file:///Users/wital/dev/nebula/evals/run_stage3_e2e.py) успешно выполняет полный цикл:
1. Инициализация базы SQLite WAL.
2. Создание интервью через API (`POST /api/v1/interviews`).
3. Переходы конечного автомата `DRAFT` -> `READY` -> `RECORDING` (с подтверждением согласия).
4. Запись сегментов транскрипта интервьюера и кандидата.
5. Постановка задач в durable очередь и обработка воркером: модель `google/gemini-3.8-flash` выставила оценки с дословными цитатами из транскрипта, прошедшими строгую проверку `EvidenceValidator`.
6. Ревью человека: одобрение вопроса 1 и калибровочная корректировка балла вопроса 2.
7. Переход сессии в статус `FINALIZED`.
8. Расчет 100-балльного скора (90.0/100) и валидация экспорта JSON с 10 событиями аудита.

### English
The end-to-end acceptance script [`evals/run_stage3_e2e.py`](file:///Users/wital/dev/nebula/evals/run_stage3_e2e.py) successfully completed the full lifecycle:
1. SQLite WAL database initialization.
2. Interview creation via API (`POST /api/v1/interviews`).
3. State machine transitions: `DRAFT` -> `READY` -> `RECORDING` (with validated consent).
4. Transcript ingestion for interviewer and candidate tracks.
5. Background job processing: `google/gemini-3.8-flash` generated structured assessments with verbatim quotes verified by `EvidenceValidator`.
6. Human review: approval of Question 1 and score calibration of Question 2.
7. Transition to `FINALIZED`.
8. Final 100-point score calculation (90.0/100) and export JSON verification with 10 immutable audit events.
