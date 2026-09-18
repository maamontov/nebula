# Отчёт о реализации сборщика цельных реплик стенограммы / Single-Source Transcript Turn Assembler Implementation Report

[English version below](#english)

---

## Русский

### 1. Обзор реализации
В соответствии с планом [`docs/single-source-transcript-turn-assembler-plan.md`](./single-source-transcript-turn-assembler-plan.md) реализован механизм формирования цельных реплик стенограммы в режиме `single_source` (а также для `dual_source`) на основе пауз (VAD).

Решение решает ключевую проблему: стенограмма больше не отображается как рваный поток случайных 1–4 секундных фрагментов, зависящих от сетевых задержек uploader-а или размера батча воркера. Теперь STT получает естественные связные смысловые блоки (5–12+ секунд), сформированные по акустическим паузам.

### 2. Сохранённые инварианты и архитектурные решения
1. **Аппаратный захват без изменений**: размер чанка спула в Rust остаётся строго **1000 мс** (надёжность записи, минимальные потери при авариях, устойчивый drift/clock).
2. **Нулевая избыточность хранения**: PCM-байты не дублируются в базе данных. Таблица `jobs` хранит только детерминированные границы `(first_sequence, first_sample_offset, last_sequence, last_sample_offset)`. Реконструкция PCM производится из уже сохранённых файлов `audio_chunks` с валидацией непрерывности sequence, формата и SHA-256 контрольных сумм.
3. **Изоляция и границы тишины**:
   - VAD реализован на основе энергии фреймов (20 мс, 320 сэмплов) с адаптивным порогом шума и гистерезисом (`speech_threshold = noise_floor + 14 dB`, `silence_threshold = noise_floor + 7 dB`).
   - Настраиваемый таймаут паузы (800 мс) разделяет реплики.
   - Pre-roll (240 мс) и post-roll (160 мс) предотвращают срезание согласных звуков в начале и конце реплик.
   - Максимальная длительность реплики ограничена 12.0 с (принудительный срез без overlap при длительной непрерывной речи).
   - Смена `capture_epoch`, пропуск номеров `sequence` и паузы захвата сбрасывают состояние VAD и никогда не склеивают несвязанный звук.
4. **Продуктовые инварианты ролей и evidence**:
   - Для дорожки `shared` роль по умолчанию строго остаётся `speaker_role="unknown"`, пока пользователь-человек явно не назначит роль.
   - `unknown` и `interviewer` по-прежнему исключены из доказательной базы кандидата (`candidate evidence`).
   - Изменение транскрипта формирует новую транзакционную ревизию с OCC (409 при конфликтах).
5. **Идемпотентность и синхронизация при завершении**:
   - Таблица `transcript_assembly_state` хранит текущий курсор сборки `(next_sequence, next_sample_offset)`.
   - При остановке интервью (`/stop`) и проверке готовности (`/readiness`) открытые речевые хвосты принудительно выталкиваются (`flush_turn_assembly`) в хронологическом порядке дорожек (`MIN(start_time_ms) ASC`).
   - Батчевая ретранскрибация (`BATCH_RETRANSCRIBE`) также использует `TurnAssembler` для сборки цельных реплик.

### 3. Затронутые компоненты
- [`backend/core/turn_assembler.py`](../backend/core/turn_assembler.py): модуль VAD и алгоритма сборки `TurnAssembler`, структуры `AudioChunkRef`, `TurnAssemblyCursor`, `AssembledTurn`.
- [`backend/db/schema.sql`](../backend/db/schema.sql): таблица `transcript_assembly_state` и индекс `idx_transcript_assembly`.
- [`backend/db/migrations.py`](../backend/db/migrations.py): миграция `009_transcript_turn_assembly`, поднятие `TARGET_VERSION` до 9.
- [`backend/db/repository.py`](../backend/db/repository.py): методы `get_turn_assembly_cursor`, `commit_turn_assembly_results`, `get_audio_chunks_for_assembly`, `get_turn_audio_pcm`, `flush_turn_assembly`.
- [`backend/workers/pipeline.py`](../backend/workers/pipeline.py): разделение задач на `TRANSCRIBE_AUDIO` (сборка реплик) и `TRANSCRIBE_TURN` (вызов STT для реплики); поддержка `BATCH_RETRANSCRIBE` через сборщик; обратная совместимость legacy payload.
- [`backend/api/app.py`](../backend/api/app.py): flush сборщика при `/stop` и проверка готовности транскрипции в `check_interview_readiness`.
- [`tests/test_turn_assembler.py`](../tests/test_turn_assembler.py): 9 юнит-тестов VAD и сборки реплик.
- [`tests/test_turn_assembler_repository.py`](../tests/test_turn_assembler_repository.py): 4 теста персистентности курсора, сборки PCM и миграции.

### 4. Результаты проверок
- **Python / pytest**: 164 passed (все тесты зеленые, включая e2e и stage6/stage8 регрессию).
- **Ruff**: `All checks passed!` (0 предупреждений/ошибок).
- **Rust / cargo fmt**: форматирование валидно.
- **Rust / cargo test**: 27 passed (включая unit, integration и 1-hour synthetic capture).
- **Desktop frontend**: `npm --prefix apps/desktop run build` успешен.
- **Git diff**: проверка `git diff --check` успешна (0 whitespace дефектов).

---

<a name="english"></a>
## English

### 1. Implementation Overview
In accordance with [`docs/single-source-transcript-turn-assembler-plan.md`](./single-source-transcript-turn-assembler-plan.md), a coherent speech turn assembly mechanism based on acoustic pauses (VAD) has been implemented for `single_source` mode (as well as `dual_source`).

This resolves a core UX issue: transcripts are no longer presented as disconnected 1–4 second fragments dependent on uploader timings or worker backlog size. STT now receives natural, contiguous speech segments (5–12+ seconds) delineated by actual speech pauses.

### 2. Invariants and Architectural Decisions
1. **Unchanged Capture Chunk Size**: Rust capture chunk size remains strictly **1000 ms** (recording reliability, fault isolation, robust clock/drift tracking).
2. **Zero Storage Redundancy**: PCM bytes are not duplicated in the database. The `jobs` table records only deterministic slice offsets `(first_sequence, first_sample_offset, last_sequence, last_sample_offset)`. Audio reconstruction reads directly from stored `audio_chunks` files with sequence continuity, format, and SHA-256 verification.
3. **Isolation and Silence Boundary Detection**:
   - VAD operates on 20 ms frames (320 samples) with adaptive noise tracking and hysteresis (`speech_threshold = noise_floor + 14 dB`, `silence_threshold = noise_floor + 7 dB`).
   - Configurable silence timeout (800 ms) marks turn endings.
   - Pre-roll (240 ms) and post-roll (160 ms) prevent clipping initial and trailing consonants.
   - Maximum turn duration is capped at 12.0 s (forced cut without overlap during continuous speech).
   - `capture_epoch` changes, missing sequence gaps, and capture pauses reset VAD state, preventing audio leakage across session boundaries.
4. **Product Invariants on Roles and Evidence**:
   - For `shared` tracks, the role remains strictly `speaker_role="unknown"` until explicitly assigned by a human interviewer.
   - `unknown` and `interviewer` utterances remain excluded from candidate scoring evidence.
   - Transcript updates create transactional revisions guarded by OCC (409 conflict detection).
5. **Idempotency and Stop Synchronization**:
   - The `transcript_assembly_state` table tracks cursor progress `(next_sequence, next_sample_offset)`.
   - On `/stop` and `/readiness` checks, pending speech tails are flushed (`flush_turn_assembly`) preserving track chronology (`MIN(start_time_ms) ASC`).
   - `BATCH_RETRANSCRIBE` routes audio chunks through `TurnAssembler` for consistent coherent turn transcription.

### 3. Affected Components
- [`backend/core/turn_assembler.py`](../backend/core/turn_assembler.py): VAD and turn assembly engine, `AudioChunkRef`, `TurnAssemblyCursor`, `AssembledTurn`.
- [`backend/db/schema.sql`](../backend/db/schema.sql): `transcript_assembly_state` table and `idx_transcript_assembly` index.
- [`backend/db/migrations.py`](../backend/db/migrations.py): Migration `009_transcript_turn_assembly`, bumping `TARGET_VERSION` to 9.
- [`backend/db/repository.py`](../backend/db/repository.py): `get_turn_assembly_cursor`, `commit_turn_assembly_results`, `get_audio_chunks_for_assembly`, `get_turn_audio_pcm`, `flush_turn_assembly`.
- [`backend/workers/pipeline.py`](../backend/workers/pipeline.py): Separation of `TRANSCRIBE_AUDIO` (turn assembly) and `TRANSCRIBE_TURN` (STT call); full assembler integration in `BATCH_RETRANSCRIBE`; backward compatibility for legacy jobs.
- [`backend/api/app.py`](../backend/api/app.py): Flush on `/stop` and turn status verification in `check_interview_readiness`.
- [`tests/test_turn_assembler.py`](../tests/test_turn_assembler.py): 9 unit tests for VAD and turn assembly.
- [`tests/test_turn_assembler_repository.py`](../tests/test_turn_assembler_repository.py): 4 tests for cursor persistence, PCM reconstruction, and migration.

### 4. Verification Results
- **Python / pytest**: 164 passed (all tests green, including e2e and stage6/stage8 regressions).
- **Ruff**: `All checks passed!` (0 lint or style errors).
- **Rust / cargo fmt**: clean formatting.
- **Rust / cargo test**: 27 passed (unit, integration, and 1-hour synthetic capture).
- **Desktop frontend**: `npm --prefix apps/desktop run build` succeeded.
- **Git diff**: `git diff --check` clean (0 whitespace defects).
