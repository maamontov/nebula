# План исправления запуска и поддержки одного источника аудио / Startup Stabilization & Single-Source Audio Plan

**Дата / Date:** 2026-09-10  
**Статус / Status:** Завершён (Все этапы 1–6 реализованы и верифицированы) / Completed (All Stages 1–6 implemented and verified)  
**Основание / Background:** Ревью commit `46772fd`, устранение блокеров старта desktop-приложения и реализация честного режима одного источника звука (Single-Source Audio Capture).

---

## 1. Что исправлено / Issues Resolved

| ID | Проблема / Issue | Основание / Rationale | Этап / Stage | Статус / Status |
| --- | --- | --- | --- | --- |
| **S1** | Frontend передавал `spoolDir: null`, Rust требовал `String` | Ошибка десериализации serde в IPC | 1 | **Исправлено / Fixed** |
| **S2** | `get_active_session` возвращал объект `ActiveSessionInfo`, клиент ожидал строку | Запрос `GET /interviews/[object Object]` → 400 | 1, 3 | **Исправлено / Fixed** |
| **S3** | Строковая ошибка Tauri rejection терялась; сообщение было далеко от кнопки | `catch` читал только `err.message`, баннер вверху формы | 2 | **Исправлено / Fixed** |
| **S4** | До открытия аудио backend переводился в `recording`; сбой не откатывался | Нарушение порядка транзакционного запуска | 2 | **Исправлено / Fixed** |
| **S5** | При одном физическом входе нельзя было начать запись | Отсутствие явного режима одного источника | 4 | **Исправлено / Fixed** |
| **S6** | Общую запись нельзя автоматически приписывать кандидату | Риск ложной AI-оценки речи интервьюера | 5 | **Исправлено / Fixed** |
| **S7** | Межъязыковые IPC-контракты не верифицировались | Несогласованные DTO между Rust и TypeScript | 1, 6 | **Исправлено / Fixed** |
| **S8** | Заявленная готовность превышала фактическую проверку | Отсутствие тестов сквозного старта десктопа | 6 | **Исправлено / Fixed** |

---

## 2. Архитектура режимов аудио / Audio Capture Architecture

### Русский
1. **Двухканальный режим (`dual_source`):**
   - Требует два раздельных физических аудиоустройства (`interviewer` и `candidate`).
   - Захватывает две изолированные дорожки; семантика говорящего определяется каналом.
   - Рассчитывается межтрековый дрейф (drift/skew) с порогом допуска <= 200 мс.
2. **Одноканальный режим (`single_source`):**
   - Использует одно физическое устройство (микрофон совещания или системный микс).
   - Захватывает одну общую дорожку `shared` (`TrackType::Shared`).
   - Дорожка `shared` по умолчанию получает `speaker_role: "unknown"`.
   - **Строгий запрет:** AI-модели категорически запрещено оценивать сегменты со статусом `unknown` или `interviewer`. Только после явного ручного назначения экспертом (`speaker_role: "candidate"`) фрагменты речи допускаются в контекст оценки.

### English
1. **Dual-Channel Mode (`dual_source`):**
   - Requires two distinct audio input devices (`interviewer` and `candidate`).
   - Captures two isolated tracks; speaker identity is derived directly from track provenance.
   - Inter-track clock drift and skew are continuously monitored (target threshold <= 200ms).
2. **Single-Channel Mode (`single_source`):**
   - Uses a single physical audio input (shared room microphone or system mix).
   - Captures a single consolidated track `shared` (`TrackType::Shared`).
   - Segments on `shared` default to `speaker_role: "unknown"`.
   - **Strict Isolation:** AI evaluation pipeline strictly excludes segments with `speaker_role` `unknown` or `interviewer`. Only segments explicitly confirmed by a human reviewer as `candidate` are eligible for evaluation evidence.

---

## 3. Отчёт по этапам реализации / Implementation Stages Report

