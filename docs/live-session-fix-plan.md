# План исправлений: живая сессия интервью / Live Session Fix Plan: Live Interview

Дата / Date: 2026-09-15. Статус / Status: **B1–B4 реализованы в рабочем дереве** / **B1–B4 implemented in working tree**.
Основание: разбор живой сессии, логов воркера (`.run/logs/`), состояния очереди `jobs`
и spool-аудио. Разделы ниже сохранены как постановка задачи; принятые решения
и фактическая реализация — в разделе «Что реализовано». /
Basis: review of live session, worker logs (`.run/logs/`), `jobs` queue state, and spool audio.
Sections below preserve problem statements; accepted decisions and actual implementation are detailed in "What Has Been Implemented".

Документ фиксирует только те проблемы, которые наблюдались фактически. Для каждой
указаны проверяемый симптом, место в коде и способ проверки. Придуманных примеров
и содержимого реальных интервью здесь нет. /
This document captures only problems actually observed. For each item, verifiable symptoms, code locations, and verification methods are provided without synthetic or fabricated interview data.

---

## Что реализовано / What Has Been Implemented

| # | Изменение / Change | Файлы / Files | Проверка / Verification |
| --- | --- | --- | --- |
| A1 | `PATCH` добавлен в CORS `allow_methods` / `PATCH` added to CORS `allow_methods` | `backend/api/app.py`, `tests/test_followup_api.py` | preflight-тест на 4 desktop-origin / preflight test across 4 desktop origins |
| A2 | Матчер не отбирает ответ у уже заданного вопроса / Matcher does not reassign answer from asked question | `backend/core/matcher.py`, `tests/test_single_source_and_startup.py` | 2 регрессии падают без фикса / 2 regression tests fail without fix |
| B1 | Наводящий вопрос доступен без ответа кандидата / Guiding question available without candidate answer | `backend/api/app.py`, `backend/core/followup_generator.py`, `apps/desktop/src/components/FollowUpSuggestions.tsx`, `tests/test_followup_questions.py`, `tests/test_followup_api.py` | 4 регрессии падают без фикса / 4 regression tests fail without fix |
| B2 | Бюджет таймаутов live-STT согласован / Live STT timeout budget aligned | `backend/adapters/stt.py`, `backend/workers/pipeline.py`, `tests/test_live_stt_timeout_budget.py` | 7 тестов / 7 tests |
| B3 | Гейт качества распознавания / STT quality gate | `backend/workers/pipeline.py`, `tests/test_stt_quality_gate.py` | 29 тестов / 29 tests |
| B4 | Бейдж модели берётся из конфигурации / Model badge read from configuration | `backend/api/app.py`, `apps/desktop/src/services/api.ts`, `apps/desktop/src/screens/LiveSessionScreen.tsx`, `tests/test_api_v1.py` | API-тест + `npm run build` / API test + `npm run build` |

### Принятые решения по B1 / Decisions for B1

#### Русский
- Гейт снят только для `mode=guide`; `probe` без ответа по-прежнему ждёт ответа.
- Промпт для `guide` без ответа опирается на вопрос плана, критерии, роль и реплики
  интервьюера, и прямо требует пустой `source_refs`.
- Валидация разрешает пустой `source_refs` только в этом случае. Любая подставленная
  ссылка всё равно проверяется против сегментов кандидата, поэтому выдуманная цитата
  отклоняется как и раньше — инвариант evidence не ослаблен.
- Дедупликация по `context_hash` уже включает `decisions_history`, поэтому после
  решения по карточке повторное нажатие даёт новый запрос. Отдельный ключ не нужен.

#### English
- Gate relaxed only for `mode=guide`; `probe` without candidate answers continues to wait for an answer.
- Prompt for `guide` without candidate answer relies on plan question, criteria, role, and interviewer turns, explicitly requiring empty `source_refs`.
- Validation permits empty `source_refs` only in this specific case. Any provided reference is still validated against candidate segments, so hallucinated quotes are rejected as before — evidence invariants remain intact.
- Deduplication by `context_hash` already incorporates `decisions_history`, so clicking after card decision yields a fresh request without an extra key.

### Принятые решения по B2 / Decisions for B2

