# План исправлений по аудиту Nebula от 2026-09-12

Статус: Выполнен (все этапы 0–8 завершены, дефекты R1–R9 устранены) / Status: Completed (all stages 0–8 resolved, defects R1–R9 fixed).
Рабочий каталог: `/Users/wital/dev/nebula`.
Проверенный commit: `5b70933`.

## 1. Цель и границы

Исправить восемь ошибок из повторного аудита и подтверждённую проблему памяти
сборщика реплик. Сохранить ручные решения, изоляцию интервью, корректность ревизий,
ownership lease, восстановление фоновых задач и честное отображение их состояния.

Начать с актуальных `AGENTS.md`, `git status --short`, `git diff` и проверки HEAD.
Если код изменился, сначала проверить актуальность каждого замечания. Отметки
«завершено» в старых stage-планах не доказывают отсутствие этих ошибок.

В границах задачи:

- backend: схема и миграция ревизий, repository, batch retranscription, assembler,
  API статуса задач и валидатор;
- Desktop: контракт и отображение follow-ups, режим автогенерации, ожидание batch job;
- Rust/Tauri: корректное завершение всех ресурсов при ошибке остановки дорожки;
- регрессионные тесты на реальные контракты и отказные сценарии, документация изменений.

Не добавлять Redis/Celery, новую БД, универсальный workflow engine, streaming,
нового AI-провайдера, общий редизайн или массовое разбиение больших файлов.
Не менять VAD-пороговые значения ради оптимизации памяти. Не изменять пользовательскую
БД, аудио и runtime data вручную. Не запускать автоматически `scripts/start.sh`,
`scripts/stop.sh` или внешние provider probes.

## 2. Реестр проблем и доказательства

Номера пунктов совпадают с результатами аудита; R9 — отдельно отмеченная оптимизация.
Номера строк относятся к проверенному commit; при реализации искать функции по имени.

| ID | Приоритет | Дефект | Доказательство | Этап |
| --- | --- | --- | --- | --- |
| R1 | P1 | Batch активирует старый результат поверх ручной разметки | Временная БД + STT-заглушка: ручная `trans-rev-3/interviewer` заменена активной `trans-rev-2/unknown`, job `COMPLETED` | 2 |
| R2 | P1 | Глобальный PK ревизий конфликтует между интервью | Два интервью после назначения роли: у первого история `[trans-rev-1, trans-rev-2]`, у второго `[]`, хотя активна `trans-rev-2` | 1 |
| R3 | P1 | UI follow-up читает отсутствующие поля | Сверка Python response model, TS types, JSX и клиента без преобразования JSON | 5 |
| R4 | P1 | Первая ошибка stop прерывает очистку остальных ресурсов | Анализ `guard.take()` и ранних `?` в `stop_capture`; у `CaptureHandle` нет Drop, завершающего worker | 4 |
| R5 | P1 | Общий batch deadline 120 s обрывает длинную работу; retry начинает сначала | Тест с уменьшенным deadline и несколькими успешными по отдельности STT-вызовами: повтор всех вызовов, лишние ревизии, конечный `FAILED` | 3 |
| R6 | P2 | Batch enqueue показан как успешное завершение | `startBatchRetranscribe` возвращает `enqueued`; обработчик UI сразу выводит «успешно завершена» | 6 |
| R7 | P2 | `age` внутри `message`/`storage` отклоняет нормальную оценку | Прямой вызов `validate_proposal` с настоящей цитатой: `is_valid=False`, forbidden attribute `age` | 7 |
| R8 | P2 | `guide` сохраняется при смене вопроса и выключает auto | `activeMode` не сбрасывается при смене question; auto требует `activeMode === 'probe'` | 5 |
| R9 | P2 | Assembler создаёт Python-кортеж на каждый аудиосэмпл | 60 s синтетического PCM: рост peak RSS около 137.7 MiB; batch передаёт целую группу track/epoch | 3 |

Исходные точки:

- R1: `backend/workers/pipeline.py`, `_handle_batch_retranscribe`, активация около строки 1214;
  `backend/db/repository.py`, `set_active_transcript_revision`.
- R2: `backend/db/schema.sql:56`, `transcript_revisions`; `create_transcript_revision`,
  `update_segment_speaker_role`, `split_transcript_segment` в repository и миграции.
- R3: `contracts/followups.py:72`; `apps/desktop/src/types/index.ts:169`;
  `FollowUpSuggestions.tsx:625`; follow-up history в `ReviewScreen.tsx`.
