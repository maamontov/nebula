# Отчёт о реализации: Адаптивные уточняющие и наводящие вопросы в реальном времени
# Implementation Report: Adaptive Real-Time Follow-Up Questions

[English version below](#english-version)

---

## Русский

### 1. Обзор функциональности
В систему Nebula добавлена подсистема адаптивных уточняющих и наводящих вопросов в реальном времени. Во время интервью система непрерывно анализирует поступающие ответы кандидата относительно критериев текущего вопроса плана интервью и предлагает интервьюеру контекстные подсказки:
1. **Режим зондирования (`probe`)**:
   - `clarify` (уточнение): если ответ кандидата неполон, содержит противоречия или поверхностные формулировки.
   - `deepen` (углубление): если базовый критерий раскрыт и есть возможность проверить глубинное понимание (архитектурные трейдоффы, граничные случаи).
   - Лимит: от 0 до 2 предложений.
   - Запуск: автоматический (при появлении новых кандидатских реплик с дебаунсом 3 с) или ручной (кнопка «Предложить вопросы»).
2. **Режим наведения (`guide`)**:
   - `guide` (мягкое наведение): если кандидат застрял, ушёл в оффтоп или испытывает затруднение. Вопрос помогает вернуться к теме без прямой подсказки правильного ответа.
   - Лимит: от 0 до 1 предложения.
   - Запуск: **строго ручной** (кнопка «Мягко направить» в интерфейсе).

### 2. Ключевые инварианты и архитектурные гарантии
- **Decision-support**: AI носит сугубо рекомендательный характер. Предложения находятся в статусе `suggested`, пока интервьюер явно не нажмёт «Задан», «С правкой» или «Отклонить» (`asked` / `dismissed`).
- **Изоляция речи и ролей**: контекстом генерации выступают исключительно реплики со статусом `candidate` (`dual_source` кандидатский канал или подтвержденный сегмент `shared` дорожки). Неразмеченная общая речь (`unknown`) и речь интервьюера категорически исключены из evidence кандидата.
- **Детерминизм и идемпотентность**: канонический хэш контекста `context_hash` (включая fingerprint кандидатских реплик, идентификаторы ревизий транскрипта и рубрики) предотвращает повторную генерацию одинаковых подсказок при отсутствии новых данных.
- **Оптимистический контроль конкурентности (OCC)**: решения интервьюера защищены версионированием `decision_version`. Сохранение правок и смена статусов требуют совпадения ожидаемой версии, предотвращая гонки между UI-вкладками и фоновыми процессами.
- **Безопасность и анти-дискриминация**: программная валидация LLM-ответа (`validate_followup_response`) проверяет соответствие JSON-схеме, дословное присутствие цитат кандидата (`source_refs`), блокирует prompt injection попытки и фильтрует запрещенные личные атрибуты (акцент, пол, возраст и др.).
- **Финализация**: при переводе интервью в статус `FINALIZED` список фактически заданных вопросов (`asked`) неизменяемо фиксируется в каноническом снимке `canonical_snapshot["followup_questions"]`. На этапе ревью (`ReviewScreen`) отображается плашка-предупреждение, напоминающая интервьюеру учесть факт использования наводящего вопроса при итоговой оценке.

### 3. Реализованные компоненты
1. **Контракты (`contracts/followups.py`, `contracts/domain.py`)**:
   - Перечисления: `FollowUpKind`, `FollowUpMode`, `FollowUpTrigger`, `FollowUpStatus`.
   - Модели валидации LLM: `FollowUpLLMSuggestion`, `FollowUpLLMResponse`.
   - Модели API и хранилища: `FollowUpSuggestion`, `GenerateFollowUpsRequest`, `PatchFollowUpSuggestionRequest`, `FollowUpsStateResponse`.
2. **Ядро генератора (`backend/core/followup_generator.py`)**:
   - Детерминированные функции вычисления хэша контекста и отпечатка речи кандидата (`compute_candidate_fingerprint`, `compute_context_hash`).
   - Сборщик контекста (`build_followup_context`) с фильтрацией ассоциаций других вопросов и жестким усечением контекста (12 000 символов стенограммы, 2 000 символов рубрики).
   - Формирователь изолированного промпта (`format_followup_prompt`).
   - Программный валидатор безопасности и схемы (`validate_followup_response`).
3. **База данных и репозиторий (`backend/db/schema.sql`, `backend/db/migrations.py`, `backend/db/repository.py`)**:
   - Таблицы `followup_requests` и `followup_suggestions` с индексами и каскадным удалением.
   - Миграция схемы `010_adaptive_followups.py` (целевая версия 10).
   - Атомарное создание запроса и фоновой задачи `create_followup_request_and_job`.
   - Сохранение результатов с проверкой lease-токена `save_followup_suggestions`.
   - Обновление статусов и текста с OCC: `update_suggestion_decision`.
   - Механизм повторной попытки terminal failure: `retry_followup_request`.
4. **Фоновый пайплайн (`backend/workers/pipeline.py`)**:
   - Выделенный независимый воркер-консьюмер для задач `GENERATE_FOLLOWUPS` с отдельным lease и concurrency limit = 1, гарантирующий отсутствие блокировок аудио-транскрипции (STT).
   - Полная проверка актуальности ревизий до и после выполнения LLM-запроса, таймаут выполнения 20 секунд.
5. **API (`backend/api/app.py`)**:
   - `GET /api/v1/interviews/{id}/followups`: получение текущего состояния подсказок.
   - `POST /api/v1/interviews/{id}/followups/generate`: запуск генерации подсказок (auto/manual).
   - `PATCH /api/v1/interviews/{id}/followups/{suggestion_id}`: утверждение/правка/отклонение подсказки.
   - `POST /api/v1/interviews/{id}/followups/requests/{request_id}/retry`: повтор запроса при сбое.
   - Защита общего эндпоинта `/jobs/enqueue` от ручного создания `GENERATE_FOLLOWUPS`.
6. **Desktop UI (`apps/desktop/src/...`)**:
   - Компонент `FollowUpSuggestions.tsx`: тумблер «Автоподсказки», ручные кнопки «Предложить вопросы» и «Мягко направить», плашки `clarify`, `deepen`, `guide`, редактирование по клику «С правкой», переход к цитате в стенограмме.
   - Встраивание в `LiveSessionScreen.tsx` (в правой панели над оперативной оценкой).
   - Встраивание в `ReviewScreen.tsx` (блок заданных follow-up вопросов с визуальным предупреждением об оказанной кандидату помощи).

### 4. Результаты тестирования
- **Unit & Property Tests (`tests/test_followup_questions.py`)**: 8 тестов (детерминизм хэша, фильтрация ролей, изоляция вопросов, валидация цитат, отсечение prompt injection и запрещенных персональных атрибутов).
- **Repository & DB Tests (`tests/test_followup_repository.py`)**: 6 тестов (дедупликация запросов по хэшу, атомарный claim/lease, OCC-контроль версий, retry терминальных ошибок, каскадное удаление, фиксация в `canonical_snapshot` при финализации).
- **API Integration Tests (`tests/test_followup_api.py`)**: 7 тестов (генерация, дебаунс/кеш, права доступа и статусы интервью, OCC 409 при конфликте версий, retry, блокировка создания через `/jobs/enqueue`).
- **Полная регрессия**:
  - `uv run pytest -q`: 185 passed.
  - `uv run ruff check .`: All checks passed.
  - `cargo test --workspace`: 24 passed (все тесты audio capture, clock, resampler, spool, synthetic и desktop commands).
  - `npm --prefix apps/desktop run build`: 0 ошибок сборки и типизации.

---

<a name="english-version"></a>
## English Version

### 1. Functional Overview
The Nebula system now features an adaptive real-time follow-up and guidance question engine. During live interviews, the system continuously analyzes candidate transcript turns against current rubric criteria and offers contextual prompts to the interviewer:
1. **Probe Mode (`probe`)**:
   - `clarify`: triggered when candidate answers are incomplete, ambiguous, or lacking technical specificity.
   - `deepen`: triggered when the core criterion is fulfilled, enabling verification of advanced understanding (architectural trade-offs, edge cases).
   - Capacity: 0 to 2 suggestion cards.
   - Trigger: automatic (debounced by 3 seconds on new candidate speech) or manual (via "Suggest questions" button).
2. **Guide Mode (`guide`)**:
   - `guide`: triggered when the candidate is stuck, diverging off-topic, or struggling with the problem framing. It gently steers the candidate back on track without providing the solution.
   - Capacity: 0 to 1 suggestion card.
   - Trigger: **strictly manual** (via "Gently guide" button in the UI).

### 2. Core Invariants & Architectural Guarantees
- **Decision Support Principle**: AI remains advisory. Suggestions are created in `suggested` state until the interviewer explicitly acts on them: "Asked", "Edited", or "Dismissed" (`asked` / `dismissed`).
- **Speaker Attribution & Role Isolation**: context generation strictly relies on verified `candidate` speech segments (`dual_source` candidate channel or verified `candidate` turn on `shared` tracks). Unassigned `unknown` speech and interviewer speech are strictly excluded from candidate evidence.
- **Determinism & Idempotency**: canonical context hash `context_hash` (incorporating candidate turn fingerprints, rubric revision ID, and transcript revision ID) prevents redundant LLM invocations when no new candidate input is present.
- **Optimistic Concurrency Control (OCC)**: interviewer decisions are guarded by `decision_version`. Any state mutation or inline edit requires matching expected version, preventing races across UI tabs and background processes.
- **Safety & Bias Prevention**: programmatic response validation (`validate_followup_response`) enforces JSON schema compliance, checks verbatim transcript citations (`source_refs`), neutralizes prompt injection attempts, and filters prohibited personal attributes (accent, gender, age, vocal pitch).
- **Session Finalization**: upon transitioning an interview to `FINALIZED`, all `asked` follow-up questions are immutably captured in `canonical_snapshot["followup_questions"]`. On the `ReviewScreen`, an explicit notice warns the interviewer to account for any guidance provided when determining final scores.

### 3. Implemented Components
1. **Contracts (`contracts/followups.py`, `contracts/domain.py`)**:
   - Enums: `FollowUpKind`, `FollowUpMode`, `FollowUpTrigger`, `FollowUpStatus`.
   - LLM Validation Models: `FollowUpLLMSuggestion`, `FollowUpLLMResponse`.
   - Domain & API Models: `FollowUpSuggestion`, `GenerateFollowUpsRequest`, `PatchFollowUpSuggestionRequest`, `FollowUpsStateResponse`.
2. **Core Generator Logic (`backend/core/followup_generator.py`)**:
   - Deterministic candidate speech fingerprinting and context hashing (`compute_candidate_fingerprint`, `compute_context_hash`).
   - Context builder (`build_followup_context`) with association filtering and strict truncation boundaries (12,000 chars transcript, 2,000 chars rubric).
   - Isolated prompt construction (`format_followup_prompt`).
   - Programmatic safety and schema validator (`validate_followup_response`).
3. **Database & Repository (`backend/db/schema.sql`, `backend/db/migrations.py`, `backend/db/repository.py`)**:
   - Tables `followup_requests` and `followup_suggestions` with indices and foreign-key cascade deletes.
   - Schema migration `010_adaptive_followups.py` (target schema version 10).
   - Atomic job and request enqueueing: `create_followup_request_and_job`.
   - Lease-checked result persistence: `save_followup_suggestions`.
   - Decision updating with OCC: `update_suggestion_decision`.
   - Explicit retry for terminal failures: `retry_followup_request`.
4. **Background Pipeline (`backend/workers/pipeline.py`)**:
   - Dedicated `GENERATE_FOLLOWUPS` worker loop with separate lease handling and concurrency limit = 1, ensuring zero blocking of real-time STT audio transcription.
   - Pre- and post-LLM revision freshness validation with a 20-second timeout.
5. **API Endpoints (`backend/api/app.py`)**:
   - `GET /api/v1/interviews/{id}/followups`: inspect current suggestions and request status.
   - `POST /api/v1/interviews/{id}/followups/generate`: request question suggestions (auto or manual).
   - `PATCH /api/v1/interviews/{id}/followups/{suggestion_id}`: record interviewer decision or text edit.
   - `POST /api/v1/interviews/{id}/followups/requests/{request_id}/retry`: retry failed generation request.
   - Direct enqueue protection: `/jobs/enqueue` rejects manual injection of `GENERATE_FOLLOWUPS`.
6. **Desktop Application (`apps/desktop/src/...`)**:
   - Component `FollowUpSuggestions.tsx`: auto-suggestion toggle, manual triggers ("Suggest questions", "Gently guide"), badges for `clarify`, `deepen`, `guide`, inline editing, and verbatim quote navigation into live transcript.
   - Integrated into `LiveSessionScreen.tsx` in the right column above quick assessment.
   - Integrated into `ReviewScreen.tsx` displaying asked follow-ups with an advisory penalty banner for guided questions.

### 4. Verification & Test Coverage
- **Unit & Property Tests (`tests/test_followup_questions.py`)**: 8 tests covering hash determinism, speaker filtering, question boundary isolation, quote verification, injection defense, and bias mitigation.
- **Repository Tests (`tests/test_followup_repository.py`)**: 6 tests covering hash deduplication, atomic lease claims, OCC version conflicts, error retry, cascade deletion, and immutable capture in `canonical_snapshot`.
- **API Tests (`tests/test_followup_api.py`)**: 7 tests covering generation lifecycle, caching, interview status guards, 409 conflict handling, retry semantics, and enqueue restrictions.
- **Full Regression Suite**:
  - `uv run pytest -q`: 185 passed.
  - `uv run ruff check .`: All checks passed.
  - `cargo test --workspace`: 24 passed (audio capture, clock, resampler, spool, synthetic, and desktop commands).
  - `npm --prefix apps/desktop run build`: 0 errors (TypeScript and Vite build clean).