#### Русский
- Бюджет попытки: `STT_TURN_ATTEMPT_TIMEOUT_SEC = 15s` (live-реплика ≤ 12s аудио).
- `STT_TURN_MAX_RETRIES = 1`: при недоступном апстриме попытка завершается быстро
  и освобождает полосу, а повтор выполняет воркер через durable-очередь.
- Дедлайн `TRANSCRIBE_TURN` снижен 30s → 25s и теперь строго больше бюджета попытки.
  Инвариант проверяется функцией `assert_stt_turn_budget`, а не комментарием.
- Адаптер принимает `timeout_seconds` и применяет его к конкретному запросу, поэтому
  его собственный таймаут срабатывает раньше внешней отмены.

#### English
- Attempt budget: `STT_TURN_ATTEMPT_TIMEOUT_SEC = 15s` (live turn ≤ 12s audio).
- `STT_TURN_MAX_RETRIES = 1`: when upstream is unavailable, the attempt terminates quickly and frees the lane; worker retries via durable queue.
- `TRANSCRIBE_TURN` deadline reduced 30s → 25s, strictly exceeding the attempt budget. Verified by `assert_stt_turn_budget`.
- Adapter accepts `timeout_seconds` and applies it per request so internal client timeout triggers before external cancellation.

### Принятые решения по B3 / Decisions for B3

#### Русский
- Отсекаются: пустой/пунктуационный текст, служебные фразы субтитров, CJK-вывод
  при любом языке, а также короткая реплика (≤ 2.5s), в которой нет ни одного
  символа кириллицы и при этом есть латиница с диакритикой.
- На длинных репликах проверка письменности не применяется: ответ кандидата на
  другом языке — это настоящий ответ, его нельзя терять.
- Смешанный текст (русский + латинские термины) сохраняется.
- Пропуск не молчаливый: причина возвращается в результате задания (`skip_reason`)
  и пишется в лог на уровне INFO.
- Эвристики можно отключить через `NEBULA_STT_QUALITY_GATE=0`; служебные фразы
  отсекаются всегда.
- Пороговый гейт по уровню/длительности **сознательно не добавлен**: измерения
  показали перекрытие уровней настоящей речи и мусора, такой гейт отбрасывал бы
  реальные короткие ответы.

#### English
- Filtered out: empty/punctuation text, subtitle boilerplate, CJK output for any language, and short turns (≤ 2.5s) with zero Cyrillic and Latin characters with diacritics.
- For long turns, script filtering is bypassed: candidate answers in other languages are valid and must not be discarded.
- Mixed text (Russian + Latin technical terms) is preserved.
- Non-silent skipping: skip reason is returned in job outcome (`skip_reason`) and logged at INFO level.
- Heuristics can be disabled via `NEBULA_STT_QUALITY_GATE=0`; subtitle boilerplate is always filtered.
- Threshold gate on RMS level / duration was intentionally omitted: measurements demonstrated overlap between faint real speech and room noise; such gating would drop genuine short replies.

#### Исправление после замера на реальном аудио / Fix After Real Audio Measurement

#### Русский
1. **Правило «зацикленное повторение» удалено.** Реальная реплика интервьюера
   (`Раз, раз, раз, два, два, два, три, три, три.`) — это счёт вслух при проверке
   микрофона; оба STT-профиля вернули одинаковый текст. Правило отбрасывало бы такие
   реплики и реальные подтверждения (`Да, да, да, да.`).
2. **Проверка письменности сужена до латиницы с диакритикой.** Прежнее условие
   «нет кириллицы, есть латиница» отбрасывало законные короткие ответы
   (`PostgreSQL.`, `MVCC.`, `Yes.`). Теперь чужой язык определяется по не-ASCII
   символам (`ř`, `ů`, `ě`), отсутствующим в технических терминах.

#### English
1. **Loop repetition rule removed.** Real interviewer utterance (`Раз, раз, раз, два, два, два, три, три, три.`) represents microphone checking; both STT profiles agreed on text. The rule would drop such utterances and valid confirmations (`Да, да, да, да.`).
2. **Script check narrowed to diacritic Latin.** The previous rule ("no Cyrillic, has Latin") dropped valid short answers (`PostgreSQL.`, `MVCC.`, `Yes.`). Foreign language detection now relies on non-ASCII symbols (`ř`, `ů`, `ě`) absent from technical terminology.

---

## Инварианты / Product Invariants

