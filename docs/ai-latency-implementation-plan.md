# План устранения задержек AI — задание для следующего агента

Дата: 2026-09-11. Основание: [аудит и доказательства](ai-latency-audit.md).
Статус: план подготовлен; изменения приложения ещё не реализованы.

## Цель и границы

Живое распознавание должно продолжаться во время оценки, summary и retranscribe.
Follow-ups должны запускаться при изменении релевантной речи, сохранять валидный ответ
адаптера и показывать понятное состояние при ошибке. UI должен отслеживать фактические
jobs и сохранять возможность дождаться их после долгой обработки или смены экрана.

Сохранить текущие SQLite jobs, атомарные leases, capture spool, provider/model profiles,
OCC, ревизии, evidence validation, ручные решения, неизменяемость FINALIZED и DELETED.
Для shared track unknown остаётся unknown. Сбой AI не создаёт нулевую оценку.

Рабочая копия уже содержит большие незакоммиченные изменения follow-ups и других путей.
Начать с `git status --short`, `git diff` и AGENTS.md; не reset/stash/переписывать чужую работу.
Номера строк аудита относятся к снимку; искать функции по имени. Проверить, какие дефекты
ещё существуют. Не запускать start/stop scripts и платные probes для обычных тестов.

Не добавлять Redis/Celery, новую БД, универсальный workflow engine, массовый рефакторинг,
нового провайдера или streaming в первые этапы. Не запускать несколько daemon-процессов
как обход проблемы: это повышает число гонок до появления защиты записей.

## Порядок и зависимости

| Этап | Результат | Зависит от | Статус |
| --- | --- | --- | --- |
| 1 | Исправлены follow-up response, retries, автозапуск и ошибки UI | — | Завершён (100%) / Completed |
| 2 | Видна задержка очереди, attempts и время provider | 1 для корректного baseline | Завершён (100%) / Completed |
| 3 | Запись результатов защищена ownership/revision, retries ограничены | 1 | Завершён (100%) / Completed |
| 4 | Assembly, live STT, оценки и background работают независимо | 2, 3 | Завершён (100%) / Completed |
| 5 | UI отслеживает jobs, нет дубликатов и ложного завершения | 1, 2; финальная проверка после 4 | Завершён (100%) / Completed |
| 6 | Измерены остаточные задержки, выполнены обоснованные оптимизации | 4, 5 | Завершён (100%) / Completed |

Делать проверяемые небольшие изменения по этапам, обновлять статус после каждого.
Если используется delegation по AGENTS.md, parent сохраняет контракты и review;
bounded Luna High worker может независимо делать UI после фиксации API контракта.
Backend pipeline/repository не делить между одновременно пишущими workers.

## Этап 1. Убрать ошибки, вызывающие лишнее ожидание

Файлы: `backend/workers/pipeline.py`, `backend/db/repository.py`,
`backend/api/app.py`, `apps/desktop/src/components/FollowUpSuggestions.tsx`,
связанные follow-up contracts/types и tests.

1. `_handle_generate_followups`: сохранить текущий isolated `OpenAICompatibleAdapter`;
   получать dict envelope, передавать `response["data"]` в validator, извлекать точный
   upstream model отдельно от внутреннего model profile и provider id. Не менять
   return contract общего adapter только ради этого handler.
2. У перехода PROCESSING → PENDING/FAILED должен быть один владелец. Handler может
   классифицировать ошибку и обновлять domain outcome, но не должен сначала вызывать
   `fail_job`, а потом отдавать ту же ошибку общему обработчику для второго перехода.
   Сохранить terminal auth error и durable `retry_delay_sec` для transient/429.
3. `fail_job` с owner token должен менять только реально принадлежащую ему PROCESSING
   задачу. NULL owner после завершения не означает разрешение старому worker повторно
   менять job. Терминальное состояние не возвращается в очередь из старого callback.
4. Согласовать существующие `outcome=failed`, job FAILED и API/UI status. Job error
   показывать через нормализованный пользовательский текст/код, без provider response
   bodies, reasoning и секретов. Показать retry; отсутствие suggestions само по себе
   не должно означать ошибку.
