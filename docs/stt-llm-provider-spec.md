# Спецификация интеграции STT и LLM / STT and LLM Integration Specification

Документ описывает архитектуру подключения провайдеров AI для речевого распознавания (STT) и моделей рассуждений/оценки (LLM). По умолчанию используется провайдер `routerai.ru` (с поддержкой `plusvibeapi.ru` в качестве альтернативного профиля).
This document specifies the architecture for integrating AI providers for speech-to-text (STT) and reasoning/evaluation models (LLM). By default, `routerai.ru` is configured (with `plusvibeapi.ru` supported as an alternative profile).

---

## 1. Обзор архитектуры / Architectural Overview

### Русский
Nebula придерживается строгой модульности и независимости от вендоров:
1. **STT (Распознавание речи)**: По умолчанию используется модель `openai/gpt-transcribe` через OpenAI-совместимый эндпойнт `POST /api/v1/audio/transcriptions` на шлюзе `routerai.ru`. Стоимость обработки составляет ~0.49 ₽ за минуту аудио (~0.0082 ₽/сек). Профиль выбран по точности на технической русской речи: `qwen/qwen3-asr-1.7b` искажал доменные термины, а такие строки становятся evidence оценки (см. раздел 3.1). Поддерживаются также `qwen/qwen3-asr-1.7b` (~10× дешевле) и профиль Whisper (`whisper-large-v3-turbo` на `plusvibeapi.ru`).
2. **LLM (Техническая оценка и скоринг)**: По умолчанию используется модель `qwen/qwen3.7-flash` с контекстным окном 1 000 000 токенов и поддержкой рассуждений (reasoning). Вызовы осуществляются через эндпойнт `POST /api/v1/chat/completions` с поддержкой структурированного вывода (`response_format: { type: "json_object" }`). Резервными моделями служат `qwen/qwen3.7-plus` и `deepseek/deepseek-v4-flash-0731`. Внутренние рассуждения модели (reasoning) для путей оценки и follow-up принудительно отключаются проверенным для каждой пары provider/model параметром — см. раздел 4.
3. **Безопасность ключей**: API-ключи `ROUTERAI_API_KEY` (и `PLUSVIBE_API_KEY`) хранятся локально в `.env` и никогда не передаются в открытом виде или коммитах.

### English
Nebula adheres to strict modularity and vendor independence:
1. **STT (Speech Recognition)**: By default, the `openai/gpt-transcribe` model is used via OpenAI-compatible `POST /api/v1/audio/transcriptions` on the `routerai.ru` gateway. Cost is ~0.49 ₽ per minute (~0.0082 ₽/sec). The profile was chosen for accuracy on technical Russian speech: `qwen/qwen3-asr-1.7b` garbled domain terms, and those strings become assessment evidence (see Section 3.1). The cheaper `qwen/qwen3-asr-1.7b` (~10x lower cost) and the Whisper profile (`whisper-large-v3-turbo` on `plusvibeapi.ru`) remain supported.
2. **LLM (Technical Evaluation & Scoring)**: By default, `qwen/qwen3.7-flash` is used with a 1,000,000 token context window and native reasoning support. Executed via `POST /api/v1/chat/completions` with structured JSON output support (`response_format: { type: "json_object" }`). Automatic fallback models are `qwen/qwen3.7-plus` and `deepseek/deepseek-v4-flash-0731`. Internal model reasoning is forcibly disabled on the assessment and follow-up paths via a parameter verified per provider/model pair — see Section 4.
3. **Key Security**: API keys `ROUTERAI_API_KEY` (and `PLUSVIBE_API_KEY`) are stored locally in `.env` and are never committed or logged in plaintext.

---

## 2. Спецификация эндпойнтов / Endpoints Specification

### RouterAI (Основной / Primary)

| Назначение / Purpose | Метод / Method | URL | Модель по умолчанию / Default Model | Формат данных / Data Format |
|---|---|---|---|---|
| **STT Transcriptions** | `POST` | `https://routerai.ru/api/v1/audio/transcriptions` | `openai/gpt-transcribe` | `multipart/form-data` |
| **LLM Completions** | `POST` | `https://routerai.ru/api/v1/chat/completions` | `qwen/qwen3.7-flash` | `application/json` |

### PlusVibe (Альтернативный / Alternative)

| Назначение / Purpose | Метод / Method | URL | Модель / Model | Формат данных / Data Format |
|---|---|---|---|---|
| **STT Transcriptions** | `POST` | `https://plusvibeapi.ru/v1/audio/transcriptions` | `whisper-large-v3-turbo` | `multipart/form-data` |
| **LLM Completions** | `POST` | `https://plusvibeapi.ru/v1/chat/completions` | `google/gemini-3.8-flash` | `application/json` |