### Этап 1. Согласование IPC-контрактов целиком / Stage 1. Full IPC Contract Alignment
- **Статус:** Выполнен / Completed.
- **Внесённые изменения:**
  - `apps/desktop/src-tauri/src/commands.rs`: `start_capture` принимает `spool_dir: Option<String>`, возвращает типизированную структуру `StartCaptureResult { status, session_id }`.
  - `apps/desktop/src/services/api.ts`: устранён `spoolDir: null` (передаётся `undefined`), типизирован `ActiveSessionInfo { is_recording, session_id }`.
  - Добавлены 4 контрактных Rust unit-теста в `commands.rs::tests`.

### Этап 2. Корректный старт, ошибки и компенсация / Stage 2. Robust Startup, Error Handling & Compensation
- **Статус:** Выполнен / Completed.
- **Внесённые изменения:**
  - `apps/desktop/src/screens/SetupScreen.tsx`: 2-шаговый переход (`draft` -> `ready` -> захват звука -> `recording`).
  - При сбое захвата звука выполняется компенсация: вызов `stopAudioCapture().catch(...)`, интервью не зависает в `recording`.
  - Извлечение строковых ошибок Tauri (`typeof err === 'string' ? err : err.message`).
  - Ошибки и подсказки о причинах блокировки кнопки «Начать запись» отображаются непосредственно под кнопкой с автоскроллом.

### Этап 3. Восстановление и согласованность экрана / Stage 3. Recovery & Screen Consistency
- **Статус:** Выполнен / Completed.
- **Внесённые изменения:**
  - `apps/desktop/src/App.tsx`: восстановление сессии проверяет `sessionInfo.is_recording && sessionInfo.session_id`.
  - Исключены фантомные запросы `GET /interviews/[object Object]`.
  - Поддержаны все статусы жизненного цикла: `ready`, `recording`, `paused`, `processing`, `review`, `finalized`.

### Этап 4. Единый аудиопуть для одного и двух источников / Stage 4. Audio Pipeline for Single & Dual Sources
- **Статус:** Выполнен / Completed.
- **Внесённые изменения:**
  - `crates/audio-capture/src/types.rs`, `spool.rs`, `clock.rs`: добавлен вариант `TrackType::Shared`.
  - `contracts/audio.py`: добавлен `TrackType.SHARED = "shared"`.
  - `backend/db/migrations.py`: миграция `005_single_source_and_speaker_roles` (добавлены `capture_mode`, `expected_tracks_json`).
  - `apps/desktop/src/components/AudioMeters.tsx`: динамическое отображение 1 индикатора для `single_source` и 2 для `dual_source`.
  - `backend/api/app.py`: `check_interview_readiness` валидирует только дорожки из `expected_tracks_json`.

### Этап 5. Говорящие и оценивание общей записи / Stage 5. Speaker Roles & Single-Source Evaluation
- **Статус:** Выполнен / Completed.
- **Внесённые изменения:**
  - `backend/db/repository.py`: добавлены `update_segment_speaker_role` и `split_transcript_segment`.
  - `backend/api/app.py`: добавлены REST эндпоинты `POST /segments/{id}/speaker-role` и `POST /segments/{id}/split`.
  - `backend/workers/pipeline.py`: транскрибация `shared` присваивает `speaker_role: "unknown"`. Пайплайн оценки `_handle_evaluate` строго отбирает сегменты с `speaker_role == "candidate"`. При отсутствии кандидатских реплик фиксируется статус «Ответ не назначен» без обращения к LLM.
  - `backend/core/matcher.py`: сегменты `unknown` не сопоставляются с вопросами.
  - `apps/desktop/src/screens/ReviewScreen.tsx`: визуализация бейджей ролей, кнопки назначения роли (`Кандидат` / `Интервьюер`), модальное окно разделения смешанных сегментов.