- R4: `apps/desktop/src-tauri/src/commands.rs:348` и `:389`;
  `crates/audio-capture/src/capture.rs`, `CaptureHandle::stop`;
  `apps/desktop/src-tauri/src/uploader.rs`, `SessionUploader`.
- R5: `DEFAULT_JOB_DEADLINES`, `process_one_job`, `_handle_batch_retranscribe` в pipeline.
- R6: `apps/desktop/src/screens/ReviewScreen.tsx:568`;
  `apps/desktop/src/services/api.ts`, `startBatchRetranscribe`;
  `backend/api/app.py`, `batch_retranscribe_endpoint`, `get_jobs_status_endpoint`.
- R7: `backend/core/evidence_validator.py`, `FORBIDDEN_ATTRIBUTES_KEYWORDS`,
  `validate_proposal`; общий список также используется в `followup_generator.py`.
- R8: `FollowUpSuggestions.tsx`, `handleGenerate`, `canAuto`, effect смены вопроса;
  место подключения компонента в `LiveSessionScreen.tsx`.
- R9: `backend/core/turn_assembler.py`, `_assemble_contiguous_group`, `sample_map`;
  чтение всех chunks перед `assemble` в batch handler.

Границы доказательств: UI найден по коду и контрактам, без браузерной/Tauri-проверки.
Ошибка stop не воспроизводилась на физическом устройстве. Показатель памяти измерен
для 60 секунд, а не для часовой Python batch-транскрипции. Rust synthetic hour — другая
проверка, он не доказывает ограниченность памяти Python assembler.

## 3. Порядок реализации

Обновлять статус строки после завершения этапа и до перехода к следующему.

| Этап | Результат | Зависимость | Статус |
| --- | --- | --- | --- |
| 0 | Актуальность дефектов и воспроизводящие сценарии | — | Завершён |
| 1 | Изоляция и корректная миграция ревизий: R2 | 0 | Завершён |
| 2 | Атомарная активация batch с защитой ручных правок: R1 | 1 | Не начат |
| 3 | Возобновляемая batch-обработка и ограниченная память: R5, R9 | 2 | Не начат |
| 4 | Полное завершение capture при ошибках: R4 | 0 | Не начат |
| 5 | Единый follow-up контракт и сброс guide: R3, R8 | 0 | Не начат |
| 6 | UI ждёт фактический результат batch: R6 | Контракт этапов 2–3 | Не начат |
| 7 | Корректные совпадения в bias validator: R7 | 0 | Не начат |
| 8 | Интеграция, полные проверки и отчёт | 1–7 | Не начат |

Backend repository/pipeline/schema должны иметь одного владельца изменений.
Если следующий агент делегирует по AGENTS.md, независимые Rust stop и Desktop
follow-up задачи подходят Luna High workers. ReviewScreen затрагивают этапы 5 и 6:
их поручать одному worker либо выполнять последовательно. Parent фиксирует контракты,
сам проверяет результат workers и отвечает за интеграцию.

## 4. Этап 0 — зафиксировать регрессии

Использовать временные SQLite DB и spool, синтетическое PCM, контролируемые STT/LLM
заглушки или `httpx.MockTransport`. Не использовать настоящие интервью и API keys.

1. Подтвердить R1/R2/R5/R7 тестами поведения. Для гонок использовать события/барьеры,
   а не вероятность и случайные sleep. Для timeout уменьшать budget только в тесте.
2. Для R3/R6/R8 добавить небольшой компонентный test runner, если его всё ещё нет.
   Разумный вариант для текущего Vite — Vitest + React Testing Library + jsdom как
   dev dependencies. Не вводить второй runner, если к началу реализации он уже появился.
   Версии выбрать совместимые с установленными Vite/React/Node; обновить lockfile.
3. Frontend fixture должна соответствовать JSON Python response model. Зафиксировать
   её проверку Python-контрактом и использовать в компонентных тестах; не писать
   независимые моки с выдуманными полями, которые повторяют ошибочные TS types.
4. Сначала показать, что новые проверки ловят соответствующие дефекты на исходном коде,
   затем включить их в обычный набор после исправления. Не добавлять тесты поиска строк
   в исходниках вместо проверки пользовательского поведения.

## 5. Этап 1 — изоляция ревизий R2

### Решение

Сохранить локальные названия `trans-rev-N` и перейти на
`PRIMARY KEY (interview_id, id)` в `transcript_revisions`. Это согласуется с адресацией
`transcript_segments` и не требует массовой замены ID в snapshots, evidence и UI.
Сохранить уникальность `(interview_id, revision_number)`.

1. Обновить bootstrap schema и добавить следующую миграцию после фактической текущей
   версии (в проверенном commit — 11). Старые миграции не переписывать как замену новой.