### Параметры STT / STT Parameters:
- `file`: Аудиофайл (WAV / MP3 / OGG, 16 кГц моно).
- `model`: `"openai/gpt-transcribe"` (или `"qwen/qwen3-asr-1.7b"`, `"whisper-large-v3-turbo"`).
- `language`: `"ru"` (или `"en"` при англоязычном интервью).

---

## 3. Политика повторов и отказоустойчивость / Retry Policy & Resilience

### Русский
- **Транзиентные ошибки (429 Too Many Requests, 500/502/503/504)**: Выполняется до 3 повторных попыток с экспоненциальной задержкой ($backoff \times 2^i$).
- **Фатальные ошибки аутентификации (401 Unauthorized, 403 Forbidden)**: Повторы не выполняются; выбрасывается `STTAuthenticationError` / `LLMAuthenticationError` с немедленным алертом в UI.
- **Таймауты**: Таймаут на транскрибацию чанка — 60 секунд. Таймаут на LLM-скоринг — 60 секунд.

### English
- **Transient Errors (429, 500/502/503/504)**: Retried up to 3 times with exponential backoff ($backoff \times 2^i$).
- **Authentication Errors (401, 403)**: No retries; immediately raises `STTAuthenticationError` / `LLMAuthenticationError` for UI alerting.
- **Timeouts**: 60 seconds for audio chunk STT; 60 seconds for structured LLM evaluation.

---

## 4. Отключение рассуждений модели / Reasoning Switch

### Русский
Reasoning-токены генерируются последовательно и доминируют в задержке оценки. Замер на реальном
промпте `_handle_evaluate` (2026-09-15, RouterAI): `qwen/qwen3.7-flash` тратил 2604 из 3258
completion-токенов на рассуждения и 32 с на вопрос; с отключённым reasoning — 0 токенов и 6 с,
при неизменном качестве (3/3 критерия, 5/5 дословных цитат, `validate_proposal` valid).

Способ отключения **зависит от вендора и не является взаимозаменяемым**. Шлюз молча игнорирует
неизвестный ключ вместо ошибки 4xx, поэтому неверный параметр выглядит как успех, но стоит
задержки, а иногда и увеличивает число reasoning-токенов:

| Модель | Параметр | reasoning до → после |
|---|---|---|
| `qwen/qwen3.7-flash` | `{"enable_thinking": false}` | 2604 → 0 |
| `qwen/qwen3.7-plus` | `{"enable_thinking": false}` | 2619 → 0 |
| `deepseek/deepseek-v4-flash-0731` | `{"reasoning_effort": "none"}` | 1751 → 0 |
| `deepseek/deepseek-v4-flash-0731` | `{"enable_thinking": false}` | **игнорируется**, 1061 остаётся |
| `z-ai/glm-5.3-flash` | `{"enable_thinking": false}` | 0 → **1156 (регресс)** |

Поэтому capability объявлена как `ModelProfile.thinking_disable_payload` — точный JSON-фрагмент,
который мержится в тело запроса. `None` означает «никогда не отправлять». Поле заполняется
**только** после проверки конкретной пары provider/model; непроверенные профили (PlusVibe)
остаются с `None`. Зарезервированные ключи `model`, `messages`, `response_format` не могут быть
переопределены фрагментом.

Проверка (ненулевые reasoning-токены = провал с кодом 3, capability записывать нельзя):
```bash
uv run python evals/provider_probe.py --model qwen/qwen3.7-flash \
  --api-key-env ROUTERAI_API_KEY --mode json_object \
  --thinking-disable-json '{"enable_thinking": false}'
```

### English
Reasoning tokens are generated sequentially and dominate assessment latency. Measured on the real
`_handle_evaluate` prompt (2026-09-15, RouterAI): `qwen/qwen3.7-flash` spent 2604 of 3258 completion
tokens on reasoning and 32 s per question; with reasoning disabled, 0 tokens and 6 s at unchanged
quality (3/3 criteria, 5/5 verbatim quotes, `validate_proposal` valid).

The switch is **vendor-specific and not interchangeable**. The gateway silently drops an unknown key
instead of returning 4xx, so a wrong parameter looks like success while costing latency, and can even
increase reasoning tokens (see the table above).

Hence the capability is `ModelProfile.thinking_disable_payload`: the exact JSON fragment merged into
the request body. `None` means "never send". It is populated **only** after verifying the exact
provider/model pair; unverified profiles (PlusVibe) stay `None`. The reserved keys `model`, `messages`
and `response_format` can never be overridden by the fragment.

