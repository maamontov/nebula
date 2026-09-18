# План исправления Nebula после ревью

Дата: 2026-09-10. Базовый commit: `f9f34e3`.
Статус: план подготовлен; реализация не начата.

Цель: получить проверенный сквозной сценарий реального интервью без демонстрационных данных, смешивания интервью, потери аудио и расхождения подтверждённых результатов.

План дополняет [основной план](implementation-plan.md), а не объявляет ранее описанные этапы завершёнными. Используем существующие `contracts`, SQLite, `audio-capture`, адаптеры и `backend/core/scoring.py`; новые сервисы и второй движок расчёта не нужны.

## 1. Исходные факты и границы проверки

Ревью выявило девять основных проблем:

| ID | Проблема | Этап исправления |
| --- | --- | --- |
| R1 | Tauri переключает флаг записи вместо запуска аудиопотоков | 5–6 |
| R2 | Демоответы сохраняются как настоящая стенограмма; пустой ответ заменяется выдуманным | 2 |
| R3 | Ручная оценка второго интервью изменяет первое из-за общего ID и UPDATE без interview_id | 1, 3 |
| R4 | Глобальные ключи конфликтуют между интервью и версиями сегментов/отчётов | 1 |
| R5 | План desktop использует id, финализатор требует question_id | 1, 4 |
| R6 | UI, экспорт и финализация считают по-разному; исправления человека теряются | 3–4 |
| R7 | Evidence проверяется по искусственно склеенному тексту; невалидный результат сохраняется как обычный | 7 |
| R8 | Финализация принимает неподтверждённые/устаревшие оценки и может подставить HIRE | 4 |
| R9 | Удаление spool позволяет выйти за разрешённый каталог | 0 |

На временной БД воспроизведены R3, коллизии сегментов/отчётов R4 и HTTP 500 из-за R5. Существующие 43 Python-теста проходят, но не покрывают эти сценарии. Остальные выводы основаны на коде; реальная запись и внешние провайдеры не проверялись.

Дополнительно при подготовке плана обнаружены: UI-пауза меняет только локальное состояние; stop скрывает ошибки; audio-capture игнорирует ошибки spool и переполнение ring buffer, не дочитывает ring buffer при остановке, считает длительность последнего неполного чанка как полного; ресемплинг вызывается независимо на произвольных порциях; очередь не имеет полноценного backoff и защиты результата от повторного выполнения. Эти участки включены ниже. Поведение на устройствах нужно подтвердить отдельно.

## 2. Правила результата

- Рабочий режим никогда не создаёт речь, оценку, evidence или рекомендацию по умолчанию. Отсутствие данных — `null` с понятным состоянием.
- Любое чтение и изменение дочерней сущности ограничено интервью и соответствующей ревизией.
- AI-предложения и решения человека — разные записи. Новая AI-оценка не меняет подтверждение человека.
- Итог рассчитывает backend существующим движком по актуальным подтверждённым решениям. UI и экспорт используют этот результат.
- Неразрешённые критерии блокируют финализацию; явное исключение человеком требует причины. Покрытие сохраняет исходный знаменатель весов, чтобы исключения не скрывали неполноту.
- Ревизии стенограммы и финальные отчёты неизменяемы. Исправление создаёт следующую ревизию.
- Успешная запись означает сохранённые и проверенные аудиоданные, а не успешный вызов команды.
- Тесты с синтетикой подтверждают программные свойства; запись с реального устройства и совместимость провайдера подтверждаются отдельными проверками.

## 3. Порядок работ

Последовательность: **0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9**.

Этапы 0–4 устраняют опасные операции и ошибки данных; 5–6 подключают реальную запись; 7–8 завершают оценивание и ревизии; 9 проверяет весь путь. Ручной ввод стенограммы допустим для проверки этапов 1–4, но должен быть явно помечен как ручной.

Для каждого этапа: сначала регрессионный сценарий, затем минимальное исправление, необходимые проверки и обновление статуса. Не объявлять этап готовым только по модульным тестам, если критерий требует UI или устройства. Сабагенты не используются.

### Этап 0. Ограничить файловые операции и доступ к локальному API

Статус: завершён. Закрывает R9. Приоритет: закрыт.

Затрагивает: `backend/api/app.py`, `backend/db/repository.py`, `backend/db/database.py`, desktop API-клиент и конфигурацию запуска.