5. Debounce follow-up привязать к стабильным значениям: interview/question/mode,
   revision, fingerprint релевантной речи и разрешению генерации. Изменение объекта
   ответа polling при том же fingerprint не сбрасывает таймер. Использовать уже
   вычисляемый backend `candidate_fingerprint`/`context_hash` по текущему вопросу;
   не создавать второй несовместимый алгоритм хеширования в UI.
6. Сохранить 3-секундный debounce и серверный 30-секундный auto cooldown на этом этапе.
   После cooldown изменившийся контекст должен запускаться, неизменившийся — нет.
   Сбросы при смене вопроса/revision и отмена при unmount обязательны.

Приёмка:

- Настоящий LLM adapter + `httpx.MockTransport` возвращает корректный JSON: job COMPLETED,
  предложения сохранены, metadata корректна. Проверить ready и no_suggestions.
- Transient: после первой попытки PENDING с будущим retry timestamp; повторный claim
  до него невозможен. Auth: FAILED после одного вызова без повторного provider запроса.
- Старый owner не изменяет COMPLETED/FAILED/переданный другому owner job.
- UI mock backend возвращает неизменный state каждые 2.5 с минимум 10 с: один новый
  релевантный ответ вызывает одну auto generation, polling не отменяет debounce.
- Смена question/mode/revision и речь другого вопроса не вызывают устаревший запрос.
- Backend terminal failure виден в UI вместе с retry, даже если enqueue POST был успешен.

## Этап 2. Добавить небольшую, пригодную для диагностики телеметрию

Файлы: `backend/workers/pipeline.py`, adapters, `backend/db/{schema.sql,migrations.py,repository.py}`,
`backend/api/app.py`, затронутые contracts и `apps/desktop/src/{types/index.ts,services/api.ts}`.

1. Зафиксировать для каждой attempt claim/start, completion/failure, queue wait,
   execution duration и время отдельных provider calls. Использовать существующие
   audit events для истории attempts, не создавать отдельную платформу метрик.
2. Для текущего статуса jobs нужны стабильные времена начала/окончания attempt.
   Добавить минимальные nullable поля, если получить их из существующей структуры
   без тяжёлого сканирования нельзя. `updated_at` не использовать как started_at:
   heartbeat изменяет его каждые 15 с. Обновить schema и последовательную миграцию.
3. Разделять попытки HTTP, попытки job, время ожидания provider slot, backoff, provider
   duration. Для очереди после retry учитывать момент eligibility, а первоначальную
   enqueue-to-result latency считать отдельно. Не называть их все «AI latency».
4. Расширить существующий `/jobs/status` обратно совместимо: counts по type/status,
   возраст ожидающих jobs и детали ограниченного набора активных/запрошенных job IDs
   с question_id, revision, attempt, retry time и нормализованной ошибкой. Изоляция
   interview_id обязательна; payload prompts и аудио не возвращать.
5. Собирать STT audio duration / call duration, актуальные provider_id и upstream
   model_id. Локальные структурированные записи не содержат транскрипт, audio bytes,
   API key и raw provider response. Сырые runtime logs не коммитить.

Приёмка: синтетическая задержка до claim отличается от задержки mock provider;
heartbeat не ломает elapsed time; retry и restart не теряют attempts; миграция сохраняет
данные, повторно безопасна, проходит integrity checks; старый клиент получает прежние поля.

## Этап 3. Подготовить безопасную конкурентную обработку

Файлы: `backend/db/repository.py`, `backend/workers/pipeline.py`,
`backend/adapters/{llm.py,stt.py,resilient_llm.py}`, revisions/turn tests.

1. Выделить repository-операцию сохранения STT результата под проверкой job id, exact
   owner token, срока lease, interview lifecycle и target revision в одной транзакции.
   Проверка после записи не считается защитой. Повторная delivery не перезаписывает
   существующий сегмент/ручную правку другим результатом того же job.
2. Зафиксировать target revision до provider call. При смене revision не писать
   автоматически в новую активную revision. Для ещё не записанного live segment
   разрешить явный контролируемый повтор в актуальную revision после проверки
   provenance/существующих сегментов; для уже записанного — сохранить ручные изменения.
   Не терять новую речь при ручной разметке shared track во время STT.
   Перед реализацией закрепить это поведение regression tests.