---

## 5. Верификационный скрипт / Live Verification Probe

### Русский
Для проверки доступности провайдера, валидности API-ключа и корректности цитирования evidence используется утилита:
```bash
uv run python evals/run_stage2_probe.py
```
Скрипт генерирует синтетический WAV-тон, выполняет транскрибацию, вызывает оценку фрагмента ответа кандидата и проверяет exact quotes через `EvidenceValidator`.

### English
To verify provider availability, key validity, and evidence citation integrity, use:
```bash
uv run python evals/run_stage2_probe.py
```
The script generates an in-memory synthetic WAV tone, calls transcription, evaluates candidate answer text, and validates exact quotes using `EvidenceValidator`.

---

## 3.1. Выбор STT-профиля / STT profile selection

Модель выбирается переменной `NEBULA_STT_MODEL` в `.env`:

| `NEBULA_STT_MODEL` | Профиль | Стоимость / мин |
|---|---|---|
| `openai/gpt-transcribe` (по умолчанию) | `routerai-gpt-transcribe` | ~0.49 ₽ |
| `qwen/qwen3-asr-1.7b` | `routerai-qwen3-asr-1.7b` | ~0.05 ₽ |
| `whisper-large-v3-turbo` | `plusvibe-whisper-turbo` | — |

Неизвестное имя модели не подменяется: точный upstream `model_id` передаётся как есть,
меняется только внутренний профиль-обёртка. Фабрики профилей детерминированы и не читают
окружение, поэтому выбор модели виден только в `get_default_stt_profile()`.

### Замеры / Measurements

Замерено в этом окружении на одной и той же синтетической речи (macOS TTS, 16 кГц моно):

| Аудио | `qwen/qwen3-asr-1.7b` | `openai/gpt-transcribe` |
|---|---|---|
| 4.0s | `PostgresQL для шардинга` | `PostgreSQL для шардинга базы данных.` |
| 12.0s | `обеспечиваете идempotence обработки` | `обеспечиваете идемпотентность обработки` |
| тихая речь (−34 dBFS) | `полст-грэс QL` | `PostgreSQL` |
| комнатный шум, речи нет | `No.` (выдуманное слово) | `''` |

Латентность на 4-секундном клипе (8 прогонов): `qwen3-asr-1.7b` med 0.75s, `gpt-transcribe`
med 1.34s. Обе укладываются в бюджет попытки `STT_TURN_ATTEMPT_TIMEOUT_SEC = 15s`.

Замеры сделаны на синтетической речи и шуме, не на записях интервью. На реальной тихой речи
с артефактами цифры могут отличаться.

---

## 6. Управление настройками AI-провайдеров через Desktop UI и API / Provider Settings Management via Desktop UI & API

### Русский
Nebula поддерживает динамическую независимую настройку STT и LLM провайдеров прямо из десктопного интерфейса (экран «Настройки») и через REST API без необходимости перезапуска сервисов.

#### 6.1. Архитектура разделения конфигурации и секретов
- **Не секретная конфигурация**: хранится в таблице SQLite `ai_settings` (singleton строка `id=1`) в виде JSON-конфигураций `transcription_config_json` и `text_analysis_config_json`. Включает URL, имена провайдеров, model ID, параметры reasoning, температуру, лимиты токенов, таймауты и concurrency.
- **Секреты (API-ключи)**: API-ключи **никогда не сохраняются в БД, не логируются и не возвращаются клиенту** (write-only). При сохранении настроек через UI/API ключи записываются в версионированный защищённый файл `data/provider-secrets-v<revision>.env` с правами доступа `0600` на Unix.
- **Приоритет окружения**: если ключ задан через системные переменные окружения (`NEBULA_STT_API_KEY`, `NEBULA_LLM_API_KEY`, `ROUTERAI_API_KEY`, `PLUSVIBE_API_KEY`), он имеет высший приоритет, помечается источником `process_environment` и блокируется для редактирования через UI (`editable=false`).
- **Действия с ключами (ApiKeyAction)**: поддерживаются `preserve` (сохранить текущий активный ключ), `replace` (записать новое переданное значение) и `clear` (удалить ключ из хранилища).

#### 6.2. OCC и транзакционные блокировки
- Запросы на обновление настроек (`PUT /api/v1/settings/ai`) передают `expected_revision`. При несовпадении ревизии возвращается `409 Conflict` (Optimistic Concurrency Control).
- Обновление настроек **строго блокируется (409 Conflict)**, если:
  1. В системе есть активное интервью в статусе `recording` или `paused`;
  2. В очереди задач есть задания в статусе `PENDING` или `PROCESSING`.