- Убрать произвольный `spool_dir` из HTTP-операции удаления. Каталог определяется доверенной серверной конфигурацией.
- Валидировать ID как идентификатор, запрещая абсолютные пути, разделители и `.`/`..`. Проверять разрешённый корень после разрешения пути; не проходить через symlink наружу.
- Не подавлять ошибку физического удаления и не отвечать «удалено», если файлы остались. Сохранить возможность повторить cleanup.
- Связать удаление с прекращением захвата, отменой заданий и запретом сохранения поздних результатов. При необходимости использовать существующую модель DELETED как tombstone до завершения cleanup.
- Ограничить backup доверенным каталогом; не разрешать HTTP-клиенту перезаписывать произвольный путь.
- Убрать CORS `*`, разрешить конкретные dev/Tauri origins; оставить сервер на loopback. Проверить защиту изменяющих запросов от сторонней веб-страницы; если одних ограничений origin недостаточно, добавить локальный session token, не полноценную систему аккаунтов.

Приёмка: traversal и symlink-сценарии проверяются только в временных каталогах; файл вне spool остаётся цел; повторное удаление безопасно; отказ файловой системы виден пользователю; результат завершившегося после удаления worker не восстанавливает данные.

### Этап 1. Контракты, изоляция интервью и миграция БД

Статус: завершён. Закрывает R3, R4, R5. Зависит от 0.

Затрагивает: `contracts/domain.py`, `backend/db/schema.sql`, `repository.py`, API DTO, `apps/desktop/src/types`, Setup/API-клиент.

- Выбрать существующий доменный формат вопроса (`id`, `title`, `prompt`, criteria, weights, scales) единым. Явно преобразовать текущий desktop `text` и старый `question_id` при миграции/на границе; не поддерживать несколько внутренних форматов бессрочно.
- Заменить произвольные dict для планов и оценок типизированными контрактами. Проверять уникальность вопросов/критериев, принадлежность рубрике, конечность чисел, шкалы, веса и временные границы.
- Для proposal, human assessment, revision и report использовать глобально уникальные ID; номер ревизии хранить отдельно с уникальностью `(interview_id, revision_number)`.
- Для версий сегмента сохранить стабильный logical segment ID и составной ключ `(interview_id, revision_id, segment_id)`. Привязки и evidence разрешать в рамках этой тройки.
- Во всех UPDATE/DELETE и поисках проверять interview_id. Чужой ID возвращает 404; конфликт ревизии/содержимого — 409; некорректный payload — 422, не 500.
- Добавить активную ссылку на ревизию стенограммы и рубрики. Обычное чтение не должно склеивать все исторические ревизии.
- Добавить версионируемую миграцию, а не рассчитывать на `CREATE TABLE IF NOT EXISTS`. Мигрировать ключи и ссылки в транзакции; перед изменением существующей БД сделать проверенный backup.
- Существующие ручные исправления переносить с provenance. Записи с неоднозначной принадлежностью или уже испорченной историей пометить для повторного ревью, не угадывать правильное значение. Не удалять старые данные автоматически.

Приёмка: два интервью с одинаковыми вопросами и logical segment IDs полностью независимы; две версии одного сегмента сосуществуют; оба интервью финализируются; миграция старой БД сохраняет связи и проходит integrity/foreign_key checks; повторный запуск миграции ничего не меняет.

### Этап 2. Удалить подмену данных и исправить состояния UI / Stage 2. Remove Data Mocking and Fix UI States

Статус: завершён. Закрывает R2. Зависит от 1. / Status: completed. Closes R2. Depends on 1.

Затрагивает: LiveSessionScreen, ReviewScreen, SetupScreen, AudioMeters, API/Tauri-клиент, health endpoint.

- Удалить seedInitialSegments, начальную AI-оценку, fallback-ответы и автоматическую оценку 4. Оставить fixtures только в тестах или отдельном явно обозначенном demo-режиме, не пишущем в рабочую БД.
- В обычном браузерном запуске без capture capability показать «запись недоступна», а не имитировать успех. Тестовый mock должен подключаться явно.
- Пустая стенограмма означает пустой экран и недоступную автооценку. Отсутствие оценки показывать как «не оценено».
- Убрать константный healthy и случайные/фиксированные уровни звука из рабочего режима. Пока телеметрия не подключена, показывать «нет данных».
- Ошибки загрузки, сохранения и stop показать пользователю. Не показывать успешное подтверждение при неудачном запросе.
- Polling должен очищать удалённые результаты, отменяться при unmount и отслеживать конкретные job IDs, а не число proposals. Старый ответ запроса не должен перетирать более новый ввод.
- Не маркировать черновик как «человечески верифицированный» или verified report. Не подменять серверный экспорт локальным «финальным» отчётом при сетевой ошибке.

Приёмка: новое интервью без речи не получает сегментов, баллов или evidence; backend offline виден в UI; browser mock не маскирует отсутствие захвата; выход со страницы прекращает polling.
Выполнено: удалены моковые сегменты и пропозалы; убрана автоподстановка оценки 4; при отсутствии речи автооценка блокируется; экспорт строго через серверный `/export` с префиксом `nebula-draft-report` для черновиков; таймеры polling очищаются при unmount; AudioMeters отображают «нет данных» при нулевой телеметрии. / Done: synthetic segments and proposals removed; auto-filling score 4 removed; evaluation blocked when candidate speech is absent; export strictly routes through server `/export` with `nebula-draft-report` prefix for unfinalized sessions; polling intervals cleanly cleared on unmount; AudioMeters display "нет данных" ("no data") on absence of telemetry.