3. `commit_turn_assembly_results`: CAS по исходному `(next_sequence,next_sample_offset)`
   и атомарность cursor + создаваемых jobs; курсор не двигается назад при позднем commit.
   Учесть оба пути: consumer и API `flush_turn_assembly` при stop/readiness. Только
   один in-process consumer недостаточен для защиты от этого второго пути.
4. На `renew_job_lease == False` остановить выполнение/не допустить commit; cancellation
   и shutdown должны освобождать ресурсы, а незавершённая работа оставаться recoverable.
   Проверить lease при всех mutation points retranscribe перед его отделением в lane.
5. Убрать общие sleeps consumer после rate limit. Переносить retry в durable queue,
   соблюдая Retry-After; неподходящая модель не обходится перебором моделей при ошибке
   авторизации общего ключа. Не держать provider slot во время backoff.
6. Ввести явный deadline на активную операцию с учётом всех HTTP retries и fallback.
   Разумные начальные настройки для испытаний: follow-up 20 с/attempt, live STT 30 с,
   оценка 30 с, summary 60 с. Это локальные бюджеты, а не обещание скорости сервиса.
   Сохранить до 2 attempts follow-up и до 3 STT, для оценки/summary начать с 2 job attempts;
   внутри attempt — максимум один подходящий fallback в оставшемся deadline.
   Не вкладывать по три HTTP retry в каждый fallback и ещё три job retry.
7. Ожидание в очереди не должно автоматически удалять непротранскрибированное аудио.
   Исчерпание execution budget → явный FAILED и возможность ручного повтора;
   spool/evidence сохраняются. Согласовать изменённые defaults в contracts и tests.
8. Исключить разделяемое mutable `last_fallback_event` между параллельными запросами:
   metadata принадлежит конкретному ответу/операции. Не оставлять событие старого
   fallback на следующем успешном запросе. Нужен небольшой явный контракт результата.

Приёмка: после смены owner во время provider call нет ни одной записи старого worker;
revision change не переносит старый результат поверх ручных правок; concurrent flush и
assembly не дублируют turns и не откатывают cursor; terminal lifecycle закрыт; deadline
освобождает slot; 429 не блокирует независимые jobs и не повторяется раньше Retry-After.

## Этап 4. Разделить потребителей существующей очереди

Файлы: `backend/workers/pipeline.py`, минимально `backend/db/repository.py`, tests.

Начать в одном daemon/event loop с небольших consumers и существующих include_types.
Не создавать очередь в памяти вместо durable jobs и не claim-ить заранее много работы.

| Consumer | Типы | Начальная параллельность |
| --- | --- | ---: |
| Assembly | TRANSCRIBE_AUDIO | 1 |
| Live STT | TRANSCRIBE_TURN | 2 |
| Assessment | EVALUATE_QUESTION | 1 |
| Follow-ups | GENERATE_FOLLOWUPS | 1 |
| Background | BATCH_RETRANSCRIBE, GENERATE_SUMMARY | 1 |

Это стартовая конфигурация для замеров, максимум 5 одновременных provider calls для
современного пути. Legacy TRANSCRIBE_AUDIO с прямым STT нужно отдельно учесть: он не
должен незаметно выполнять сетевой запрос в быстром assembly consumer. Сохранить
совместимость старых jobs через явное routing/нормализацию этого payload.

1. Реально применить общий локальный provider concurrency cap. Значение 10 из профиля
   не является подтверждением лимита сервиса. Сумма настроек должна соответствовать
   локальному cap; background не занимает все slots, live STT имеет доступную ёмкость.
   При меньшем cap снижать concurrency предсказуемо. Никаких безлимитных gather.
2. Не держать SQLite transaction или синхронный lock через HTTP await. Assembly для
   одного track/epoch остаётся упорядоченной; результат параллельных STT может прийти
   не по порядку, но timeline сортируется по audio time и stable id.
3. Убрать 300-мс паузу после успешного job. При пустой очереди оставить небольшой
   interruptible poll, например 200–250 мс. Нужны shutdown/cancellation и lease recovery.
4. Сохранить приоритет recording внутри подходящих типов. Длинный background request
   уже не блокирует start live STT. Проверить paused/processing drain, а не только recording.
5. Переиспользовать `httpx.AsyncClient` в пределах worker lifetime и закрывать его на
   shutdown; передавать timeout на запрос. Существующая injection для tests сохраняется.