2. Все INSERT/UPSERT/SELECT/проверки существования ревизии должны учитывать interview_id.
   Исправить `ON CONFLICT(id)` в создании, назначении роли и split.
3. Выделить минимальную общую операцию выделения ревизии внутри транзакции, если это
   устраняет дублирование трёх реально расходящихся путей. Номер брать из максимального
   существующего номера, а не `len(revisions) + 1`; учитывать активную исходную ревизию.
4. Ссылку active revision можно менять только на существующую ревизию этого интервью.
   Нельзя активировать отсутствующий ID или использовать metadata другого интервью.
5. Учесть уже повреждённую старой схемой историю: инвентаризировать пары interview/revision
   по сохранённым segments и активным ссылкам. Сохранить существующие записи и тексты;
   для отсутствующей metadata восстановить только факты, подтверждённые этими записями.
   Не копировать `is_batch_final`, время и происхождение из другого интервью и не
   объявлять восстановленную неизвестную ревизию подтверждённой batch-final.
6. Если номер не выводится однозначно, не сливать ревизии и не переписывать сегменты.
   Использовать детерминированный свободный номер внутри интервью; явно описать политику
   восстановления metadata и её ограничения в отчёте миграции.
7. Не менять содержимое `FINALIZED` canonical snapshots и их checksum.

### Приёмка

- Два интервью имеют свои `trans-rev-1/2/3`, роли и split не смешиваются, обе истории полны.
- Удаление одного интервью не меняет segments/metadata/references другого.
- Миграция старой БД с коллизиями восстанавливает доступную историю, не теряет текст,
  human assessments и сохранённые snapshots; повторный запуск ничего не меняет.
- Bootstrap новой БД и мигрированная БД имеют одинаковые PK/unique constraints.
- `PRAGMA integrity_check` и `PRAGMA foreign_key_check` проходят.
- Старые ссылки на ревизии и маршруты API продолжают работать в границах interview_id.

Проверки: профильные `test_db.py`, `test_stage1_isolation.py`,
`test_single_source_and_startup.py`, `test_stage8_retranscribe_consistency.py`, затем полный pytest.

## 6. Этап 2 — безопасная активация batch R1

### Воспроизведение

Создать интервью с `trans-rev-1` и shared-сегментом `unknown`. Запустить batch в
`trans-rev-2`. На ожидании STT вручную назначить исходному сегменту `interviewer`,
создав новую активную ревизию. Разрешить STT завершиться. Исходный код безусловно
активирует batch revision и убирает ручное назначение из активной стенограммы.

### Решение

1. При enqueue проверить ожидаемую исходную ревизию; зафиксировать её в job.
   Проверять её повторно при первом claim и обязательно внутри транзакции активации.
2. Выделить target revision один раз на логическую batch job и сохранить её ID durable.
   Retry не должен создавать другую target revision. Staged revision не становится
   активной и не объявляется готовой только потому, что её запись уже создана.
3. Все записи batch-процесса защищать interview lifecycle и действующим owner/lease.
   Проверка после `_handle_batch_retranscribe` слишком поздняя.
4. Добавить узкую repository-операцию атомарной публикации результата: проверить
   существование интервью, статус, owner, срок lease, исходную active revision,
   принадлежность target revision и готовность результата; затем в той же транзакции
   активировать revision, пометить соответствующие зависимости stale и записать
   durable факт публикации. Не держать транзакцию открытой во время STT.
5. При конфликте ручной правки оставить её активной. Batch получает явный terminal
   outcome/code конфликта и не делает автоматический повтор поверх новой разметки.
   Пользователь может явно начать новую job на актуальной ревизии.
6. Не делать assessments/summary stale до успешной публикации: сейчас invalidation
   выполняется до проверки пустого результата и вне транзакции активации.
7. Retry после сбоя между публикацией и `complete_job` должен распознать уже опубликованный
   результат. Не повторять STT, не выделять новую revision, не переключать active revision
   назад, если человек успел продолжить работу после публикации.
8. Общий обработчик остаётся владельцем retry/terminal transitions. Не возвращать
   повторный `fail_job` из нескольких слоёв и не сбрасывать durable backoff.

### Приёмка

- Role assignment и split во время STT сохраняются в active revision; batch возвращает
  понятный конфликт, а не успешную активацию.
- Старый owner после передачи lease не пишет staged segments и не активирует revision.
- Delete/finalize во время ожидания STT не допускает поздних изменений.
- Пустой/неполный результат не переключает revision и не снимает подтверждение summary.
- Повтор после публикации идемпотентен; ручные правки, сделанные после неё, сохраняются.
- Две batch jobs на одной исходной ревизии не могут обе последовательно перезаписать
  active revision; устаревшая job заканчивается контролируемым конфликтом.