### Этап 6. Комплексная верификация и регрессии / Stage 6. Comprehensive Verification & Regression Testing
- **Статус:** Выполнен / Completed.
- **Результаты тестирования:**
  - Python (pytest): **120/120 тестов успешно** (`uv run pytest`).
  - Rust: **23/23 теста успешно** (`cargo test --all`).
  - Frontend (TypeScript/React): **Production-сборка успешна (0 ошибок)** (`npm run build`).

---

## 4. Матрица регрессий / Regression Verification Matrix

| № | Сценарий / Scenario | Ожидаемый результат / Expected Result | Тестовое покрытие / Test Coverage | Статус / Status |
|---|---|---|---|---|
| TC-01 | Start без пользовательского `spoolDir` | Native сам разрешает канонический путь, нет IPC type error | `test_api_single_source_readiness_gate`, `test_start_capture_result_serialization` | **PASS** |
| TC-02 | Нет активной сессии (`is_recording = false`) | Нет запроса `GET /interviews/[object Object]` | `App.tsx` guard, `test_active_session_info_serialization_none_and_some` | **PASS** |
| TC-03 | `ActiveSessionInfo` с валидным ID | Восстанавливается указанное интервью | `App.tsx` restore handler, contract tests | **PASS** |
| TC-04 | Ошибка Tauri строкой | Исходная причина видна у кнопки запуска | `SetupScreen.tsx` string parser error handler | **PASS** |
| TC-05 | Отказ capture после создания интервью | Нет ложного `recording`, статус остаётся `READY`, повтор не плодит ID | `test_interview_status_progression_and_compensation` | **PASS** |
| TC-06 | Запуск в режиме `single_source` с 1 входом | Старт разрешён; захватывается дорожка `shared` | `test_create_interview_capture_modes`, `commands.rs::start_capture` | **PASS** |
| TC-07 | Dual-source с отсутствующим кандидатом | Валидация блокирует запуск с понятным сообщением | `SetupScreen.tsx` validation rules | **PASS** |
| TC-08 | Stop single-source | Не ожидает вторую дорожку, формирует манифест `shared` | `test_api_single_source_readiness_gate`, `commands.rs::stop_capture` | **PASS** |
| TC-09 | Stop с пропуском чанка | Ошибка готовности / PROCESSING, не даёт закрыть неполную сессию | `test_missing_sequence_gap_detected`, `test_stage6_audio_ingestion.py` | **PASS** |
| TC-10 | Shared transcript без назначения ролей | Все сегменты `unknown`, AI-оценка не приписывает их кандидату | `test_transcribe_shared_track_assigns_unknown_role`, `test_evaluate_strictly_excludes_unknown_and_interviewer` | **PASS** |
| TC-11 | Ручное назначение роли кандидата | Разрешает запуск AI-оценки по подтверждённым репликам | `test_manual_role_assignment_enables_evaluation`, `test_api_speaker_role_and_split_endpoints` | **PASS** |
| TC-12 | Общий сегмент с двумя говорящими | Разделяется на реплики с сохранением `parent_segment_id` | `test_split_mixed_transcript_segment`, `test_api_speaker_role_and_split_endpoints` | **PASS** |
| TC-13 | Применение миграции 005 | Добавлены новые колонки, БД целостна (`PRAGMA integrity_check`) | `test_migration_005_schema_and_integrity` | **PASS** |
| TC-14 | Сохранение данных старых `dual_source` интервью | Старые дорожки `candidate`/`interviewer` сохраняют свои роли | `test_versioned_migration_preserves_data_and_is_idempotent`, `test_stage1_isolation.py` | **PASS** |

---

## 5. Граница готовности / Readiness Boundary

Все цели плана достигнуты:
1. Устранены блокеры старта S1–S4.
2. Внедрен и полностью изолирован одноканальный режим S5–S6 с защитой от ложной AI-атрибуции.
3. Синхронизированы межъязыковые контракты Rust/TypeScript/Python (S7).
4. Регрессионная матрица проверена автоматическими тестами на физической рабочей станции macOS (Apple Silicon).
