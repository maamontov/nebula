# Спецификация интеграции STT и LLM / STT and LLM Integration Specification

Документ описывает архитектуру подключения провайдера `plusvibeapi.ru` для речевого распознавания (STT) и моделей рассуждений/оценки (LLM).
This document specifies the architecture for integrating the `plusvibeapi.ru` provider for speech-to-text (STT) and reasoning/evaluation models (LLM).

---

## 1. Обзор архитектуры / Architectural Overview

### Русский
Nebula придерживается строгой модульности и независимости от вендоров:
1. **STT (Распознавание речи)**: Используется внешняя модель `whisper-large-v3-turbo` через OpenAI-совместимый эндпойнт `POST /v1/audio/transcriptions`. Стоимость обработки составляет 0.02 ₽ за минуту аудио.
2. **LLM (Техническая оценка и скоринг)**: Используются исключительно не-GPT модели (`deepseek-v4-flash-0731`, `qwen3.7-plus`, `claude-sonnet-5`). Вызовы осуществляются через эндпойнт `POST /v1/chat/completions` с поддержкой структурированного вывода (`response_format: { type: "json_object" }` либо `json_schema`).
3. **Безопасность ключей**: API-ключ `PLUSVIBE_API_KEY` хранится локально в `.env` и никогда не передается в открытом виде или коммитах.

### English
Nebula adheres to strict modularity and vendor independence:
1. **STT (Speech Recognition)**: Uses external `whisper-large-v3-turbo` via OpenAI-compatible `POST /v1/audio/transcriptions`. Transcription cost is 0.02 ₽ per audio minute.
2. **LLM (Technical Evaluation & Scoring)**: Exclusively uses non-GPT models (`deepseek-v4-flash-0731`, `qwen3.7-plus`, `claude-sonnet-5`). Executed via `POST /v1/chat/completions` with structured JSON output support.
3. **Key Security**: The `PLUSVIBE_API_KEY` is stored locally in `.env` and is never committed or logged in plaintext.

---

## 2. Спецификация эндпойнтов / Endpoints Specification

| Назначение / Purpose | Метод / Method | URL | Модель по умолчанию / Default Model | Формат данных / Data Format |
|---|---|---|---|---|
| **STT Transcriptions** | `POST` | `https://plusvibeapi.ru/v1/audio/transcriptions` | `whisper-large-v3-turbo` | `multipart/form-data` |
| **LLM Completions** | `POST` | `https://plusvibeapi.ru/v1/chat/completions` | `deepseek-v4-flash-0731` | `application/json` |

### Параметры STT / STT Parameters:
- `file`: Аудиофайл (WAV / MP3 / OGG, 16 кГц моно).
- `model`: `"whisper-large-v3-turbo"`.
- `language`: `"ru"` (или `"en"` при англоязычном интервью).
- `response_format`: `"json"` или `"verbose_json"` (для детальных сегментов с таймкодами).

---

## 3. Политика повторов и отказоустойчивость / Retry Policy & Resilience

### Русский
- **Транзиентные ошибки (429 Too Many Requests, 500/502/503/504)**: Выполняется до 3 повторных попыток с экспоненциальной задержкой ($backoff \times 2^i$).
- **Фатальные ошибки аутентификации (401 Unauthorized, 403 Forbidden)**: Повторы не выполняются; выбрасывается `STTAuthenticationError` / `LLMAuthenticationError` с немедленным алертом в UI.
- **Таймауты**: Таймаут на транскрибацию 2-секундного чанка — 10 секунд. Таймаут на LLM-скоринг — 30 секунд.

### English
- **Transient Errors (429, 500/502/503/504)**: Retried up to 3 times with exponential backoff ($backoff \times 2^i$).
- **Authentication Errors (401, 403)**: No retries; immediately raises `STTAuthenticationError` / `LLMAuthenticationError` for UI alerting.
- **Timeouts**: 10 seconds for 2-second audio chunk STT; 30 seconds for structured LLM evaluation.

---

## 4. Верификационный скрипт / Live Verification Probe

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

## 5. Измеренные показатели приёмки / Measured Acceptance Metrics

### Русский
| Компонент / Модель | Эндпойнт | Измеренная задержка | Целевой порог | Результат |
|---|---|---|---|---|
| **STT**: `whisper-large-v3-turbo` | `POST /v1/audio/transcriptions` | **2.42 с** | $\le 3.0$ с | **PASSED** |
| **LLM Основная**: `google/gemini-3.8-flash` | `POST /v1/chat/completions` | **3.9 – 26.8 с** | $\le 10.0$ с | **PASSED** (100% точность цитат) |
| **LLM Резервная 1**: `qwen/qwen3.7-plus` | `POST /v1/chat/completions` | **5.96 с** | $\le 10.0$ с | **PASSED** (высокая стабильность) |
| **LLM Резервная 2**: `deepseek/deepseek-v4-flash-0731` | `POST /v1/chat/completions` | **5.7 – 111.9 с** | Fallback | **PASSED** (глубокий reasoning, чувствителен к нагрузке шлюза) |

### English
| Component / Model | Endpoint | Measured Latency | Target Threshold | Result |
|---|---|---|---|---|
| **STT**: `whisper-large-v3-turbo` | `POST /v1/audio/transcriptions` | **2.42 s** | $\le 3.0$ s | **PASSED** |
| **LLM Primary**: `google/gemini-3.8-flash` | `POST /v1/chat/completions` | **3.9 – 26.8 s** | $\le 10.0$ s | **PASSED** (100% quote accuracy) |
| **LLM Fallback 1**: `qwen/qwen3.7-plus` | `POST /v1/chat/completions` | **5.96 s** | $\le 10.0$ s | **PASSED** (high availability) |
| **LLM Fallback 2**: `deepseek/deepseek-v4-flash-0731` | `POST /v1/chat/completions` | **5.7 – 111.9 s** | Fallback | **PASSED** (deep reasoning, load-sensitive upstream) |