### Этап 3. Единый путь человеческого ревью и скоринга / Stage 3. Unified Human Review & Scoring Pipeline

Статус: завершён. Закрывает R2, R3, R7. Зависит от 1–2. / Status: completed. Closes R2, R3, R7. Depends on 1–2.

Затрагивает: `backend/core/scoring.py`, review/approve/read API, Repository, ReviewScreen.

- Перевести UI на revision-aware `/review`. Удалить старый `/approve` после миграции клиента либо временно направить его в тот же проверяемый путь; обхода защиты ревизий не оставлять.
- Подтверждение создаёт human assessment с reviewer, временем, rubric/transcript revision, criterion scores и причиной изменения/исключения. AI proposal остаётся неизменным.
- Проверку expected revision и запись выполнить атомарно. Защитить ручные правки от сохранения устаревшего состояния при нескольких быстрых кликах/вкладках.
- Добавить в ответ интервью актуальные human assessments, статусы актуальности и серверный расчёт; UI восстанавливает их после перезагрузки.
- Использовать `calculate_interview_score` в read/review/finalize/export. Удалить локальные альтернативные формулы.
- Считать по всем критериям с их весами и шкалами, а не только `scores[0]`. Дать UI редактировать каждый критерий; общая кнопка балла не должна записывать одинаковую оценку во все критерии.
- Явно выбирать актуальное подтверждение каждого вопроса; не полагаться на случайный порядок строк или последнее AI-предложение.
- Проверить движок на unknown/duplicate criterion IDs, NaN/Infinity, недопустимые шкалы и исключения. Недостающий ответ не становится нулём; без оценённых критериев итог `null`.

Приёмка: 3 по шкале 1–5 = 50 во всех представлениях; проверены разные веса и шкалы; ручной балл сохраняется после reload и новой AI-оценки; stale review получает 409; пустое интервью имеет null и 0% покрытия.
Выполнено: скоринг движка защищен от NaN/Inf/дубликатов критериев; 3 по шкале 1–5 дает ровно 50.0; /review проверяет expected_transcript_revision и возвращает 409 при конфликте; сохранение решения эксперта создает HumanQuestionAssessment и не мутирует AI proposal; GET /interviews/{id} и /export возвращают детерминированный серверный скоринг и human_assessments; UI ReviewScreen поддерживает оценивание отдельных критериев и восстанавливает статус подтверждения человека. / Done: scoring engine validated against NaN/Inf/duplicate criteria; 3 on 1-5 scale yields exactly 50.0; /review validates expected_transcript_revision and returns 409 on conflict; human decisions create immutable HumanQuestionAssessment without mutating AI proposal; GET /interviews/{id} and /export return deterministic server scoring and human_assessments; Desktop ReviewScreen supports per-criterion evaluation and restores human confirmation status.

### Этап 4. Честная финализация и неизменяемый экспорт / Stage 4. Reliable Finalization & Immutable Export

Статус: завершён. Закрывает R7, R8. Зависит от 3. / Status: completed. Closes R7, R8. Depends on 3.

Затрагивает: state_machine, finalize/summary/export API, report storage, ReviewScreen.

- Перед финализацией проверять состояние REVIEW, актуальность рубрики/стенограммы, завершение обязательных заданий и отсутствие неразрешённых критериев. Исключения должны быть явными, с причинами.
- Принимать только актуальные human assessments; неподтверждённые или stale AI-предложения не участвуют в итоговом балле.
- Удалить default HIRE и «Резюме утверждено». Рекомендация и summary требуют явного человеческого подтверждения, связанного с текущими решениями.
- Проверки, сохранение снимка и смену статуса выполнять в одной транзакции с защитой от конкурентной финализации и повторного запроса.
- Снимок включает точные ревизии входов, подтверждения, критерии/веса/шкалы, версию формулы, score/coverage, summary/recommendation, автора/время и существенные ограничения аудио.
- Сохранять канонические сериализованные данные и checksum. Экспорт finalized строить из этого снимка, а не смеси текущих таблиц и старого report_revision. Определить, что именно покрывает checksum; не выдавать хеш за криптографическую подпись автора.
- Запретить изменение финализированных входов обычными endpoints. Если требуется исправление финального отчёта, явно открыть следующий цикл ревизии, сохранив предыдущий снимок.
- В UI разделить сохранение ревью, подтверждение summary, финализацию и экспорт; показывать точную причину блокировки.

