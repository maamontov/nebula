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