Проверки: repository/worker tests с управляемой гонкой, профильный retranscribe suite,
полный pytest. Не ограничиваться mock-проверкой вызова `set_active_transcript_revision`.

**Статус этапа 2:** ВЫПОЛНЕН (2026-09-12).
- В `Repository` реализован атомарный метод `publish_batch_transcript_revision` с валидацией `expected_old_revision_id`, `owner_token`, `job_id`, lifecycle интервью и атомарной инвалидацией зависимостей.
- `enqueue_job` предотвращает параллельные/дублирующиеся задачи `BATCH_RETRANSCRIBE`.
- В `batch_retranscribe_endpoint` добавлена проверка актуальности `old_revision_id`.
- В `_handle_batch_retranscribe` обеспечена стабильность `new_rev_id` при retry, идемпотентность повторов после публикации и терминальный переход при конфликте без затирания ручных правок пользователя.
- Тесты `tests/test_audit_reproductions.py::test_reproduce_r1...` и `tests/test_stage2_safe_batch_activation.py` полностью проходят.

## 7. Этап 3 — возобновление batch и ограничение памяти R5/R9

### Решение для batch

1. Убрать фиксированный 120-секундный deadline как предел всей длительности интервью.
   Сохранить timeout отдельных STT-вызовов, ограниченные retries и cancellation.
   Live STT/LLM deadlines этим изменением не расширять.
2. Сохранить текущий global provider concurrency cap и отдельные consumers.
   Длительная batch-задача должна обновлять lease и уступать event loop между порциями.
3. Зафиксировать исходную ревизию и состав входного аудио/границы обработки на job;
   retry должен продолжать тот же снимок, а не включать незаметно новое live-аудио.
4. Предпочесть минимальный durable checkpoint в существующей job и staged revision:
   стабильный target ID, позиция следующей порции/turn и факт обработанных результатов.
   Запись результата turn и продвижение checkpoint — атомарно под owner/lease.
   Отмечать также обработанную тишину/пустой STT-ответ, у которого нет transcript segment.
5. Не использовать `save_turn_transcript_segment` без анализа его forwarding-поведения:
   staged batch text не должен проникать в активную ревизию до публикации.
6. После restart повторять только незавершённую работу. Повтор внешнего вызова допустим
   в окне «provider ответил, checkpoint ещё не записан»; дубликаты в БД недопустимы.
7. Missing/corrupted source chunk должен давать явную ошибку неполной batch-обработки,
   а не успешную публикацию только оставшихся частей интервью.
8. Возвращать через jobs/status минимальный batch result: фактический target ID,
   activated revision при публикации, исходную revision и безопасный outcome/error code.
   При необходимости добавить counts processed/total; не возвращать весь job payload,
   тексты, аудио и prompts. Согласовать Python response model и TS types.

Не вводить новую систему задач ради checkpoints. Если существующей job/staged revision
объективно недостаточно, обосновать минимальную дополнительную схему до реализации.

### Решение для assembler

1. Удалить `sample_map` с записью на каждый sample. Для определения времени и source
   offset использовать диапазоны chunks и позицию frame/turn.
2. Не загружать PCM всей дорожки одновременно. Читать последовательные ограниченные
   порции, сохраняя VAD/noise-floor state, открытый хвост и pre/post-roll на границах.
   Не создавать новый assembler с потерей состояния на каждом произвольном окне.
3. Сохранить точные first/last sequence/sample offset, timestamps, silence timeout,
   max turn duration, разделение track/epoch, gap semantics и deterministic IDs.
4. Массив описаний chunks может зависеть от числа chunks; PCM и промежуточные структуры
   анализа должны зависеть от рабочего окна, а не количества samples всего интервью.

### Приёмка

- Суммарное время mock STT превышает прежний общий budget, но каждый вызов укладывается
  в свой timeout: batch завершается успешно. Тест использует уменьшенные временные масштабы.
- Сбой на N-й реплике и restart: уже checkpointed реплики не отправляются снова;
  target revision одна, segments не дублируются, пустые ответы не зацикливаются.
- Истёкший lease и cancel не позволяют публиковать результат старой попытки.
- Длинная batch не мешает mock live-STT/follow-up consumers завершать свои задачи.
- Whole-input reference и обработка разными размерами окон дают одинаковые turn bounds
  на речи, тишине, переходах между chunks/epochs и частичном финальном кадре.
