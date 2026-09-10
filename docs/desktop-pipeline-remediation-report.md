# Отчёт об устранении дефектов десктопного аудиопайплайна / Desktop Audio Pipeline Remediation Report

[English version below](#english)

---

## Русский

### 1. Обзор и цели ремедиации
Данный отчёт фиксирует результаты выполнения плана устранения дефектов десктопного контура захвата, передачи, транскрибации и оценки аудио (`docs/desktop-pipeline-remediation-plan.md`). 

Все этапы плана (**A → B → C → D → E → F → G**) успешно реализованы в едином потоке без использования субагентов, с абсолютным сохранением всех исторических данных спула (3471 чанк, 105.89 МБ).

---

### 2. Реализованные этапы и архитектурные изменения

#### Этап A: Единый канонический контур хранения и инвентаризация спула
- **Канонические переменные окружения**: В [`scripts/nebula.sh`](../scripts/nebula.sh) зафиксированы и экспортируются абсолютные пути:
  - `NEBULA_DATA_DIR`: `$PROJECT_ROOT/data`
  - `NEBULA_CAPTURE_SPOOL_DIR`: `$PROJECT_ROOT/apps/desktop/src-tauri/spool` (с сохранением существующего архива)
  - `NEBULA_BACKEND_SPOOL_DIR`: `$PROJECT_ROOT/data/spool`
  - `NEBULA_DB_PATH`: `$PROJECT_ROOT/data/nebula.db`
  - `NEBULA_BACKUP_DIR`: `$PROJECT_ROOT/data/backups`
- **Бэкенд конфигурация**: В [`backend/api/app.py`](../backend/api/app.py) и [`backend/db/database.py`](../backend/db/database.py) пути синхронизированы с каноническими переменными; добавлен эндпоинт `GET /api/v1/system/config`.
- **Инвентаризация исторического спула**: Создан скрипт [`scripts/inventory_spool.py`](../scripts/inventory_spool.py). Проведена инвентаризация 3471 чанков:
  - Сессия `inv-mtvrsil6`: 61 чанк (1.85 МБ), оба трека запечатаны (`SEALED`), целостность 100%.
  - Сессия `inv-mtvs53um`: 1675 чанков (51.04 МБ), оба трека не запечатаны (`UNSEALED`), целостность 100%, 0 повреждений.
  - Аудит речи: утечек речи между вопросами в спуле не обнаружено.

#### Этап B: Надежная доставка чанков из Tauri Desktop (`SessionUploader`)
- **Формат аудио**: Устранено расхождение сериализации (`pcm_s16le` vs `pcm_s16_le`) в [`crates/audio-capture/src/types.rs`](../crates/audio-capture/src/types.rs) и [`contracts/audio.py`](../contracts/audio.py).
- **Фоновый загрузчик (`uploader.rs`)**: В [`apps/desktop/src-tauri/src/uploader.rs`](../apps/desktop/src-tauri/src/uploader.rs) реализован `SessionUploader`:
  - Идемпотентная доставка чанков на `POST /api/v1/interviews/{id}/audio/chunks`.
  - Атомарные файлы подтверждения (`.ack`) и файлы ошибок (`.err`).
  - Экспоненциальный backoff с jitter (от 200 мс до 5 с).
  - Отслеживание чанков "в полёте" (`in_flight`) и прогресса выгрузки.
  - Обработка 404 (сессия удалена) и 409 (сессия запечатана / завершена).

#### Этап C: Серверный гейт готовности (`Readiness Gate`) и запечатывание манифестов
- **Проверка готовности**: В [`backend/api/app.py`](../backend/api/app.py) реализована функция `check_interview_readiness`, проверяющая:
  1. Наличие и валидность запечатанных манифестов обоих треков (`TrackManifest`).
  2. Непрерывность последовательности чанков от 0 до $N-1$ в БД `audio_chunks`.
  3. Завершение всех задач транскрибации (`TRANSCRIBE_AUDIO` в статусе `COMPLETED`).
- **Эндпоинт готовности**: Добавлен `GET /api/v1/interviews/{interview_id}/readiness`.
- **Строгая валидация статуса**: Переход `POST /api/v1/interviews/{id}/status` в статус `REVIEW` возвращает `409 Conflict`, если сессия не прошла гейт готовности.

#### Этап D: Реальная аудио-телеметрия (RMS и Peak)
- **Атомарные метрики**: В [`crates/audio-capture/src/capture.rs`](../crates/audio-capture/src/capture.rs) внедрен lock-free трекер `AudioLevelMetrics` на базе `AtomicU32` с побитовым хранением float-значений.
- **Расчет пиков**: В [`crates/audio-capture/src/resampler.rs`](../crates/audio-capture/src/resampler.rs) добавлена функция `calculate_peak_f32`.
- **Устранение фиктивных уровней**: В [`apps/desktop/src-tauri/src/commands.rs`](../apps/desktop/src-tauri/src/commands.rs) команда `get_audio_levels` теперь считывает реальные атомарные значения захвата вместо захардкоженных констант.
- **Валидация устройств**: Устранен скрытый fallback на микрофон по умолчанию; запрещен выбор одного и того же физического устройства для каналов интервьюера и кандидата.

#### Этап E: Целостность пайплайна оценивания (устранение утечек и устаревания)
- **Устранение утечки `cand_all`**: В [`backend/workers/pipeline.py`](../backend/workers/pipeline.py) удален fallback на всю речь кандидата при отсутствии сегментов у конкретного вопроса.
- **Интеграция `QuestionMatcher`**: При отсутствии привязок воркер автоматически запускает `QuestionMatcher` по вопросам плана и сегментам речи.
- **Отсутствие ответа**: Если после сопоставления у вопроса нет сегментов речи кандидата, воркер фиксирует предложение с `score = None` и причиной `"Ответ кандидата отсутствует..."` **без обращения к LLM**.
- **Динамические ревизии**: Используются актуальные ревизии из БД (`active_rubric_revision_id`, `active_transcript_revision_id`).
- **Проверка устаревания**: Перед сохранением предложения проверяется изменение ревизий во время исполнения; при изменении выставляется `is_stale = 1` со `stale_reason`.

#### Этап F: Адаптация Desktop UI и восстановление сессий
- **SetupScreen**: Удален захардкоженный путь `'./spool'`, добавлена валидация различия выбранных аудиоустройств.
- **LiveSessionScreen**:
  - Удален фиксированный 15-секундный таймаут.
  - Манифесты из `stopAudioCapture` передаются в `stopInterview`.
  - Внедрен опрос `/readiness` и прогресса выгрузки до получения `is_ready: true`.
  - Переход к ревью осуществляется строго после подтверждения готовности данных.
- **App.tsx**: Добавлено восстановление активной сессии через `getActiveSession()` и `getInterview()` при монтировании компонента (устойчивость к перезагрузке WebView).

#### Этап G: Сквозная верификация и регрессионное тестирование
- Создан сквозной интеграционный тест [`tests/test_desktop_pipeline_e2e.py`](../tests/test_desktop_pipeline_e2e.py), проверяющий весь жизненный цикл от загрузки чанков и гейта готовности до раздельного скоринга вопросов и финализации отчёта.

---

### 3. Результаты проверок и тестов

| Набор тестов / Компонент | Запущенная команда | Результат |
|---|---|:---:|
| **Python Backend & E2E Matrix** | `uv run pytest` | **110 passed**, 0 failed |
| **Сквозной E2E тест пайплайна** | `uv run pytest tests/test_desktop_pipeline_e2e.py` | **1 passed** (100% lifecycle) |
| **Тесты изоляции вопросов & Stale** | `uv run pytest tests/test_stage7_evidence_matcher_queue.py` | **10 passed**, 0 failed |
| **Rust Audio Capture Core** | `cargo test -p audio-capture` | **19 passed**, 0 failed |
| **Rust Workspace & Synthetic 1h** | `cargo test --workspace` | **19 passed** (вкл. 1h запись за 42s) |
| **Tauri Desktop Compilation** | `cargo check -p nebula-desktop` | **0 errors, 0 warnings** |
| **Desktop Frontend Build** | `npm --prefix apps/desktop run build` | **0 errors (tsc & vite build)** |

---

<a name="english"></a>
## English

### 1. Remediation Overview & Objectives
This report documents the completion of the remediation plan for the desktop audio capture, delivery, transcription, and scoring pipeline (`docs/desktop-pipeline-remediation-plan.md`).

All stages (**A → B → C → D → E → F → G**) were executed sequentially in the main thread without subagents, guaranteeing 100% data preservation of existing spool archives (3471 chunks, 105.89 MB).

---

### 2. Implemented Stages & Architecture Changes

#### Stage A: Unified Storage & Historical Spool Inventory
- **Canonical Environment Paths**: [`scripts/nebula.sh`](../scripts/nebula.sh) exports unified absolute paths:
  - `NEBULA_DATA_DIR`: `$PROJECT_ROOT/data`
  - `NEBULA_CAPTURE_SPOOL_DIR`: `$PROJECT_ROOT/apps/desktop/src-tauri/spool` (preserving historical archive)
  - `NEBULA_BACKEND_SPOOL_DIR`: `$PROJECT_ROOT/data/spool`
  - `NEBULA_DB_PATH`: `$PROJECT_ROOT/data/nebula.db`
  - `NEBULA_BACKUP_DIR`: `$PROJECT_ROOT/data/backups`
- **Backend Sync**: [`backend/api/app.py`](../backend/api/app.py) and [`backend/db/database.py`](../backend/db/database.py) resolve canonical paths; added `GET /api/v1/system/config`.
- **Spool Inventory**: Created [`scripts/inventory_spool.py`](../scripts/inventory_spool.py). Catalogued 3471 chunks:
  - Session `inv-mtvrsil6`: 61 chunks (1.85 MB), `SEALED` both tracks, 100% integrity.
  - Session `inv-mtvs53um`: 1675 chunks (51.04 MB), `UNSEALED` both tracks, 100% integrity, 0 corruption.
  - Speech audit: Zero speech leaks detected in historical recordings.

#### Stage B: Durable Desktop Delivery (`SessionUploader`)
- **Audio Format Match**: Resolved format serialization discrepancy (`pcm_s16le` vs `pcm_s16_le`) in [`crates/audio-capture/src/types.rs`](../crates/audio-capture/src/types.rs) and [`contracts/audio.py`](../contracts/audio.py).
- **Background Uploader**: Implemented `SessionUploader` in [`apps/desktop/src-tauri/src/uploader.rs`](../apps/desktop/src-tauri/src/uploader.rs):
  - Idempotent chunk ingestion via `POST /api/v1/interviews/{id}/audio/chunks`.
  - Atomic acknowledgment (`.ack`) and error (`.err`) sidecar files.
  - Exponential backoff with jitter (200ms to 5s).
  - In-flight chunk tracking and upload progress reporting.
  - Safe handling of 404 (deleted interview) and 409 (sealed/finalized).

#### Stage C: Server Readiness Gate & Manifest Sealing
- **Readiness Verification**: Implemented `check_interview_readiness` in [`backend/api/app.py`](../backend/api/app.py) enforcing:
  1. Both track manifests present and sealed (`TrackManifest`).
  2. Sequential completeness of chunks from 0 to $N-1$ in SQLite `audio_chunks`.
  3. Completion of all pending STT jobs (`TRANSCRIBE_AUDIO` in `COMPLETED` status).
- **Readiness API**: Added `GET /api/v1/interviews/{interview_id}/readiness`.
- **State Enforcement**: `POST /api/v1/interviews/{id}/status` transitioning to `REVIEW` returns `409 Conflict` if session is not ready.

#### Stage D: Real Audio Telemetry (RMS & Peak)
- **Lock-Free Metrics**: Added `AudioLevelMetrics` using `AtomicU32` bitcasts in [`crates/audio-capture/src/capture.rs`](../crates/audio-capture/src/capture.rs).
- **Peak Calculation**: Added `calculate_peak_f32` in [`crates/audio-capture/src/resampler.rs`](../crates/audio-capture/src/resampler.rs).
- **Eliminated Fake Telemetry**: In [`apps/desktop/src-tauri/src/commands.rs`](../apps/desktop/src-tauri/src/commands.rs), `get_audio_levels` returns real calculated atomic RMS/peak instead of hardcoded numbers.
- **Device Validation**: Eliminated silent fallback to default microphone; disallowed identical audio devices for interviewer and candidate channels.

#### Stage E: Scoring Pipeline Integrity (No Leaks & Revision Check)
- **Eliminated `cand_all` Leak**: Removed fallback to all candidate speech in [`backend/workers/pipeline.py`](../backend/workers/pipeline.py).
- **`QuestionMatcher` Integration**: Automatically runs segment matcher when associations are missing for planned questions.
- **Explicit Unanswered Proposals**: If candidate gave no response for a question, records proposal with `score = None` and descriptive reason **without calling LLM**.
- **Dynamic Revisions**: Fetches active rubric and transcript revisions from the database.
- **OCC Stale Check**: Detects revision updates during evaluation and tags proposal with `is_stale = 1` and `stale_reason`.

#### Stage F: Desktop UI Alignment & Session Reconnection
- **SetupScreen**: Removed hardcoded `'./spool'`, added device differentiation validation.
- **LiveSessionScreen**:
  - Removed arbitrary 15s wait timeout.
  - Passes real sealed manifests to `stopInterview`.
  - Polls `/readiness` and upload progress until `is_ready: true`.
  - Enforces readiness gate before proceeding to review screen.
- **App.tsx**: Restores active session on component mount via `getActiveSession()` and `getInterview()` (resilient across WebView reloads).

#### Stage G: End-to-End Verification
- Created [`tests/test_desktop_pipeline_e2e.py`](../tests/test_desktop_pipeline_e2e.py) validating the complete workflow from chunk ingestion and readiness gate to leak-free scoring and immutable finalization.

---

### 3. Verification Test Matrix

| Test Suite / Component | Execution Command | Result |
|---|---|:---:|
| **Python Backend & E2E Matrix** | `uv run pytest` | **110 passed**, 0 failed |
| **Desktop Pipeline E2E Test** | `uv run pytest tests/test_desktop_pipeline_e2e.py` | **1 passed** (100% lifecycle) |
| **Question Isolation & Stale Tests** | `uv run pytest tests/test_stage7_evidence_matcher_queue.py` | **10 passed**, 0 failed |
| **Rust Audio Capture Core** | `cargo test -p audio-capture` | **19 passed**, 0 failed |
| **Rust Workspace & 1h Synthetic** | `cargo test --workspace` | **19 passed** (incl. 1h simulation in 42s) |
| **Tauri Desktop Compilation** | `cargo check -p nebula-desktop` | **0 errors, 0 warnings** |
| **Desktop Frontend Build** | `npm --prefix apps/desktop run build` | **0 errors (tsc & vite build)** |