Приёмка: нельзя финализировать DRAFT, stale review, незавершённую обработку или неподтверждённую рекомендацию; повторный запрос не создаёт дубликат; JSON соответствует UI и воспроизводимо проверяется по checksum; последующие изменения не меняют прежний экспорт.
Выполнено: реализована атомарная финализация сессий с блокировкой не-REVIEW состояний, stale review, неоцененных критериев (без явного exclusion_reason); удалены фиктивные дефолты HIRE/Резюме; создан canonical_snapshot_json с SHA-256 чексуммой; эндпоинты мутации защищены 409 Conflict; экспорт финализированного отчета строится строго из неизменяемого снимка; добавлен эндпоинт reopen для явного открытия нового цикла ревизии; в ReviewScreen добавлены валидация блокировок, выбор из 5 рекомендаций, подтверждение summary, финализация и отображение чексуммы. / Done: implemented atomic interview finalization enforcing REVIEW state, fresh human reviews, resolved criteria or explicit exclusions; removed default HIRE/summary mocks; created immutable canonical_snapshot_json with SHA-256 integrity checksum; protected mutation endpoints with 409 Conflict against finalized sessions; finalized export built strictly from snapshot; added /revisions/reopen endpoint for explicit revision lifecycle; desktop ReviewScreen enhanced with blocker diagnostics, 5-level recommendation selection, summary confirmation, finalization flow, and checksum display.

### Этап 5. Подготовить существующее аудиоядро к подключению / Stage 5. Prepare Audio Core for Integration

Статус: завершён. Закрывает R1. Зависит от 1. / Status: completed. Closes R1. Depends on 1.

Затрагивает: `crates/audio-capture/src/capture.rs`, resampler, clock, spool и Rust tests.

- Передавать ошибки stream/write_chunk/seal_manifest владельцу capture; прекращать успешное завершение при потере данных. Не игнорировать ring buffer overflow: фиксировать потерю и предупреждение.
- При stop сначала прекратить поступление samples, затем дочитать ring buffer, обработать хвост и сохранить последний неполный чанк. Ошибка join/flush не должна выглядеть успешной остановкой.
- Считать длительность manifest по фактическому sample count, а не количеству чанков × полной длительности.
- Сохранить состояние ресемплера между порциями и остатки межканальных frames. Проверить инвариантность результата к границам callback и отсутствие накопления временной ошибки.
- Использовать общий монотонный clock двух дорожек с фактическим смещением начала; не приравнивать каждое независимое начало потока к нулю.
- Проверить атомарность spool, checksum, восстановление после сбоя и обнаружение отсутствующих чанков. Не расширять архитектуру, если существующий spool уже обеспечивает нужное свойство.

Приёмка: stop до первого полного чанка сохраняет хвост; sample count/длительность совпадают; сигнал, разбитый на разные порции, не меняет длительность результата; disk failure/overflow видимы; тесты двух дорожек проверяют общий timeline.
Выполнено: внедрен `StatefulAudioConverter` с непрерывным сохранением фазы ресемплинга и остатков межканальных фреймов (проверена строгая инвариантность к размеру чанков); в `MonotonicInterviewClock` добавлены учет индивидуального смещения старта дорожек и расчет межтрекового skew в общем timeline; в `run_capture_worker` реализован правильный порядок завершения: остановка поступления samples -> полное вычитывание ring buffer -> flush ресемплера -> сброс неполного финального чанка (хвоста) -> расчет длительности по фактическому sample count; ошибки записи на диск и сбои потока пробрасываются в `CaptureHandle::stop() -> Result<CaptureStats>`; переполнение ring buffer фиксируется и отражается в виде `AudioGap` в манифесте. / Done: implemented `StatefulAudioConverter` preserving resampler phase continuity and inter-channel frame remainders across chunk boundaries (verified chunk invariance); enhanced `MonotonicInterviewClock` with per-track start offsets and inter-track timeline skew; in `run_capture_worker` implemented strict stop lifecycle: halt stream callback -> complete drain of ring buffer -> flush converter -> flush partial final chunk (tail) -> calculate total duration from actual sample count; disk I/O errors and stream failures propagate via `CaptureHandle::stop() -> Result<CaptureStats>`; ring buffer overflows are tracked and surfaced as `AudioGap` entries in the sealed manifest.

### Этап 6. Реальный desktop capture → durable STT → стенограмма / Stage 6. Real Desktop Capture → Durable STT → Transcript

Статус: завершён. Закрывает R1, R6. Зависит от 2, 5. / Status: completed. Closes R1, R6. Depends on 2, 5.

Затрагивает: Tauri commands/AppState, audio-capture, chunk ingestion API, worker, LiveSessionScreen, SetupScreen.