- Запись снапшота секретов и коммит ревизии в БД согласованы: при отказе транзакции БД подготовленный файл секрета немедленно удаляется.

#### 6.3. Горячая перезагрузка воркера (PipelineWorker Reload)
- `PipelineWorker` использует иммутабельный бандл `WorkerAiBundle` (`revision`, адаптеры STT/LLM, семафоры).
- Перед взятием каждой задачи из очереди воркер под блокировкой проверяет актуальную ревизию настроек. При изменении ревизии воркер мгновенно пересобирает бандл без перезапуска процесса.
- Семафоры STT и LLM разделены: фоновые обращения к STT не блокируют и не конкурируют с лимитами запросов к LLM.

#### 6.4. Эндпоинты API
- `GET /api/v1/settings/ai`: возвращает текущую эффективную конфигурацию, ревизию, источник (`environment` или `database`), статус ключей (`configured`, `source`, `editable`) и признак `can_update` / `update_blocker`. Значения ключей никогда не возвращаются.
- `PUT /api/v1/settings/ai`: выполняет атомарное обновление конфигурации с OCC-контролем и snapshotting секретов.
- `POST /api/v1/settings/ai/test`: проверяет подключение к STT (синтетический WAV без речи) или LLM (минимальный JSON probe) с возвратом латентности и статуса без сохранения настроек.
- `GET /api/v1/system/models`: возвращает активные провайдеры и модели из резолвера настроек с текущей `settings_revision`.

---

### English
Nebula supports dynamic, independent configuration of STT and LLM providers directly from the Desktop UI ("Settings" screen) and via REST API without requiring a service restart.

#### 6.1. Configuration and Secret Separation Architecture
- **Non-Secret Configuration**: Persisted in the SQLite `ai_settings` table (singleton row `id=1`) as JSON documents `transcription_config_json` and `text_analysis_config_json`. Holds URLs, provider identifiers, model IDs, reasoning policies, temperature, token bounds, timeouts, and concurrency caps.
- **Secrets (API Keys)**: API keys are **never stored in the database, never logged, and never returned to clients** (write-only). When saved via UI/API, secrets are committed to a versioned, restricted file `data/provider-secrets-v<revision>.env` with `0600` permissions on Unix.
- **Environment Precedence**: If credentials are provided via process environment variables (`NEBULA_STT_API_KEY`, `NEBULA_LLM_API_KEY`, `ROUTERAI_API_KEY`, `PLUSVIBE_API_KEY`), they take top precedence, are marked as `process_environment`, and cannot be modified via UI (`editable=false`).
- **Key Actions (ApiKeyAction)**: Supported actions are `preserve` (retain current active secret), `replace` (commit newly supplied secret), and `clear` (remove key from runtime store).

#### 6.2. OCC and Transactional Blockers
- Update requests (`PUT /api/v1/settings/ai`) submit `expected_revision`. A mismatch returns `409 Conflict` (Optimistic Concurrency Control).
- Settings updates are **strictly blocked (409 Conflict)** if:
  1. An interview is currently `recording` or `paused`;
  2. Any queue jobs are in `PENDING` or `PROCESSING` state.
- Secret file preparation and database commit are synchronized: if the DB transaction fails or a blocker is detected, the uncommitted secret snapshot is immediately cleaned up.

#### 6.3. Dynamic Worker Reload (PipelineWorker Reload)
- `PipelineWorker` maintains an immutable `WorkerAiBundle` containing revision, STT/LLM adapters, and concurrency semaphores.
- Before executing claimed jobs, the worker checks the active settings revision under a refresh lock. If the revision has bumped, the worker instantiates a new bundle seamlessly without application restart.
- STT and LLM semaphores are decoupled: STT streaming does not consume LLM rate limits or concurrency slots.

#### 6.4. API Endpoints
- `GET /api/v1/settings/ai`: Returns effective configuration, revision, source (`environment` or `database`), credential statuses (`configured`, `source`, `editable`), and `can_update` / `update_blocker`. Key values are never included.
- `PUT /api/v1/settings/ai`: Atomically commits configuration with OCC verification and secret snapshotting.
- `POST /api/v1/settings/ai/test`: Tests candidate STT (synthetic speech-free WAV) or LLM (minimal JSON probe) configurations and returns latency and acceptance status without persisting changes.
- `GET /api/v1/system/models`: Returns active STT/LLM models and fallback lists resolved from current settings along with `settings_revision`.
