# Спецификация Контрактов и Доменной Модели Nebula
# Nebula Contracts & Domain Model Specification

Документ фиксирует машиночитаемые контракты данных, формулы расчета и правила валидации для системы Nebula (Этап 0 плана реализации).
This document specifies the machine-readable data contracts, calculation formulas, and validation rules for the Nebula system (Stage 0 of the implementation plan).

---

## 1. Профиль Провайдера и Модели / Provider & Model Profile

### RU
В соответствии с разделом 4 плана реализации, интеграция с LLM-провайдерами строится исключительно вокруг протокола `POST /v1/chat/completions` без предположений о моделях GPT или закрытых сервисах OpenAI:
- `ProviderProfile`: задает базовый URL, допустимые эндпоинты (для защиты от перенаправления секретов), таймауты и лимиты. Секретный ключ хранится в переменной окружения, имя которой фиксируется в профиле.
- `ModelProfile`: хранит точный идентификатор модели на стороне upstream (`upstream_model_id`), режим структурированного вывода (`json_schema`, `json_object`, `prompt_instruction`), лимиты токенов и признак поддержки скрытого reasoning (`supports_reasoning`). Внутренний текст рассуждений (`<think>`) принудительно очищается и никогда не выводится пользователю как обоснование оценки.

### EN
In accordance with Section 4 of the implementation plan, integration with LLM providers is built strictly around the `POST /v1/chat/completions` protocol without any assumptions about GPT models or closed OpenAI services:
- `ProviderProfile`: defines the base URL, allowed endpoints (preventing credential exfiltration via redirects), timeouts, and rate limits. The secret API key is resolved from an environment variable referenced by name.
- `ModelProfile`: preserves the exact upstream model identifier (`upstream_model_id`), structured output mode (`json_schema`, `json_object`, `prompt_instruction`), token budgets, and the reasoning flag (`supports_reasoning`). Internal reasoning blocks (`<think>`) are sanitized and never exposed to the user as candidate evaluation explanations.

---

## 2. Аудиочанки и Манифест Дорожек / Audio Chunks & Track Manifest

### RU
- `TrackType`: разделение на два изолированных канала — `interviewer` (микрофон интервьюера) и `candidate` (системный звук/приложение звонка).
- `AudioChunkMetadata`: атомарный дескриптор каждого записанного фрагмента на монотонной шкале времени. Включает `capture_epoch` (для различения сессий после перезапуска/паузы), `sequence`, метки времени в миллисекундах (`start_time_ms`, `end_time_ms`), формат, количество сэмплов и контрольную сумму `checksum_sha256`.
- `TrackManifest`: формируется при вызове `Stop` и опечатывается (`is_sealed`) после подтверждения загрузки всех фрагментов бэкендом.

### EN
- `TrackType`: separation into two isolated channels — `interviewer` (interviewer mic) and `candidate` (loopback / call application output).
- `AudioChunkMetadata`: atomic descriptor for every audio slice recorded against a monotonic interview timeline. Contains `capture_epoch` (to distinguish capture sequences after pause/crash recovery), `sequence`, millisecond timestamps (`start_time_ms`, `end_time_ms`), audio format, sample count, and a `checksum_sha256` digest.
- `TrackManifest`: generated upon calling `Stop` and sealed (`is_sealed`) after all chunks are verified and persisted by the backend.

---

## 3. Детерминированный Расчет Оценок / Deterministic Scoring Engine

### RU
Формула расчета строго детерминирована и исключает субъективные модельные галлюцинации:
1. **Нормализация критерия**:
   $$s_c = \frac{\text{raw\_score} - \min_c}{\max_c - \min_c} \in [0, 1]$$
   При $\max_c \le \min_c$ генерируется ошибка конфигурации шкалы.
2. **Балл по вопросу**:
   $$S_q = \frac{\sum_{c \in C_q^{active}} w_c \cdot s_c}{\sum_{c \in C_q^{active}} w_c}$$
   Если критерий явно исключен человеком (`is_excluded = True`) или не имеет оценки, он исключается из знаменателя.