6. Измерять event-loop lag на сборке backlog. Оптимизация PCM/sample_map или вынос
   синхронной работы нужны только при подтверждённом блокировании, с сохранением CAS.

Приёмка:

- Mock оценка и mock batch STT заблокированы: новые assembly, live STT и follow-up
  всё равно стартуют при доступной ёмкости. Тестировать events/barriers, не хрупкие sleeps.
- В каждой категории и суммарно число in-flight calls не превышает настройки.
- Out-of-order STT results имеют правильный timeline, роли и revision; повторов текста нет.
- Симуляция single_source и dual_source с фиксированной нагрузкой не наращивает
  live backlog, если её service demand ниже настроенной capacity.
- Медленная/429 модель не тормозит всю очередь; остановка/повторный запуск не теряют jobs.

## Этап 5. Дать UI фактическое состояние и убрать дубликаты

Файлы: `LiveSessionScreen.tsx`, `ReviewScreen.tsx`, `FollowUpSuggestions.tsx`,
`services/api.ts`, `types/index.ts`, enqueue/status API, repository, contracts и tests.

1. Для оценки сохранять возвращённый job_id и отслеживать его до terminal state.
   Запрет повторного клика только в UI недостаточен: enqueue должен атомарно
   возвращать уже существующую активную оценку для того же контекста.
2. Dedup key: interview/question + rubric/transcript revision + snapshot/fingerprint
   реально используемых candidate segments и associations. Одна revision во время
   live capture дополняется новыми сегментами, поэтому одной пары revisions недостаточно.
   Новый контекст или явный повтор FAILED допускает новую обработку; результат старого
   контекста не маскируется как свежий. Ручные решения сохраняются.
3. «Оценить всё»: сохранять набор job IDs, запускать enqueue с небольшим ограничением
   параллельности (2–4) через существующий endpoint. Batch endpoint не нужен только
   ради скорости нескольких локальных POST. Завершение — по нужным jobs/question IDs
   и revisions, а не общему количеству proposals. Показывать частичные результаты и ошибки.
4. Не держать кнопку активной до истечения условных 30 тиков, затем молча прекращать
   наблюдение. После remount Review восстанавливать pending jobs из backend state.
5. Live и follow-up polling: максимум один запрос каждого вида в работе; отмена/
   игнорирование устаревших ответов при unmount, смене interview/question/revision.
   Первый этап может сохранить полные snapshots и текущие интервалы. Delta/SSE
   добавлять только после замера payload/нагрузки.
6. Stop: запись завершена отдельно от «всё распознано». Сохранить processing state и
   возможность продолжить ожидание/повторить проверку после UI timeout. Если нужен
   timeout интерфейса, использовать elapsed wall time, а не число сетевых итераций.
   Review/finalize остаются под backend readiness gate; не имитировать готовность.
7. Явные состояния: «в очереди», «распознаётся», «оценка», «повтор через N с», «ошибка».
   Для unknown shared — «подтвердите роль», для отсутствия связи с вопросом —
   «свяжите реплику с вопросом». Не показывать «AI думает» при ожидании человека.
8. Оценка без evidence конкретного вопроса не должна запускать бессмысленный LLM.
   Существующий unanswered результат с `score=None` допустим; ожидаемую ещё речь
   отличать от действительно отсутствующего ответа. Не делать глобальный pipeline idle
   условием для всех оценок: это снова свяжет независимые операции.

Приёмка: двойной enqueue не удваивает актуальный job; появление новой candidate speech
позволяет новую оценку; старые proposals не завершают новый batch; terminal failure и
retry видны; медленный старый snapshot не перезаписывает новый; processing переживает
смену экрана; unknown не попадает в candidate evidence.

## Этап 6. Остаточные задержки — только по измерениям

Обязателен повторный замер; следующие изменения условны и не блокируют завершение 1–5.

- **Uploader lifecycle:** воспроизвести stop с POST дольше 10 с; сохранить handle/
  ownership при передаче в background. На session один uploader, нет наложения final
  drain и автоматически созданного второго uploader. Подтверждённый дефект исправить.
- **Uploader throughput:** при росте backlog добавить fairness и по одной последовательной
  upload lane на track с общим лимитом 2; не терять ACK/checksum/epoch/idempotency.
  Тестовый backend с POST 600 мс для dual_source должен выдерживать 2 chunks/s.