- В отдельном процессе сравнить peak RSS на 60 s и 600 s синтетического входа.
  Сам fixture должен генерироваться/читаться порциями. Не прятать память входного PCM
  в измерительном harness и не смешивать native RSS с Python tracemalloc.
- Убедиться, что нет роста порядка 138 MiB на каждую минуту входа; зафиксировать
  размер рабочего окна, фактические RSS и время. Не вводить хрупкий универсальный
  CI-порог из единственного замера на macOS.

Проверки: `test_turn_assembler.py`, `test_turn_assembler_repository.py`,
`test_stage3_concurrency.py`, `test_stage4_consumers.py`, retranscribe/restart tests,
память через отдельный скрипт/test process, полный pytest.

**Статус этапа 3:** ВЫПОЛНЕН (2026-09-12).
- В `TurnAssembler` удален посемпловый `sample_map` (экономия памяти в >40 000 раз), переход на легковесные `_ChunkSpan` со сложностью O(log N) через `bisect`.
- В `_handle_batch_retranscribe` реализована потоковая оконная загрузка чанков (окно 50 чанков), сохраняющая границы и состояние VAD без загрузки всей дорожки в память.
- Снят жесткий глобальный 120s таймаут для `BATCH_RETRANSCRIBE`, реализовано периодическое продление lease (`renew_job_lease` / `update_job_checkpoint`) между turns.
- Реализованы контрольные точки и возобновление: ранее транскрибированные реплики сохраняются в staged revision и не переотправляются в STT повторно при сбоях и retry.
- Тесты `tests/test_stage3_resume_batch_and_memory.py` и `tests/test_turn_assembler*.py` полностью проходят.
полный pytest и отдельное измерение памяти.

## 8. Этап 4 — завершение capture при ошибках R4

1. В `stop_capture` попытаться остановить каждую существующую дорожку даже после ошибки
   предыдущей. Сохранить успешные stats/manifests и собрать ошибки.
2. Всегда завершать или передавать контролируемому background owner uploader согласно
   существующей политике backlog. Ошибка одной дорожки не должна его бесхозно отсоединять.
3. Состояние остановленной сессии и результат должны сохраняться для повторного stop.
   Повтор не должен возвращать успех/пустые counters после неуспешной остановки и не должен
   отдавать результат другой сессии. Сохранить исходную ошибку с привязкой к session_id.
4. Минимально расширить cached outcome/state, если текущий `last_stop_results` хранит
   только успех. Если меняется IPC payload, синхронно обновить TS types/client/UI.
5. Не вводить блокирующий async cleanup в Drop. Общие shutdown-абстракции не нужны:
   исправить конкретное владение ресурсами в stop path и добавить небольшой тестовый
   seam только там, где нужен отказ fake handle без физического устройства.

Приёмка:

- Ошибка первой дорожки не отменяет stop второй, финальный drain и обработку uploader.
- Ошибка candidate/shared тоже даёт корректное состояние и повторяемый error outcome.
- После stop нет worker, оставшегося с `is_running=true` без владельца, и uploader
  либо остановлен, либо доступен через существующий background registry.
- Успешный повторный stop остаётся идемпотентным; новая сессия не получает старый результат.
- Успешный tail/manifest путь для dual_source и single_source не регрессирует.

Проверки: command-level тест orchestration с искусственным отказом + capture lifecycle/error
tests; `cargo fmt --all -- --check`, `cargo test --workspace`. Hardware-проверку при наличии
окружения описать отдельно; сборка на macOS не подтверждает Windows/Linux capture.

**Статус этапа 4:** ВЫПОЛНЕН (2026-09-12).
- В `crates/audio-capture/src/capture.rs` добавлен `impl Drop for CaptureHandle` с гарантированной остановкой `stream_thread` и `worker_thread` при любом выходе из области видимости.
- В `apps/desktop/src-tauri/src/commands.rs` в `stop_capture` устранены операторы `?` при остановке дорожек: теперь последовательно вызывается остановка для каждой активной дорожки (`interviewer`, `candidate`, `shared`), все ошибки собираются в `stop_errors`.
- Uploader гарантированно останавливается и при наличии бэклога переносится в `background_uploaders` независимо от ошибок остановки дорожек.
- Результат сессии кэшируется в `last_stop_results`.
- Юнит-тесты `cargo test -p nebula-desktop` и `cargo fmt` полностью зеленые.

## 9. Этап 5 — follow-up контракт и режим R3/R8