### Русский
- Evidence оценки и подсказок берётся только из речи кандидата. Отсутствие ответа
  нельзя превращать в фиктивную цитату или в «доказательство» из реплики интервьюера.
- Пропущенный вопрос или техническая ошибка AI не превращаются в нулевой балл.
- Ручные решения не перезаписываются результатами AI или retranscription.
- Наводящий вопрос раскрывает часть решения и меняет условия оценки, поэтому его
  использование должно оставаться видимым в итоговом отчёте.

### English
- Assessment and suggestion evidence originates strictly from candidate speech. Absence of answer must not become fictitious quotes or "evidence" from interviewer turns.
- Skipped questions or AI technical errors do not default to zero score.
- Manual decisions are never overwritten by AI or retranscription results.
- A guiding question reveals part of the solution and alters evaluation context; its usage must remain transparent in the final report.

---

## A. Предшествующие изменения / Prior Base Changes

### Русский
- A1: `PATCH` добавлен в CORS `allow_methods`: без него preflight запроса «Задан» отклонялся (400), а WebView показывал `Load failed`.
- A2: `QuestionMatcher` больше не отбирает ответ у уже заданного вопроса в пользу вопроса, который ещё не прозвучал.

### English
- A1: `PATCH` added to CORS `allow_methods`: without it, preflight for "Asked" button was rejected (400) and WebView displayed `Load failed`.
- A2: `QuestionMatcher` no longer steals answers from an already-asked question in favor of an unasked question.

---

## B1. Наводящий вопрос без ответа кандидата / Guiding Question Without Candidate Response

### Русский
- **Проблема**: `mode=guide` полезен, когда кандидат молчит или затрудняется, однако ранее система блокировала генерацию из-за отсутствия реплик кандидата.
- **Решение**: Разрешена генерация `guide` без сегментов кандидата при условии формирования контекста на основе вопроса, критериев и реплик интервьюера.
- **Инвариант**: Пустые `source_refs` допустимы исключительно для наводящих вопросов без ответа.

### English
- **Problem**: `mode=guide` is useful when the candidate hesitates or is silent, yet the system previously blocked generation due to missing candidate segments.
- **Solution**: Allowed `guide` generation without candidate segments using question text, rubric criteria, and interviewer speech.
- **Invariant**: Empty `source_refs` are permitted strictly for guiding questions generated without candidate response.

---

## B2. Согласование таймаутов STT / STT Timeout Alignment

### Русский
- **Проблема**: Дедлайн `TRANSCRIBE_TURN` (30s) был меньше клиентского таймаута (60s), приводя к `CancelledError`, сжиганию очередей и накоплению отставания.
- **Решение**: Бюджет попытки сокращён до 15s с 1 ретраем внутри адаптера, а дедлайн очереди установлен в 25s.

### English
- **Problem**: `TRANSCRIBE_TURN` deadline (30s) was shorter than client timeout (60s), causing `CancelledError`, queue backlog, and live transcription latency.
- **Solution**: Attempt budget reduced to 15s with 1 retry in adapter, and worker job deadline configured to 25s.

---

## B3. Гейт качества STT / STT Quality Gate

### Русский
- **Проблема**: Тихие шумы приводили к ложным коротким сегментам и галлюцинациям, искажавшим доказательную базу.
- **Решение**: Введён `transcript_skip_reason` для фильтрации шума, субтитровых клише и аномальной диакритики.

### English
- **Problem**: Room noise generated spurious short transcript segments and hallucinations corrupting evidence.
- **Solution**: Introduced `transcript_skip_reason` to filter noise, subtitle boilerplate, and foreign diacritics.

---

## B4. Бейдж модели в UI / Model Badge in UI

### Русский
- **Проблема**: В UI отображался статичный текст `Gemini 3.8 Flash`.
- **Решение**: Значения берутся из конфигурации и API эндпоинта `/api/v1/system/status`.

### English
- **Problem**: UI displayed hardcoded `Gemini 3.8 Flash`.
- **Solution**: Values are dynamically loaded from configuration and the `/api/v1/system/status` API endpoint.

---

## Проверки / Verification

```bash
uv run ruff check .
uv run pytest -q
cargo fmt --all -- --check
cargo test --workspace
npm --prefix apps/desktop run build
git diff --check
git status --short
```