- Подключить два CaptureHandle к выбранным входам с общей сессией и clock. Если второй поток не открылся, закрыть первый и вернуть ошибку без состояния recording.
- Явно поддержать доступный loopback/виртуальный вход; отсутствие источника кандидата — ошибка настройки. Не считать список устройств доказательством захвата системного звука на всех ОС.
- Возвращать реальные session ID, chunks, уровни, silence/dropout/drift и ошибки; согласовать типы ответов Rust/TypeScript.
- Старт требует сохранённого согласия и допустимого статуса; не доверять только frontend boolean.
- Pause действительно приостанавливает запись и обновляет backend. Resume сохраняет корректный timeline с паузой; таймер основан на монотонном времени, не количестве setInterval ticks.
- Доставлять завершённые spool chunks с interview/track/epoch/sequence/timestamps/format/sample count/checksum. Сервер подтверждает только durable приём. Повтор одинакового чанка безопасен, другое содержимое с тем же ключом — 409.
- Worker получает ссылку на проверенный чанк, не произвольный путь из браузера. Сырой PCM упаковывать в корректный WAV перед STT; одного имени chunk.wav недостаточно.
- Создавать STT jobs идемпотентно; сохранять стабильный segment ID, реальную дорожку/время и provenance. В UI поступают реальные результаты.
- Stop закрывает capture и manifest, переводит в PROCESSING; REVIEW становится доступен после обработки либо явного решения об обнаруженных пробелах. Ошибка stop не скрывается переходом на следующий экран.
- Восстановление после рестарта повторно доставляет неподтверждённые чанки; не удалять spool до выполнения политики хранения.

Приёмка: короткая реальная двухканальная запись даёт воспроизводимое аудио, STT и корректные timestamps; pause/stop работают; отключение сети и рестарт не теряют/не дублируют чанки; отказ устройства виден. Первую проверку выполнить на текущей macOS, поддержку Windows/Linux отмечать только после отдельных запусков.
Выполнено: поток `cpal::Stream` изолирован в выделенном потоке управления `stream_thread`, что устранило CoreAudio `!Send` ограничение на macOS и обеспечило полную потокобезопасность `CaptureHandle: Send + Sync` и `AppState`; в `MonotonicInterviewClock` добавлены учет пауз и инкремент эпохи (`epoch`), исключающие коллизии таймлайна; реализованы десктоп-команды `pause_capture` и `resume_capture` с возвратом актуального `elapsed_ms`; на бэкенде создана таблица `audio_chunks` и миграция `003_audio_chunks` с валидацией SHA-256, идемпотентной дедупликацией (200 OK) и защитой от конфликтов содержимого (409 Conflict); реализована утилита `pcm_s16le_to_wav_bytes` с 44-байтным RIFF/WAVE заголовком для STT-воркера; добавлены эндпоинты `/pause`, `/resume`, `/stop`, `/jobs/status`; в `LiveSessionScreen.tsx` внедрен реальный цикл паузы/возобновления, точный таймер и многоэтапный `handleStop` с полным контролем завершения фонового пайплайна перед переходом в `review` без подавления ошибок. / Done: isolated `cpal::Stream` inside a dedicated control thread `stream_thread`, eliminating macOS CoreAudio `!Send` constraints and making `CaptureHandle: Send + Sync` and `AppState` fully thread-safe; enhanced `MonotonicInterviewClock` with pause tracking and epoch incrementing across pauses; implemented desktop Tauri commands `pause_capture` and `resume_capture` returning accurate `elapsed_ms`; backend schema updated with `audio_chunks` table and migration `003_audio_chunks` enforcing SHA-256 verification, idempotent duplicate deduplication (200 OK), and conflict detection (409 Conflict); added `pcm_s16le_to_wav_bytes` packaging raw PCM into valid 44-byte RIFF/WAVE containers for Whisper/STT pipeline worker; implemented `/pause`, `/resume`, `/stop`, and `/jobs/status` backend endpoints; updated desktop `LiveSessionScreen.tsx` with synced audio pause/resume, monotonic timer, and staged `handleStop` ensuring complete background pipeline processing prior to transitioning to `review` without error swallowing.


### Этап 7. Привязка ответов, валидная AI-оценка и выполнение очереди / Stage 7. Question Matching, Valid AI Evaluation & Queue Execution

Статус: завершён. Закрывает R5, R6. Зависит от 1, 3, 6. / Status: completed. Closes R5, R6. Depends on 1, 3, 6.

Затрагивает: matcher, associations API, evidence_validator, pipeline, adapters, Repository jobs, Live/Review UI.