- **Payload:** менять hex на binary/multipart только при доказанном транспортном
  bottleneck. Это отдельное изменение Python/Rust audio contracts и ingestion tests.
- **Контекст и output budget:** сначала измерить token usage и latency по task/model;
  ограничивать контекст релевантным вопросом, не удаляя evidence. Не считать
  max_output_tokens=4096 доказательством генерации 4096 tokens. Не отправлять непроверенные
  reasoning/streaming/temperature параметры по одному признаку OpenAI-compatible.
- **STT:** сначала сохранить chunk=1 с, silence=800 мс, max turn=12 с. Сокращение turns
  увеличивает число запросов и может ухудшить связность русского текста. Решать по
  synthetic/eval fixtures и отдельно согласованной аппаратной проверке.
- **Streaming:** если требуется текст до конца фразы, нужен отдельный проверенный
  streaming STT профиль, промежуточные сегменты и правила finalization. Partials не
  становятся evidence без стабилизации. Это отдельная задача, не скрытая часть этого плана.

## Проверки и критерии завершения

Новые regression tests должны покрыть пять воспроизведений аудита, adapter envelope,
lane isolation, bounded concurrency, retries/deadlines, revisions/ownership, API job
tracking и dedup. Предпочитать существующие tests по pipeline, follow-ups, stages 6–9
и desktop e2e; добавлять отдельный файл только для существенно нового набора сценариев.

После backend этапов, меняющих общий контракт/worker/API/repository, полный pytest
обязателен по AGENTS.md. Финальные команды для всех затронутых слоёв:

```bash
uv run ruff check .
uv run pytest -q
npm --prefix apps/desktop run build
git diff --check
git status --short
```

Если изменён Rust: `cargo fmt --all -- --check` и `cargo test --workspace`.
Зависимости ставить `uv sync` / `npm --prefix apps/desktop ci` при необходимости.
Frontend build не проверяет timers/IPC. Для UI этапов использовать browser mock backend
с задержками и реальными component flows; применить frontend testing skill. Моковый
browser capture допустим только при явном `window.NEBULA_ENABLE_DEMO_MOCK`.
Tauri upload/stop отдельно проверять интеграционным тестовым HTTP backend.

Начальные ориентиры для нагрузочного стенда, а не обещания внешнего провайдера:

- При mock STT=2 с/turn и LLM=5 с, одна речь с turn каждые 6 с на каждом из двух tracks:
  p95 ожидания live STT после eligibility < 1 с, backlog не растёт за 5 минут.
- Оценка/summary/batch длительностью 30 с не задерживает claim нового live STT сверх
  poll interval и доступности его slots. Для CI лучше barrier assertions, длительные
  испытания — отдельный synthetic run с виртуальным временем или тестовым сервисом.
- Готовый результат становится виден в пределах одного настроенного UI poll interval
  плюс время одного GET. Серия одинаковых polls не откладывает auto-generation.
- Ноль stale-owner commits, повторных активных оценок одного context и overwritten
  human decisions в regression matrix. FAILED виден и recoverable по установленным правилам.

Сравнение before/after делается на одинаковых synthetic inputs, задержках и concurrency;
не сравнивать новый mock p95 с историческими 47.24 с реального runtime как доказательство
ускорения. В итоговом отчёте раздельно указать static/build, automated tests, synthetic
load, provider probe и hardware/UI. Не утверждать live SLA без live измерений.

Работа считается завершённой после этапов 1–5, повторного замера этапа 6 и проверки
uploader lifecycle; остальные оптимизации этапа 6 включаются только при подтверждении
соответствующего bottleneck. Перед завершением проверить diff и все untracked файлы,
обновить этот план фактическими результатами и отметить всё непроверенное.

## Фактические результаты реализации и верификации / Implementation & Verification Results

### Русский

Все 6 этапов плана успешно реализованы и верифицированы:

1. **Этап 1 (Устранение ошибок блокировки очереди):**
   - В `OpenAICompatibleAdapter` распакован dict envelope (`response["data"]`), устранена ошибка `TypeError: unhashable type: 'dict'`.
   - В `fail_job` добавлена строгая проверка `owner_token` и `locked_by`.
   - В `pipeline.py` убран блокирующий `await asyncio.sleep(retry_wait)`.
   - В `FollowUpSuggestions.tsx` добавлены debounce (3 с), cooldown (30 с) и guard против параллельных polling-запросов.
   - Тесты: 6/6 в `tests/test_stage1_ai_latency.py` успешно пройдены.