3. **Итоговый балл (0–100)**:
   $$\text{FinalScore} = \left( \frac{\sum_{q \in Q^{active}} W_q \cdot S_q}{\sum_{q \in Q^{active}} W_q} \right) \times 100$$
4. **Покрытие интервью (`coverage_percentage`)**:
   $$\text{Coverage} = \left( \frac{\sum_{q \in Q^{active}} W_q}{\sum_{q \in Q^{planned}} W_q} \right) \times 100\%$$
   Исключение вопросов человеком честно снижает показатель покрытия, предотвращая скрытие неполноты интервью. Пропущенный вопрос никогда не превращается в нулевой балл.

### EN
The scoring formula is strictly deterministic and eliminates subjective model hallucinations:
1. **Criterion Normalization**:
   $$s_c = \frac{\text{raw\_score} - \min_c}{\max_c - \min_c} \in [0, 1]$$
   If $\max_c \le \min_c$, a scale configuration error is raised.
2. **Question Score**:
   $$S_q = \frac{\sum_{c \in C_q^{active}} w_c \cdot s_c}{\sum_{c \in C_q^{active}} w_c}$$
   If a criterion is explicitly excluded by the interviewer (`is_excluded = True`) or unobserved, its weight is removed from the denominator.
3. **Final Interview Score (0–100)**:
   $$\text{FinalScore} = \left( \frac{\sum_{q \in Q^{active}} W_q \cdot S_q}{\sum_{q \in Q^{active}} W_q} \right) \times 100$$
4. **Interview Coverage (`coverage_percentage`)**:
   $$\text{Coverage} = \left( \frac{\sum_{q \in Q^{active}} W_q}{\sum_{q \in Q^{planned}} W_q} \right) \times 100\%$$
   Excluding questions reduces coverage transparently without masking interview incompleteness. An unanswered question is never converted into an artificial zero score.

---

## 4. Валидация Цитат (Evidence) и Безопасность / Evidence Validation & Safety

### RU
Модель оценивания не является доверенным источником. Каждая рекомендация (`AssessmentProposal`) проходит проверку:
1. Все `segment_id` обязаны существовать в указанной `TranscriptRevision`.
2. Каждая цитата `exact_quote` программно проверяется на дословное присутствие в тексте сегмента кандидата.
3. Обоснования (`explanation`) сканируются на попытки prompt injection (например, «поставь максимальный балл»).
4. Обоснования сканируются на запрещенные нерелевантные признаки кандидата (акцент, тембр, скорость речи, пол, возраст, национальность). Обнаружение таких признаков блокирует утверждение оценки.

### EN
The evaluation model is not a trusted source. Every recommendation (`AssessmentProposal`) undergoes strict verification:
1. All `segment_id` entries must exist in the referenced `TranscriptRevision`.
2. Every `exact_quote` is programmatically verified to match the candidate segment text verbatim.
3. Explanations (`explanation`) are scanned for prompt injection attempts (e.g. "give maximum score").
4. Explanations are scanned for forbidden non-professional candidate traits (accent, timbre, speech rate, gender, age, nationality). Presence of these triggers blocks proposal approval.

---

## 5. Жизненный Цикл Интервью / Interview Lifecycle

### RU
Конечный автомат сессии строго контролирует допустимые переходы:
```text
DRAFT → READY → RECORDING ↔ PAUSED → PROCESSING → REVIEW → FINALIZED
  ↓       ↓         ↓          ↓          ↓          ↓         ↓
───────────────────────────→ DELETED ←───────────────────────────
```
- Состояние `DELETED` является терминальным; любые фоновые задачи или прием аудиочанков для удаленного интервью немедленно прекращаются.
- Завершение (`FINALIZED`) фиксирует неизменяемый снимок отчета (`ReportRevision`).

### EN
The state machine strictly governs valid transitions:
```text
DRAFT → READY → RECORDING ↔ PAUSED → PROCESSING → REVIEW → FINALIZED
  ↓       ↓         ↓          ↓          ↓          ↓         ↓
───────────────────────────→ DELETED ←───────────────────────────
```
- The `DELETED` state is strictly terminal; any background processing or chunk ingestion for a deleted session is immediately rejected.
- `FINALIZED` creates an immutable report snapshot (`ReportRevision`).