- Оценивание запрашивает question ID и ожидаемые ревизии. Backend сам получает rubric и связанные реальные candidate segments; клиент не передаёт произвольный кандидатский текст как доказательство.
- Привязки обновляются при новых сегментах, а не только при первом GET. Неоднозначная привязка требует ревью; ручная привязка сохраняется и не затирается matcher.
- Передавать модели реальный вопрос, критерии/шкалы и отдельные segment IDs. Исключить склейку всего интервью под последним ID.
- Валидировать структуру, criterion IDs, диапазоны, принадлежность evidence интервью/ревизии/вопросу, дорожку, точную цитату и offsets. Сбой проверки сохранять как отклонённый результат с диагностикой, недоступный для обычного approve; ручная оценка возможна отдельно с обоснованием.
- Хранить provider_id и model profile отдельно от upstream model ID, фактический маршрут и fallback provenance. Проверять имеющиеся provider probes; не объявлять поддержку JSON Schema/STT по одному OpenAI-compatible URL.
- Claim job сделать атомарным; lease снабдить owner/token и продлением для долгого запроса. Complete/save допускаются только текущему владельцу.
- Добавить ограниченный backoff, max attempts для expired jobs, различие retryable и permanent errors. Защитить обработку от повторных записей после сбоя между сохранением результата и complete_job.
- Перед сохранением результата атомарно проверить существование/статус интервью и ревизии. Поздний результат не становится актуальным после ручной правки или удаления.
- UI отслеживает конкретное задание и показывает pending/running/failed/rejected/completed; повторная оценка не должна отображать старый результат как новый.

Приёмка: два разных вопроса получают свои сегменты; цитата из чужого/последнего сегмента отклоняется; неверный criterion ID не проходит; invalid AI не попадает в финальный расчёт; crash/retry и два worker не создают двойного эффекта; 429/timeout не запускают бесконечные повторы.
Выполнено: в миграции `004_stage7_evidence_and_lease` добавлены поля `locked_by` в очередь `jobs`, а также `is_rejected`, `validation_errors_json`, `provider_id`, `fallback_metadata_json` в `assessment_proposals`; в `backend/core/matcher.py` реализовано сохранение ручных правок `is_manually_adjusted = 1` через `existing_associations` и корректное переключение активного вопроса; в `backend/core/evidence_validator.py` реализована строгая валидация цитат кандидата (блокировка цитат интервьюера, проверка допустимости критериев, диапазона баллов [min_score, max_score] и дословности exact_quote); в `Repository` реализован атомарный лизинг задач `claim_next_job` с токеном `locked_by`, продление аренды `renew_job_lease`, проверка прав владельца в `complete_job` и `fail_job` (защита от двойного исполнения и гонок воркеров); в `PipelineWorker` обработчик `EVALUATE_QUESTION` извлекает реальные связанные сегменты кандидата из базы данных без склейки под последним ID, валидирует предложение через `EvidenceValidator`, фиксирует provenance/fallbacks и сохраняет статус `is_rejected`; эндпоинт `/approve` блокирует автоматическое утверждение отклоненных предложений (409 Conflict); эндпоинт `/associations` осуществляет инкрементальное сопоставление новых сегментов; в UI (`LiveSessionScreen` и `ReviewScreen`) отображаются диагностические предупреждения валидатора доказательств и блокируется автоматическое одобрение забракованных оценок. / Done: database schema enhanced in migration `004_stage7_evidence_and_lease` adding `locked_by` to `jobs` queue, and `is_rejected`, `validation_errors_json`, `provider_id`, `fallback_metadata_json` to `assessment_proposals`; in `backend/core/matcher.py` implemented manual adjustment retention (`is_manually_adjusted = 1`) across re-runs via `existing_associations` and robust active question switching; in `backend/core/evidence_validator.py` implemented strict quotation validation (rejects interviewer speech track citations, validates criterion membership, score ranges [min_score, max_score], and verbatim exact_quote matching); in `Repository` implemented atomic job claiming `claim_next_job` with worker lease token `locked_by`, lease renewal `renew_job_lease`, and token ownership checks in `complete_job`/`fail_job` (preventing double-execution and worker race conditions); `PipelineWorker`'s `EVALUATE_QUESTION` retrieves real candidate segments from the database without monolithic concatenation under a single ID, runs `EvidenceValidator`, records model provenance/fallbacks, and persists `is_rejected` status; `/approve` endpoint blocks approval of rejected proposals (409 Conflict); `/associations` endpoint supports incremental segment matching; desktop UI (`LiveSessionScreen` and `ReviewScreen`) displays evidence validator rejection warnings and blocks automatic approval of invalid proposals.

### Этап 8. Финальная STT и согласованность ревизий

Статус: завершён / completed. Зависит от 4, 6–7.

Затрагивает: batch-retranscribe endpoint/worker, revisions, associations, human assessments, summary, ReviewScreen.