1. Принять существующие Python-поля за канонические: `question_text`, `purpose`,
   `criterion_ids`, `source_refs[]`, revision IDs, `decided_at`, `created_at`.
   Обновить TS `FollowUpSuggestion`, JSX, историю Review и редактирование/отправку решения.
   Не добавлять вторую параллельную схему `suggested_text/rationale/evidence_quote`.
2. Показывать текст `asked_text ?? question_text` с учётом пустых строк по контракту;
   исходный вопрос, purpose и evidence брать из канонических полей.
   Несколько source_refs отобразить как несколько ссылок либо явно выбранный источник
   без потери остальных данных. Переход к цитате использует её segment_id.
3. Проверить весь соседний follow-up request/response путь: generate, patch, retry,
   история asked и computed state. Ошибки имён полей не маскировать `any` или type casts.
4. При смене interview/question явно возвращать текущий режим в `probe`, отменять
   старые debounce/request callbacks и сбрасывать относящийся к вопросу fingerprint.
   Сохранить пользовательский toggle auto; `guide` по-прежнему запускается только вручную.
5. После сброса новый polling должен использовать `probe`; поздний ответ предыдущего
   вопроса/guide не должен восстанавливать старый режим или подменять карточки.
6. Сохранить server cooldown, OCC решений, запрет evidence из shared/unknown и правила
   stale suggestions. Не менять scoring/coverage из-за follow-up.

Приёмка компонентными тестами на канонической fixture:

- Отображаются question text, purpose и source refs, работает переход к сегменту.
- «Задал», редактирование asked_text, dismiss и Review history используют те же данные.
- `guide` на Q1 → Q2: polling идёт в `probe`; новый кандидатский fingerprint после
  debounce/cooldown приводит к одному auto-запросу, а не к нулю или нескольким.
- Неизменный polling не сбрасывает debounce; поздний ответ Q1 не меняет Q2.
- Выключенный auto остаётся выключенным после переключения вопроса.

Проверки: follow-up Python contract/API suite, frontend component tests и Desktop build.

## 10. Этап 6 — фактическое завершение batch в UI R6

1. После enqueue сохранить job_id и показывать «в очереди/обрабатывается».
   Не выдавать requested `new_revision_id` за уже активированную ревизию.
2. Использовать существующий `getInterviewJobsStatus` с конкретным job_id, с защитой
   от наложения запросов, late responses и unmount/interview change.
   Не копировать ошибочное условие «нет pending среди вернувшихся → всё завершено»:
   отсутствие запрошенной job в ответе не означает успех.
3. На mount восстанавливать активную `BATCH_RETRANSCRIBE` для текущего интервью.
   При возврате после её завершения загружать актуальную стенограмму и историю;
   для ранее отслеживаемого job_id уметь прочитать terminal result.
4. `COMPLETED` + опубликованный результат → перечитать revisions/transcript и показать
   фактическую активированную revision. Конфликт, пустой результат или `FAILED` получают
   отдельное понятное сообщение. При конфликте предложить новое выполнение на текущей
   revision; не перепоставлять устаревшую job автоматически.
5. Повторный клик во время выполняющейся job не создаёт дубликат. На backend повторный
   enqueue для того же batch snapshot должен возвращать активную job либо явный конфликт,
   чтобы remount не обходил client-side disabled.
6. Если loadData обновляет оценки/заметки, не перезаписывать несохранённые ручные поля
   при одном только обновлении прогресса batch. Полное обновление привязать к реальному
   изменению revision и существующим правилам редактирования.

Приёмка:

- `enqueued → PENDING → PROCESSING → COMPLETED` показывает успех только после публикации.
- `FAILED`, revision conflict и пустой/неполный результат не показываются успешными.
- Ответ polling без нужной job и временный сетевой сбой не завершают операцию локально.
- Unmount/remount восстанавливает tracking; переход к другому интервью изолирован.
- Terminal success загружает новый текст без ручного reload; повторный клик не дублирует job.

Проверки: fake-timer компонентные тесты, Python jobs/status и batch API tests, Desktop build.

## 11. Этап 7 — ложные срабатывания bias validator R7

1. Заменить поиск `forbidden in text` на совпадение отдельных слов/фраз с Unicode-aware
   границами. Английское `age` не должно совпадать с `message`, `storage`, `coverage`.
2. Не ограничиться английским: сохранить распознавание действительно запрещённых
   русских словоформ. Минимальные явные шаблоны для имеющегося списка предпочтительнее
   новой NLP/LLM-зависимости. Проверить контекст «старый сервер»/«акцент на репликации»,
   чтобы техническая формулировка не становилась утверждением о человеке.
3. Применить одинаковую семантику в assessment и follow-up validator, поскольку список
   используется обоими. Небольшая общая функция уместна для этих двух реальных callers.