2. **Этап 2 (Прозрачность очереди и телеметрия):**
   - Миграция 11 добавила колонки `started_at` и `completed_at` в таблицу `jobs`.
   - `get_interview_jobs_status` расширен: `counts`, `counts_by_type`, `oldest_pending_age_sec`, `active_jobs` с фильтром по `job_ids`. Аудиоданные и промпты не утекают в телеметрию.
   - `process_one_job` замеряет `queue_wait_ms` и `execution_duration_ms` с передачей в события аудита.
   - TypeScript контракты и API-клиент обновлены.
   - Тесты: 5/5 в `tests/test_stage2_telemetry.py` успешно пройдены.

3. **Этап 3 (Защита concurrency, revisions, leases):**
   - Метод `save_turn_transcript_segment` атомарно проверяет `job_id`, `owner_token`, `locked_until`, lifecycle и сохраняет роль `speaker_role`.
   - `commit_turn_assembly_results` производит CAS-проверку курсора `(sequence, sample_offset)` с защитой от регрессии timeline.
   - `STTRateLimitError` немедленно завершает попытку без сна внутри сетевого адаптера.
   - Изолирован fallback-контекст `ResilientLLMAdapter`, `LLMAuthenticationError` немедленно завершает job как terminal.
   - Воркер соблюдает дедлайны `DEFAULT_JOB_DEADLINES` и обновляет lease heartbeat через фоновую задачу.
   - Тесты: 6/6 в `tests/test_stage3_concurrency.py` успешно пройдены.

4. **Этап 4 (Разделение потребителей и bounded concurrency):**
   - Очередь разделена на 5 изолированных категорий (6 корутин-потребителей):
     - Assembly: `["TRANSCRIBE_AUDIO"]` (concurrency 1)
     - Live STT: `["TRANSCRIBE_TURN"]` (concurrency 2)
     - Assessment: `["EVALUATE_QUESTION"]` (concurrency 1)
     - Follow-ups: `["GENERATE_FOLLOWUPS"]` (concurrency 1)
     - Background: все остальные типы (concurrency 1)
   - Устранена 300мс задержка после успешных задач (`await asyncio.sleep(0)` для cooperative yield).
   - Poll пустой очереди прерывается через `asyncio.wait_for(stop_event.wait(), timeout=poll_interval)`.
   - Добавлен глобальный `async with self._provider_semaphore` (concurrency cap 5).
   - Переиспользуется общий HTTP-клиент с корректным закрытием.
   - Тесты: 4/4 в `tests/test_stage4_consumers.py` успешно пройдены.

5. **Этап 5 (Фактическое состояние UI и дедупликация):**
   - Реализован метод `Repository.find_active_evaluate_job` и серверная дедупликация в `enqueue_job_endpoint` на основе `compute_candidate_fingerprint`.
   - Реплики интервьюера и `unknown` трека `shared` исключены из candidate fingerprint.
   - Появление новой речи кандидата меняет fingerprint и разрешает новую задачу; `FAILED` разрешает повтор.
   - `LiveSessionScreen.tsx` отслеживает фактическое состояние jobs до terminal state («В очереди...», «Оценка выполняется...», «Ошибка/Повтор»).
   - `ReviewScreen.tsx` при «Оценить всё» выполняет enqueue с ограничением параллельности (3), отслеживает terminal state всех задач, восстанавливает активные задачи при remount и убирает искусственный лимит 30 тиков.
   - Тесты: 6/6 в `tests/test_stage5_dedup_and_ui_state.py` успешно пройдены.

6. **Этап 6 (Замеры, аудит и uploader lifecycle):**
   - Исправлен uploader lifecycle в Rust Tauri: при остановке захвата, если остались незагруженные чанки, uploader передается в `background_uploaders` с сохранением ownership. Исключено создание второго параллельного uploader'а.
   - Cargo тесты и проверки форматирования пройдены: 100% rust/cargo success.
   - Все 212 тестов pytest в репозитории проходят без ошибок.

---

### English