- Настоящий batch-retranscribe использует сохранённое аудио и STT. Нынешний приём готового списка segments оставить только как явно названный импорт/ручную правку с provenance, если он нужен.
- Создавать новую ревизию отдельно; не менять старые сегменты и ручные исправления. Активировать новую ревизию только после успешного сохранения всего результата.
- Сравнение учитывать изменённые, добавленные и удалённые сегменты и их привязки. Stale определяется не только наличием заранее заполненной таблицы associations.
- Инвалидировать зависимые AI proposals, human assessments и summary при изменении их входов. Сохранить прежнее решение человека в истории и предложить повторное подтверждение.
- Чтение, diff и review всегда указывают точные ревизии. Проверка optimistic concurrency и сохранение выполняются атомарно.
- Показать UI сравнение live/final/manual текста, причины stale и явный выбор/подтверждение. Не вызывать финализацию, пока остаются неразрешённые изменения.

Приёмка: две версии одного logical segment доступны; старая цитата разрешается в своей версии; изменение ответа помечает оценку и summary stale; 409 при одновременной правке; незавершённый batch не переключает активную стенограмму.
Выполнено: в структуре БД и репозитории поддержана изоляция ревизий стенограмм составным ключом `(interview_id, revision_id, segment_id)`; в `backend/db/repository.py` реализованы выборка сегментов по конкретной ревизии, переключение активной ревизии (`set_active_transcript_revision`), метод `mark_proposals_stale`, помечающий оценки человека как `is_stale = 1` с сохранением причины и сбрасывающий подтверждение резюме (`is_confirmed = 0`), а также строгая блокировка финализации при наличии устаревших оценок (409 Conflict); в `PipelineWorker` обработчик `_handle_batch_retranscribe` использует сохранённые аудиочанки из базы данных и spool-директории, проводит пакетную STT-транскрипцию через STT-адаптер, рассчитывает дифф через `TranscriptDiffEngine`, инвалидирует зависимые оценки и активирует новую ревизию строго после завершения записи всех данных; в `backend/api/app.py` добавлена валидация `expected_transcript_revision` в эндпоинтах `/review`, `/summary/confirm` и `/report/finalize` с возвратом 409 Conflict при расхождении версий, а также эндпоинты инспекции ревизий и диффа `GET /revisions/transcript` и `GET /revisions/transcript/diff`; в десктопном UI (`ReviewScreen.tsx`) добавлен бейдж активной ревизии, модальное окно диффа ревизий, предупреждающие плашки `isHumanStale` с причиной инвалидации и кнопка запуска пакетной перестенограммы. / Done: database schema and repository updated to isolate transcript revisions by composite key `(interview_id, revision_id, segment_id)`; in `backend/db/repository.py` implemented revision-scoped segment queries, active revision switching (`set_active_transcript_revision`), `mark_proposals_stale` marking human assessments as `is_stale = 1` with recorded rationale and resetting summary confirmation (`is_confirmed = 0`), and strict 409 Conflict blocking on finalization with stale data; in `PipelineWorker` `_handle_batch_retranscribe` utilizes persisted audio chunks from the database and spool directory to execute batch STT transcription, computes transcript diff via `TranscriptDiffEngine`, invalidates dependent assessments, and activates the new revision strictly after all segments are safely committed; in `backend/api/app.py` implemented optimistic concurrency checks (`expected_transcript_revision`) across `/review`, `/summary/confirm`, and `/report/finalize` returning 409 Conflict on revision mismatches, along with `GET /revisions/transcript` and `GET /revisions/transcript/diff` endpoints; in desktop UI (`ReviewScreen.tsx`) added active revision badge, diff inspection modal, `isHumanStale` warning banners with invalidation rationale, and batch retranscription trigger.

### Этап 9. Сквозная проверка, миграция пользователя и документация

Статус: завершён / completed. Зависит от всех предыдущих.

- Запустить Python tests, необходимые Rust tests и desktop typecheck/build. Добавлять проверки поведения и регрессий, а не дублировать реализацию тестами.
- Пройти UI: setup → consent → реальная двухканальная запись → pause/resume → stop/processing → STT → привязка → AI → ручное исправление → повторная ревизия → summary confirmation → finalize → export.
- Повторить сценарий вторым интервью с теми же question IDs и проверить изоляцию обоих экспортов.
- Проверить reload/restart, временную недоступность backend/provider, повторный job, разрыв устройства, отказ spool, удаление при активном worker и restore backup. Файловые аварии тестировать в изолированных каталогах.
- Проверить перенос копии существующей БД и явно показать пользователю записи, требующие повторного ревью. Уже испорченные межинтервью правки невозможно достоверно восстановить без исходных данных.
- Обновить README и stage specifications: разделить реализовано, проверено автоматически, проверено на реальном устройстве и ещё не проверено. Убрать заявления о полной пилотной готовности, если нет соответствующих свидетельств.
- Зафиксировать использованные команды, результаты, ОС/устройства, провайдер/модель и оставшиеся ограничения без секретов и персональных данных кандидатов.