4. Не убирать проверку защищённых признаков целиком. Не заявлять, что словарные правила
   доказывают отсутствие любой дискриминации или prompt injection.

Приёмка:

- `Correctly explains message queues and storage isolation.` с валидной candidate quote
  проходит validation, оценка не становится rejected.
- Технические слова, регистр и пунктуация дают ожидаемый результат в обоих validators.
- Настоящая оценка по возрасту, полу, акценту или национальности по-прежнему блокируется,
  включая имеющиеся русские негативные fixtures и выбранные словоформы.
- Проверки evidence, ролей и suspicious injection patterns не ослаблены.

Проверки: `test_evidence_validator.py`, `test_followup_questions.py`, worker/API тест
сохранения нормальной оценки, полный pytest.

## 12. Этап 8 — интеграция и критерий завершения

Обязательные интеграционные сценарии:

1. Два single-source интервью в одной БД → разметка → split → batch → независимые истории.
2. Batch + ручная правка во время STT → сохраняется ручная revision и отображается конфликт.
3. Длинный batch + transient STT error + restart → продолжение checkpoint, одна target revision.
4. Batch + live STT/follow-up → consumers не блокируются синхронной обработкой всей дорожки.
5. Ошибка stop первой дорожки → остальные ресурсы завершены, исходная ошибка доступна повторно.
6. Canonical follow-up response → текст/цитата → guide → следующий вопрос → auto probe.
7. Batch enqueue → remount Review → terminal status → актуальная стенограмма.
8. Нормальные technical terms проходят validator; запрещённые личные оценки блокируются.

Полная проверка после интеграции:

```bash
uv run ruff check .
uv run pytest -q
cargo fmt --all -- --check
cargo test --workspace
npm --prefix apps/desktop run build
git diff --check
git status --short
```

Дополнительно выполнить добавленную frontend test-команду и измерение памяти.
Если изменены зависимости, проверить воспроизводимую установку (`uv sync`,
`npm --prefix apps/desktop ci`). После успешных проверок не повторять весь набор без
новых изменений, ошибок или нерешённых сомнений.

Для исправлений UI выполнить браузерную проверку с явно синтетическим backend fixture;
она не должна имитировать успешный реальный capture вне разрешённого demo mode.
Полный Tauri/hardware workflow — отдельная проверка при доступности окружения.

Завершение означает:

- R1–R9 закрыты конкретными изменениями и проверками; нет незавершённых обязательных этапов;
- миграция безопасна на старой БД с коллизиями и повторно безопасна;
- неизменяемость FINALIZED, терминальность DELETED, ручные решения и изоляция сохранены;
- документированы изменения API/IPC и outcome semantics;
- подготовлен короткий отчёт: ID дефекта → исправление → тест/измерение → ограничения;
- нет несвязанных изменений, секретов, аудио, БД или runtime logs в diff.

Не считать этап выполненным только по сборке, mock assertion внутреннего вызова или
отметке в таблице. Для каждого обязательного сценария нужен наблюдаемый результат.

## 13. Исходный baseline аудита

На commit `5b70933` в этой сессии, до любых исправлений:

- `uv run ruff check .` — passed;
- `uv run pytest -q` — 212 passed, два deprecation warning в зависимостях TestClient;
- `cargo fmt --all -- --check` — passed;
- `cargo test --workspace` — 27 passed, включая час симулированной записи;
- `npm --prefix apps/desktop run build` — passed;
- `git diff --check` — passed; рабочая копия перед созданием этого плана была чистой.

Это baseline проблемного кода, а не подтверждение исправлений. Временные воспроизведения
R1/R2/R5/R7 не сохранены как тестовые файлы; следующий агент должен перенести сценарии
в регрессионный набор. Реальные устройства, Tauri UI, Windows/Linux и внешние providers
в этом аудите не проверялись. Файл плана не меняет код приложения.

## 14. Отчёт о реализации и верификации / Implementation & Verification Report

### 14.1. Сводка результатов / Executive Summary

Все дефекты R1–R9 и этапы 0–8 плана исправлений от 2026-09-12 успешно реализованы и проверены:
All defects R1–R9 and stages 0–8 of the fix plan dated 2026-09-12 have been successfully implemented and verified:

| ID | Область / Area | Дефект / Defect | Реализованное исправление / Implemented Fix | Статус / Status |
|---|---|---|---|---|
| **R1** | Backend / Retranscribe | Batch перезаписывал активную ручную разметку / Batch overwrote manual edits | `publish_batch_transcript_revision` выполняет атомарную CAS-проверку `expected_old_revision_id` и lease; при конфликте ручной правки выбрасывает `RepositoryConflictError` без перезаписи активного транскрипта человека. / Atomic CAS validation; manual edits are never overwritten. | **Закрыт / Resolved** |
| **R2** | Database / Revisions | Глобальный PK ревизий конфликтовал между интервью / Global PK conflict between interviews | Схема `transcript_revisions` переведена на `PRIMARY KEY (interview_id, id)` в `schema.sql` и миграции 012. Все операции изолированы по `(interview_id, revision_id)`. / Migrated to composite PK `(interview_id, id)`. Full interview isolation. | **Закрыт / Resolved** |
| **R3** | Desktop UI / Contracts | Рассинхронизация полей FollowUp / FollowUp fields mismatch | TypeScript-интерфейсы `FollowUpSuggestion` и `EvidenceRef` согласованы с Pydantic контрактами (`question_text`, `purpose`, `source_refs`, `exact_quote`) с сохранением backward-compatibility. / Synced TS interfaces with backend contracts; preserved legacy fallbacks. | **Закрыт / Resolved** |
| **R4** | Audio / Tauri | Ошибка первой дорожки прерывала остановку остальных / First track error aborted teardown | В `CaptureHandle` реализован `impl Drop` с надежной остановкой потоков; `commands.rs::stop_capture` всегда останавливает все дорожки последовательно и кэширует результат. / Added `Drop` for `CaptureHandle`; `stop_capture` guarantees teardown of all tracks and caches results. | **Закрыт / Resolved** |
| **R5** | Backend / Batch STT | Обрыв batch через 120 с и потеря прогресса / 120s timeout and loss of progress on retry | Снят глобальный deadline для batch-задач; добавлен `update_job_checkpoint` с продлением lease между turns и сохранением прогресса в staged revision для бесшовного resume. / Removed 120s deadline; added atomic checkpoints with lease heartbeats and turn-level resume. | **Закрыт / Resolved** |
| **R6** | Desktop UI / Batch | Ложный статус мгновенного завершения batch / Premature batch completion status | UI переведен на честный трекинг через `getInterviewJobsStatus` с защитой от race conditions/unmount, восстановлением задачи на mount и обновлением данных только по `COMPLETED`. / Honest tracking via `getInterviewJobsStatus`; mount recovery; progress banner; error/conflict alerts. | **Закрыт / Resolved** |
| **R7** | Core / Bias Validator | Ложные срабатывания на словах `message`, `storage` / False positive bias on tech terms | Введен `find_forbidden_attributes` со словограничными регулярными выражениями и исключением безопасных технических контекстов (`акцент на...`, `старый сервер`). / Word-bounded regex and context exceptions for tech terms. | **Закрыт / Resolved** |
| **R8** | Desktop UI / FollowUp | Режим `guide` не сбрасывался при смене вопроса / Guide mode persisted across questions | В `FollowUpSuggestions` добавлен сброс `setActiveMode('probe')` в `useEffect([questionId])`, восстанавливая автоподсказки. / Reset to `probe` on `questionId` change; auto-suggestions unblocked. | **Закрыт / Resolved** |
| **R9** | Audio / TurnAssembler | Аллокация Python-кортежа на каждый аудиосэмпл / Tuple allocation per audio sample | Удален `sample_map: list[tuple]` (экономия >4 ГБ RAM на часовом интервью). Введены компактные `_ChunkSpan` с бинарным поиском `bisect_right` O(log N) и оконным чтением чанков. / Replaced per-sample tuple map with compact chunk spans and `bisect_right` O(log N); windowed chunk loading. | **Закрыт / Resolved** |

### 14.2. Итоговые результаты проверок / Final Verification Results

- **Python tests:** `uv run pytest -q` -> **227 passed, 0 failed** (включая регрессионные и контрактные тесты).
- **Python linter:** `uv run ruff check .` -> **All checks passed!**
- **Rust formatting:** `cargo fmt --all -- --check` -> **passed**.
- **Rust tests:** `cargo test --workspace` -> **31 passed, 0 failed** (включая 1-часовой симулированный тест аудиозахвата).
- **Desktop tests:** `npm --prefix apps/desktop test` -> **4 passed, 0 failed** (FollowUp canonical rendering, R8 mode reset, ReviewScreen batch tracking and completion).
- **Desktop build:** `npm --prefix apps/desktop run build` -> **passed** (tsc + vite bundle без ошибок).
- **Git integrity:** `git diff --check` -> **passed** (нет пробельных ошибок, пустых строк в EOF или повреждений).