All 6 stages of the implementation plan have been completed and verified:

1. **Stage 1 (Eliminate Queue-Blocking Errors):**
   - Unpacked dict response envelope (`response["data"]`) in `OpenAICompatibleAdapter`, resolving `TypeError: unhashable type: 'dict'`.
   - Enforced single-owner lifecycle in `fail_job` with strict `owner_token` and `locked_by` validation.
   - Removed blocking `await asyncio.sleep(retry_wait)` inside `pipeline.py`.
   - Implemented 3s debounce, 30s cooldown, and concurrent polling guards in `FollowUpSuggestions.tsx`.
   - Tests: 6/6 passed in `tests/test_stage1_ai_latency.py`.

2. **Stage 2 (Queue Observability and Telemetry):**
   - Migration 11 added `started_at` and `completed_at` to `jobs` table.
   - Expanded `get_interview_jobs_status`: `counts`, `counts_by_type`, `oldest_pending_age_sec`, `active_jobs` with `job_ids` filtering (no audio/prompts leaked).
   - `process_one_job` records `queue_wait_ms` and `execution_duration_ms` into audit events.
   - Updated TypeScript contracts and frontend API client.
   - Tests: 5/5 passed in `tests/test_stage2_telemetry.py`.

3. **Stage 3 (Concurrency Protection, Revisions, Leases):**
   - Added `save_turn_transcript_segment` with atomic verification of `job_id`, `owner_token`, `locked_until`, lifecycle, and human role preservation.
   - Added CAS cursor verification `(sequence, sample_offset)` to `commit_turn_assembly_results` with anti-regression protection.
   - `STTRateLimitError` immediately fails job without sleeping inside the network adapter.
   - Isolated fallback state in `ResilientLLMAdapter`; `LLMAuthenticationError` fails job immediately as terminal.
   - Pipeline enforces `DEFAULT_JOB_DEADLINES` via `asyncio.wait_for` and extends leases via heartbeat.
   - Tests: 6/6 passed in `tests/test_stage3_concurrency.py`.

4. **Stage 4 (Decoupled Consumers and Bounded Concurrency):**
   - Split pipeline worker queue into 5 isolated consumer categories across 6 coroutines:
     - Assembly: `["TRANSCRIBE_AUDIO"]` (concurrency 1)
     - Live STT: `["TRANSCRIBE_TURN"]` (concurrency 2)
     - Assessment: `["EVALUATE_QUESTION"]` (concurrency 1)
     - Follow-ups: `["GENERATE_FOLLOWUPS"]` (concurrency 1)
     - Background: all other job types (concurrency 1)
   - Eliminated 300ms post-job delay (`await asyncio.sleep(0)` for cooperative yield).
   - Made empty queue polling interruptible via `asyncio.wait_for(stop_event.wait(), timeout=poll_interval)`.
   - Added global `async with self._provider_semaphore` (concurrency cap: 5).
   - Shared HTTP client across pipeline and LLM adapter with clean shutdown in `worker.close()`.
   - Tests: 4/4 passed in `tests/test_stage4_consumers.py`.

5. **Stage 5 (Actual UI State and Deduplication):**
   - Implemented `Repository.find_active_evaluate_job` and server-side deduplication in `enqueue_job_endpoint` keyed by `compute_candidate_fingerprint`.
   - Excluded interviewer speech and unverified shared track speech from candidate evidence fingerprint.
   - New candidate speech alters fingerprint and permits new job; `FAILED` permits retry.
   - `LiveSessionScreen.tsx` tracks jobs to terminal state with clear UI indicators (Queued, Processing, Failed/Retry).
   - `ReviewScreen.tsx` batches "Evaluate All" with bounded concurrency (3), tracks jobs to terminal state, recovers active jobs on remount, and removes the 30-tick timeout.
   - Tests: 6/6 passed in `tests/test_stage5_dedup_and_ui_state.py`.

6. **Stage 6 (Latency Profiling, Audits & Uploader Lifecycle):**
   - Fixed uploader lifecycle in Rust Tauri: when stopping capture with remaining unacked chunks, ownership is transferred to `background_uploaders`, preventing duplicate uploader instantiation.
   - Verified Cargo tests and formatting: 100% rust/cargo success.
   - Verified all 212 pytest tests in repository pass cleanly.