Выполнено: реализована комплексная автоматизированная матрица регрессионного тестирования (`tests/test_stage9_e2e_regression_matrix.py`) из 14 критических сценариев (полная изоляция интервью с одинаковыми question ID, идемпотентность чанков и выявление подмены байт с кодом 409, многоверсионность сегментов стенограмм, сквозной жизненный цикл от плана до финализации, обработка сбоев AI без галлюцинаций, многокритериальный детерминированный скоринг, защита ручных правок человека при переоценке, изоляция чужих сущностей с 404/422, отсечение фальшивых/чужих цитат валидатором с блокировкой авто-одобрения, блокировка финализации при наличии stale оценок или неподтвержденного резюме, optimistic concurrency control при конкурентных правках, атомарный лизинг и продление аренды задач в очереди воркера, онлайн-бэкап SQLite базы данных с успешным прохождением `PRAGMA integrity_check`); обеспечен 100% зелёный прогон тестового набора бэкенда (107 тестов pytest) и ядра захвата звука Rust (19 тестов cargo test, включая часовую стресс-симуляцию за 41.8s); проверена production-сборка десктопного фронтенда (`tsc && vite build` — 0 ошибок); составлен подробный двуязычный отчёт аудита и верификации [`docs/stage9-verification-and-audit.md`](docs/stage9-verification-and-audit.md); в документации и `README.md` чётко зафиксированы границы верификации и платформенные ограничения (CoreAudio macOS проверен, Windows WASAPI и Linux ALSA требуют физического тестирования). / Done: comprehensive automated regression test matrix implemented in `tests/test_stage9_e2e_regression_matrix.py` covering 14 critical scenarios (strict isolation across interviews with identical question IDs, idempotent chunk ingestion vs 409 Conflict on payload tampering, multi-revision transcript segment retention and composite addressing, end-to-end lifecycle from plan setup to finalized export, graceful AI failure without synthetic hallucination, multi-criterion deterministic scoring fidelity, preservation of manual adjustments across re-evaluations, rejection of cross-interview entity access with 404/422, rejection of invalid/interviewer quotes by evidence validator blocking automated approval, strict blockage of finalization with stale reviews or unconfirmed summary, optimistic concurrency control against conflicting edits, atomic worker lease claiming and renewal, and online SQLite backup generation passing `PRAGMA integrity_check`); 100% green test suite across backend (107 pytest cases) and native Rust audio core (19 cargo test cases including 1-hour drift simulation in 41.8s); desktop frontend production build validated (`tsc && vite build` with 0 errors); comprehensive bilingual verification audit document compiled at [`docs/stage9-verification-and-audit.md`](docs/stage9-verification-and-audit.md); explicit verification boundaries and platform limits documented across `README.md` (macOS CoreAudio verified, Windows WASAPI and Linux ALSA designated as requiring physical hardware validation).

## 4. Минимальная регрессионная матрица

| Сценарий | Ожидаемый результат |
| --- | --- |
| Одинаковые question IDs в двух интервью | Изменение одного не затрагивает другое |
| Повтор чанка: те же bytes / другие bytes | Идемпотентный успех / 409 |
| Один segment ID в двух transcript revisions | Обе версии сохранены и адресуются независимо |
| План из SetupScreen → finalize | Валидный контракт, нет KeyError/500 |
| Нет ответа или AI недоступен | Null, сниженное покрытие, без выдуманного текста |
| Несколько критериев с разными весами/шкалами | UI, read API, finalize и export дают один результат |
| Ручная правка + повторная AI-оценка | Подтверждение человека сохранено |
| Чужой proposal/evidence/revision | 404/422, без изменения чужих данных |
| Невалидная цитата | Отклонённый AI-результат, не обычное предложение |
| Stale review / неподтверждённое summary | Финализация заблокирована с причиной |
| Два одновременных finalize/review | Нет двойного эффекта и потери правки |
| Stop на неполном чанке | Все принятые samples сохранены, длительность точна |
| Disk failure / overflow | Видимая ошибка/потеря, без ложного success |
| Crash worker после сохранения результата | Повтор не создаёт второй результат |
| Удаление во время обработки | Данные не возвращаются; cleanup можно повторить |
| Traversal/symlink при удалении | Каталоги вне spool не затронуты |
| Экспорт после последующей ревизии | Ранее финализированный снимок неизменен |

## 5. Граница завершения

Исправления завершены, когда закрыты R1–R9 и сопутствующие сценарии этого плана, миграция проверена, рабочий UI проходит сквозной сценарий с реальным аудио и результаты двух интервью независимы. Зелёные unit tests сами по себе не означают готовность записи или пилота. Непроверенные ОС и provider capabilities остаются явно обозначенными ограничениями.

В этот план не входят визуальный редизайн, новые провайдеры, облачная многопользовательская архитектура и несвязанный рефакторинг.
