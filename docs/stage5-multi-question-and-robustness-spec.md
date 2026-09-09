# Спецификация Этапа 5: Полное live-интервью (Multi-Question Live Session & Robustness)
# Stage 5 Specification: Multi-Question Live Session and Robustness

---

## 1. Обзор и архитектура этапа / Overview and Architecture

### Русский
Этап 5 расширяет систему от вертикального среза одного вопроса до полноценного живого собеседования с комплексной динамикой речи и защитой от сбоев в соответствии с Разделами 6, 7, 8, 10 и 11 плана реализации ([`docs/implementation-plan.md`](file:///Users/wital/dev/nebula/docs/implementation-plan.md)).

Ключевые механизмы:
1. **Многовопросный план и динамика тем**:
   - Поддержка плана из произвольного числа вопросов с индивидуальными весами и критериями.
   - Отслеживание активного вопроса в реальном времени.
2. **Интеллектуальная привязка речи к плану (Question Matcher)**:
   - Автоматическая группировка реплик кандидата по вопросам.
   - Распознавание уточнений (Follow-up / Clarification questions): короткие реплики интервьюера («А что насчет C-расширений?») не сбрасывают текущий вопрос, а привязываются как контекст к активному вопросу.
3. **Детекция перебиваний (Speech Interruption)**:
   - Анализ временных интервалов дорожек `interviewer` и `candidate`.
   - Если один участник начинает говорить до завершения реплики другого (наложение $\ge 250$ мс), фиксируется событие перебивания с точной длительностью наложения в миллисекундах.
4. **Принцип открытой неоднозначности (Ambiguity is never hidden)**:
   - Если ответ кандидата уходит от темы или релевантность критериям ниже порога, система **не угадывает привязку скрытно**, а выставляет флаг `is_ambiguous = True` с понятным объяснением.
   - Интервьюер может в один клик вручную перепривязать реплику через API/UI (`POST /api/v1/interviews/{id}/segments/{segment_id}/associate`).
5. **Отказоустойчивость AI и автоматический Fallback**:
   - `ResilientLLMAdapter` обеспечивает прозрачное переключение моделей при 429, 5xx, таймауте или сетевом сбое:
     - **Первичная**: `google/gemini-3.8-flash`
     - **Резервная №1**: `qwen/qwen3.7-plus`
     - **Резервная №2**: `deepseek/deepseek-v4-flash-0731`
   - Событие переключения (`MODEL_FALLBACK_TRIGGERED`) записывается в иммутабельный аудит-лог.
6. **Мониторинг здоровья аудиоканалов (Audio Health Monitor)**:
   - Детекция тишины (RMS < 0.005 > 30 с) и клиппинга (Peak >= 0.98).
   - Эндпойнт `GET /api/v1/interviews/{id}/health`.

### English
Stage 5 expands the solution from a single-question vertical slice into a production-grade multi-question interview session with real conversational dynamics and fault tolerance according to Sections 6, 7, 8, 10, and 11 of the implementation plan ([`docs/implementation-plan.md`](file:///Users/wital/dev/nebula/docs/implementation-plan.md)).

Key capabilities:
1. **Multi-Question Plan & Topic Tracking**:
   - Dynamic tracking of multiple planned questions with distinct weights and criteria rubrics.
2. **Question-to-Transcript Matcher**:
   - Automated grouping of candidate turns under corresponding questions.
   - Follow-up & clarification detection: short interviewer prompts (e.g. "What about C-extensions?") remain linked to the active question context.
3. **Interruption Detection**:
   - Cross-track timestamp overlap detection between `interviewer` and `candidate`.
   - Overlaps $\ge 250$ ms are recorded with exact millisecond duration.
4. **Principle of Explicit Ambiguity (Ambiguity is never hidden)**:
   - If a candidate's answer is ambiguous or strays from the criteria, the engine flags `is_ambiguous = True` rather than silently guessing.
   - Reviewers can manually re-link segments via API/UI (`POST /api/v1/interviews/{id}/segments/{segment_id}/associate`).
5. **Resilient AI Pipeline with Transparent Model Fallback**:
   - Automatic model failover on rate limits, HTTP 5xx, or network drops:
     - **Primary**: `google/gemini-3.8-flash`
     - **Secondary**: `qwen/qwen3.7-plus`
     - **Tertiary**: `deepseek/deepseek-v4-flash-0731`
   - Emits `MODEL_FALLBACK_TRIGGERED` audit event.
6. **Audio Channel Health Monitoring**:
   - Prolonged silence (RMS < 0.005 > 30s) and clipping detection (Peak >= 0.98).
   - Dedicated health status endpoint `GET /api/v1/interviews/{id}/health`.

---

## 2. Метрики приёмки Этапа 5 / Acceptance Metrics

| Область проверки | Целевой критерий | Результат тестирования | Статус |
|---|---|---|---|
| **Multi-question Plan** | $\ge 3$ вопросов с весами | **3 вопроса (Linux, GIL bypass, Saga)** | **PASSED** |
| **Interruption Detection** | Точная фиксация наложения $\ge 250$ мс | **1500 мс наложение зафиксировано** | **PASSED** |
| **Clarification Matching** | Связывание коротких уточнений | **Привязано к контексту вопроса #2** | **PASSED** |
| **Ambiguity Flagging** | Явный флаг `is_ambiguous = True` | **100% фиксация сомнительных реплик** | **PASSED** |
| **Manual Re-association** | Ручная корректировка связей | **Успешно через API / UI** | **PASSED** |
| **AI Resilient Fallback** | Gemini 3.8 Flash $\to$ Qwen 3.7 Plus | **Бесшовное переключение с записью в аудит** | **PASSED** |
| **Audio Health** | Мониторинг тишины и клиппинга | **Отчет доступен через эндпойнт /health** | **PASSED** |
| **Сводный скоринг** | 100-балльная шкала по всем вопросам | **90.0 / 100 с подтверждением человека** | **PASSED** |
