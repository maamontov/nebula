# План: цельные реплики стенограммы для `single_source`

## Статус документа

План предназначен для реализации другим агентом. Он описывает требуемое поведение,
границы изменений, порядок работ и проверки. На момент составления плана код не
изменялся и live/provider-проверки не выполнялись.

## Цель

В режиме `single_source` перестать показывать стенограмму как набор произвольных
фрагментов длительностью 1–4 секунды. Технические аудиочанки должны остаться мелкими
и надёжными, но STT должен получать цельные речевые блоки, сформированные по паузам,
а один завершённый речевой блок должен сохраняться как один `TranscriptSegment`.

Ожидаемый результат:

- обычная реплика длиной 5–12 секунд отображается одной карточкой;
- короткая пауза внутри предложения не создаёт новый segment;
- явная пауза между репликами закрывает текущий segment;
- результат не зависит от скорости uploader-а и размера backlog worker-а;
- pause/resume, разрыв последовательности и смена `capture_epoch` никогда не
  склеивают звук в одну реплику;
- `shared` по-прежнему получает `speaker_role="unknown"`, пока человек явно не
  назначит роль;
- ручные роли, split, transcript revisions, OCC и evidence-инварианты сохраняются.

## Текущее поведение и причина проблемы

1. `apps/desktop/src-tauri/src/commands.rs` запускает захват `single_source` как
   `TrackType::Shared` с `chunk_duration_ms = 1000`.
2. `crates/audio-capture/src/capture.rs` пишет независимые секундные PCM-чанки в
   spool. Это корректная единица хранения и доставки.
3. `backend/db/repository.py::save_audio_chunk` создаёт отдельную
   `TRANSCRIBE_AUDIO` job для каждого чанка и копирует PCM в `payload_json` как
   `audio_hex`.
4. `backend/workers/pipeline.py::_handle_transcribe` берёт первый чанк и
   оппортунистически присоединяет максимум три уже ожидающих соседних job.
5. Если uploader и worker идут почти синхронно, впереди часто нет достаточного
   backlog. Поэтому в STT попадает от одной до четырёх секунд, а границы зависят от
   планирования процессов, а не от речи.
6. Worker сохраняет один финальный `TranscriptSegment` на каждый такой запрос.
   `LiveSessionScreen.tsx` и `ReviewScreen.tsx` честно рисуют каждый backend segment
   отдельно и сами проблему не создают.
7. Текущий `BATCH_RETRANSCRIBE` также вызывает STT для каждого сохранённого
   секундного чанка отдельно. Это не полноценный контекстный batch-проход.
8. `capture_epoch` сохраняется в `audio_chunks`, но не попадает в текущий STT job
   payload. Поэтому проверка epoch в `claim_adjacent_transcribe_jobs` фактически
   получает `None` и не является достаточной границей pause/resume.
9. Профиль PlusVibe Whisper сейчас явно содержит `provides_timestamps=False` и
   `provides_partials=False`; адаптер отправляет только `model` и `language`, затем
   читает только поле `text`. Нельзя проектировать решение так, будто upstream уже
   возвращает word/segment timestamps.

## Принятые архитектурные решения

### 1. Не менять размер capture-чанка

Оставить 1000 мс. Увеличение до 10–15 секунд повысит задержку, ухудшит
восстановление после сбоя и увеличит объём потенциально потерянного хвоста. Размер
транспортного чанка не должен задавать размер пользовательской реплики.

### 2. Собирать реплики на backend до обращения к STT

Добавить отдельный чистый модуль `backend/core/turn_assembler.py`. Он получает
упорядоченный PCM одного `(interview_id, track_id, capture_epoch)`, анализирует
короткие аудиофреймы и возвращает ноль или несколько законченных диапазонов плюс
незакрытый хвост.

Не использовать пунктуацию результата STT как основной детектор границ: она
появляется слишком поздно, зависит от провайдера и не решает live-латентность.

### 3. Не хранить вторую копию аудио

Готовая STT-job должна ссылаться на диапазон существующих `audio_chunks`, а не
содержать объединённый WAV или большой `audio_hex` в SQLite. Worker реконструирует
PCM/WAV из файлов чанков при каждой попытке. Это сохраняет retry и не раздувает DB.

### 4. Разделить событие поступления чанка и STT реплики

Сохранить существующий `TRANSCRIBE_AUDIO` как durable-сигнал о поступлении чанка,
но изменить его обработчик: он двигает assembler и создаёт `TRANSCRIBE_TURN` только
для законченных диапазонов. Сам провайдер вызывается только обработчиком
`TRANSCRIBE_TURN`.

Такой подход не требует опасно переписывать уже существующие строки `jobs`. Для
старых pending jobs без `capture_epoch` оставить узкий legacy fallback: разрешить
текущий прямой STT-путь либо однозначно восстановить epoch/sequence по
`audio_chunks`. Если соответствие неоднозначно, job должна завершиться явной
ошибкой, а не склеивать разные эпохи.

## Границы первой версии VAD

Первая версия должна быть простой и детерминированной, без diarization и тяжёлых
ML-зависимостей:

- вход: mono PCM S16LE 16 kHz; другие поддержанные форматы сначала нормализовать
  существующим аудиокодом;
- анализ: фреймы 20–30 мс;
- использовать RMS/mean absolute amplitude в dBFS с гистерезисом;
- порог должен учитывать измеренный noise floor, а не повторять текущий почти
  абсолютный `mean_abs < 12`;
- добавить небольшой pre-roll, чтобы не обрезать начало слова;
- стартовое закрытие реплики: 800 мс непрерывной тишины;
- стартовая максимальная длительность реплики: 12 секунд;
- pause, stop, новый epoch, gap и конец sealed manifest принудительно закрывают
  непустой хвост;
- чистая тишина не создаёт segment и не отправляется в STT;
- первая версия не обязана показывать unstable partial text.

Конкретные энергетические пороги не считать доказанными до проверки на реальных
записях. Вынести их в именованные backend-константы, но не добавлять пользовательскую
конфигурацию до появления измеренной необходимости.

При достижении максимальной длительности допускается принудительный разрез без
overlap в первой версии. Не добавлять текстовую дедупликацию на основе эвристик:
она может незаметно менять verbatim evidence. Если потери слов на границе будут
подтверждены измерениями, overlap и provenance проектируются отдельно.

## Состояние assembler и миграция 009

Обновить одновременно bootstrap schema и `backend/db/migrations.py`. Следующая
актуальная миграция — `009_transcript_turn_assembly`.

Минимальная таблица:

```sql
CREATE TABLE transcript_assembly_state (
    interview_id TEXT NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    track_id TEXT NOT NULL,
    capture_epoch INTEGER NOT NULL,
    next_sequence INTEGER NOT NULL DEFAULT 0,
    next_sample_offset INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (interview_id, track_id, capture_epoch)
);
```

`next_sequence + next_sample_offset` обозначают первый ещё не зафиксированный
sample. Незавершённый хвост не копируется в таблицу: при следующем событии assembler
повторно читает небольшой диапазон исходных чанков от cursor. Максимальная длина
реплики ограничивает стоимость такого повторного анализа.

Требования к миграции:

- `CREATE TABLE IF NOT EXISTS`;
- безопасный повторный запуск;
- каскадное удаление вместе с interview;
- запись версии 9 только после успешного изменения схемы;
- `verify_integrity()` после миграции;
- существующие interview, chunks, transcript revisions и jobs не изменять;
- добавить таблицу и нужный индекс также в `backend/db/schema.sql`.

Если во время реализации окажется, что одного cursor недостаточно для атомарного
recovery, допустимо добавить `open_turn_start_*`, но сначала доказать необходимость
тестом. Не создавать отдельную таблицу аудиоблобов или универсальный event framework.

## Формат `TRANSCRIBE_TURN`

Payload готовой реплики должен содержать как минимум:

```json
{
  "track_id": "shared",
  "capture_epoch": 2,
  "first_sequence": 15,
  "first_sample_offset": 3200,
  "last_sequence": 24,
  "last_sample_offset": 12800,
  "start_ms": 15300,
  "end_ms": 24800,
  "sample_rate": 16000,
  "channels": 1,
  "language": "ru"
}
```

Job ID и segment ID должны быть детерминированными от source range. Не использовать
случайный UUID:

```text
stt-turn-{interview_id}-{track_id}-{epoch}-{first_seq}-{first_offset}-{last_seq}-{last_offset}
seg-{interview_id}-{track_id}-{epoch}-{first_seq}-{first_offset}-{last_seq}-{last_offset}
```

Если эти строки могут выйти за практичный размер, использовать стабильный SHA-256
канонического source-range payload, сохраняя сам диапазон в payload.

## Транзакционные гарантии repository

Добавить узкие методы, а не выполнять SQL из worker:

1. Получить непрерывные `audio_chunks` от текущего cursor в пределах одного track и
   epoch.
2. Атомарно:
   - продвинуть cursor через завершённые реплики и отброшенную ведущую тишину;
   - `INSERT OR IGNORE` детерминированные `TRANSCRIBE_TURN` jobs;
   - не продвигать cursor через незавершённый хвост;
   - не создавать jobs после `DELETED` или `FINALIZED`.
3. Собрать байты source range для `TRANSCRIBE_TURN` с проверкой:
   - всех sequence;
   - checksum сохранённых файлов;
   - sample rate/channels/format;
   - точных sample offsets.

Создание turn-job и продвижение cursor должны происходить в одной DB-транзакции.
Повтор обработки того же chunk event не должен создавать второй turn-job.

Не вызывать внешний STT внутри DB-транзакции.

## Изменения worker

В `PipelineWorker.process_one_job` добавить явный обработчик `TRANSCRIBE_TURN`.

### Обработка `TRANSCRIBE_AUDIO`

- проверить существование и состояние interview;
- извлечь/восстановить track, epoch и sequence;
- запросить у repository непрерывный непросмотренный диапазон;
- передать PCM в `TurnAssembler`;
- атомарно записать новый cursor и turn-jobs;
- завершить chunk-event job;
- не обращаться к STT напрямую для новых jobs.

Удалить `claim_adjacent_transcribe_jobs` после перевода всех вызовов и тестов, если
он больше нигде не нужен. Не оставлять два конкурирующих способа группировки.

### Обработка `TRANSCRIBE_TURN`

- восстановить PCM из `audio_chunks` по диапазону;
- завернуть его в WAV через существующий `pcm_s16le_to_wav_bytes`;
- вызвать текущий STT adapter один раз;
- применить существующую фильтрацию пустого текста и известных silence
  hallucinations;
- сохранить ровно один финальный segment с точными start/end;
- для `track_id="shared"` установить только `speaker_role="unknown"`;
- сохранить segment идемпотентно;
- после успешной записи завершить job;
- ошибки upstream обрабатывать существующим retry/lease механизмом.

Нельзя объявлять job завершённой до устойчивой записи segment. Поздний результат
после удаления interview должен быть отброшен существующей anti-resurrection
проверкой.

## Pause, stop и readiness

Текущий `/stop` и readiness gate считают чанки и STT jobs. После изменения они
должны различать три состояния:

1. все ожидаемые аудиочанки загружены;
2. assembler обработал их и принудительно сбросил открытый хвост;
3. все созданные `TRANSCRIBE_TURN` jobs завершены.

На pause:

- текущий epoch закрывается;
- непустая открытая реплика сбрасывается;
- новая реплика после resume начинается только в новом epoch.

На stop/sealed manifest:

- последний непустой хвост превращается в `TRANSCRIBE_TURN`, даже если после него
  нет 800 мс тишины;
- readiness не возвращает `READY_FOR_REVIEW`, пока эта job не завершена или не
  перешла в явный terminal failure;
- terminal failure должен быть виден пользователю и не маскироваться пустой
  стенограммой.

## `single_source`, роли и evidence

Это изменение не является diarization.

- Все новые segments дорожки `shared` остаются `unknown`.
- VAD сообщает только границы речевой активности.
- `unknown` и `interviewer` нельзя включать в candidate evidence.
- Роль назначает человек существующим `speaker-role` endpoint.
- Сегмент с двумя говорящими делится существующим `split` endpoint.
- Изменение роли или split продолжает создавать новую transcript revision,
  переносить ассоциации безопасным способом и помечать зависимые оценки stale.
- Не использовать LLM-классификацию текста для автоматического подтверждения роли.

Позже можно добавить diarization как отдельную предлагаемую разметку с confidence,
но она не входит в этот change set.

## Финальная перестенограмма

Переделать `_handle_batch_retranscribe`, чтобы он не отправлял каждый секундный
`audio_chunk` отдельно.

Первая версия должна:

1. заново собрать исходное аудио по track/epoch;
2. применить тот же `TurnAssembler` детерминированно;
3. вызвать STT на каждой цельной реплике;
4. создать новую transcript revision только после записи всех segments;
5. активировать её только при наличии непустого успешного результата;
6. переносить ручные роли по однозначному временному overlap;
7. оставлять роль `unknown`, если новый segment пересекает разные ручные роли;
8. инвалидировать затронутые proposals/human assessments/summary существующим
   механизмом stale;
9. не перезаписывать live revision и ручные правки на месте.

Не добавлять `verbose_json`, word timestamps, prompt continuation или streaming по
предположению. Их можно использовать только после отдельного provider probe для
конкретной пары PlusVibe/`whisper-large-v3-turbo` и обновления `STTProfile`.

## UI

Основной эффект должен появиться без клиентской склейки: UI уже отображает один
элемент на backend segment.

Обязательное изменение UI только одно: состояние завершения интервью не должно
переходить в review, пока assembler/turn-STT не закончили flush.

Допустимое небольшое улучшение:

- показывать нейтральный индикатор «Распознаётся текущая реплика…», пока существует
  открытый assembler tail или pending `TRANSCRIBE_TURN`;
- не создавать fake partial transcript;
- не объединять `unknown` segments только визуально: это может склеить двух людей и
  усложнить ручную разметку.

## Файлы в ожидаемом change set

Основные:

- `backend/core/turn_assembler.py` — чистая логика VAD и границ;
- `backend/workers/pipeline.py` — assembly и `TRANSCRIBE_TURN`;
- `backend/db/schema.sql` — bootstrap schema;
- `backend/db/migrations.py` — миграция 009;
- `backend/db/repository.py` — cursor, выборка диапазонов и атомарные jobs;
- `backend/api/app.py` — flush/readiness/status;
- `tests/test_turn_assembler.py` — unit tests алгоритма;
- `tests/test_pipeline_worker.py` — job lifecycle и STT;
- `tests/test_stage6_audio_ingestion.py` — ingest/readiness;
- `tests/test_single_source_and_startup.py` — роли и single-source;
- `tests/test_stage8_retranscribe_consistency.py` — финальная ревизия.

Возможные, только если реально потребуются контрактом:

- `contracts/audio.py`;
- `contracts/domain.py`;
- `apps/desktop/src/services/api.ts`;
- `apps/desktop/src/screens/LiveSessionScreen.tsx`.

Не менять Rust capture, длительность чанка и generated Tauri schemas без
доказанной необходимости.

## Порядок реализации

### Шаг 1. Зафиксировать baseline

- проверить `git status --short` и сохранить несвязанные изменения пользователя;
- запустить узкие существующие тесты pipeline/audio ingestion/single source;
- не запускать live stack и provider probe автоматически.

### Шаг 2. Реализовать чистый assembler

- создать структуры input/output без зависимости от repository;
- покрыть синтетическими PCM fixtures;
- проверить тишину, короткую/длинную речь, несколько реплик, forced max split,
  tail flush и gap/epoch boundary.

### Шаг 3. Добавить migration/schema/repository

- migration 009 и bootstrap schema;
- cursor read/update;
- чтение непрерывных chunk ranges;
- атомарное создание детерминированных turn-jobs;
- integrity и idempotency tests.

### Шаг 4. Перевести live worker

- `TRANSCRIBE_AUDIO` становится assembly trigger;
- `TRANSCRIBE_TURN` выполняет реальный STT;
- добавить `capture_epoch` в новые chunk job payload;
- удалить старую оппортунистическую группировку после перевода тестов;
- проверить lease expiry, retry, duplicate event и deletion during STT.

### Шаг 5. Исправить stop/readiness

- flush открытых хвостов на pause/stop/manifest seal;
- ожидать assembly и turn-STT;
- выдавать отдельную диагностику assembly/STT failure.

### Шаг 6. Перевести batch retranscribe

- переиспользовать тот же assembler;
- сохранить revision/OCC/stale guarantees;
- проверить перенос ручных ролей и неоднозначный overlap.

### Шаг 7. Минимально обновить UI и документацию

- только реальный processing status, если backend его предоставляет;
- описать разницу между chunk, turn и transcript segment;
- не заявлять diarization или streaming STT.

## Обязательные автоматические сценарии

### Turn assembler

- 10 секунд непрерывной речи → одна реплика;
- речь, 300 мс тишины, речь → одна реплика;
- речь, 900 мс тишины, речь → две реплики;
- чистая тишина → ни одной STT-job;
- 25 секунд речи → ограниченное число forced segments, покрывающих весь звук без
  пропусков и overlap;
- последний хвост без завершающей тишины → segment после flush;
- gap или новый epoch → два независимых segment;
- фоновый шум ниже адаптивного порога не держит реплику открытой бесконечно.

### Repository/jobs

- повтор chunk event не создаёт второй `TRANSCRIBE_TURN`;
- crash между assembly и STT восстанавливается по lease;
- cursor и turn-job фиксируются атомарно;
- missing/corrupt chunk даёт явную ошибку;
- segment ID стабилен после retry;
- удалённое/finalized interview не получает новые результаты;
- legacy pending job без epoch обрабатывается безопасным fallback или явно падает.

### Single-source domain

- каждый `shared` turn сохраняется как `unknown`;
- `unknown` не попадает в оценивание;
- ручное назначение `candidate` разрешает evidence только в новой активной revision;
- mixed-speaker turn можно разделить без потери parent provenance;
- pause/resume не склеивает реплики.

### Batch revision

- batch использует turn ranges, а не секундные chunks;
- новая revision активируется только после полной успешной записи;
- пустой/неуспешный batch не переключает active revision;
- изменившийся текст помечает связанные решения stale;
- подтверждённые ручные данные не перезаписываются автоматически.

## Полная проверка

После узких тестов выполнить:

```bash
uv run ruff check .
uv run pytest -q
cargo fmt --all -- --check
cargo test --workspace
npm --prefix apps/desktop run build
git diff --check
git status --short
```

Provider probe выполнять только отдельно и осознанно: он требует секрета, расходует
квоту и доказывает возможности только конкретной пары provider/model.

## Ручная проверка, которую нельзя заменить unit-тестами

На разрешённой тестовой записи или реальном интервью проверить:

- 10–15 минут разговора через общий микрофон/микс;
- обычные паузы, перебивания, тихую речь и фоновый шум;
- задержку от конца реплики до появления текста;
- сохранность терминов, отрицаний и чисел;
- удобство ручного назначения роли цельной реплике;
- отсутствие склейки через pause/resume.

Целевой live-порог из архитектурного плана: p95 не более 3 секунд от конца
речевого блока до финального текста при штатной сети. Автотесты и frontend build не
доказывают достижение этого порога.

## Критерии завершения

Изменение готово, когда одновременно выполнено следующее:

- размер capture-чанка остался 1000 мс;
- границы live segments определяются аудио, а не случайным backlog;
- типичная цельная реплика отображается одной карточкой;
- каждый source sample либо относится к известному turn/silence/gap, либо остаётся
  в явно открытом хвосте;
- retry, lease expiry и рестарт не создают дублей и не теряют звук;
- никакой segment не пересекает `capture_epoch`;
- batch retranscribe больше не распознаёт секундные чанки по отдельности;
- `shared → unknown` и candidate-evidence invariant сохранены;
- migration 009 проходит на новой и существующей DB с successful integrity check;
- полный набор проверок зелёный;
- границы ручной hardware/provider-проверки явно указаны в отчёте.

## Не входит в задачу

- автоматическое определение личности говорящего;
- автоматическое подтверждение роли кандидата;
- streaming STT или fake partials;
- смена STT-провайдера;
- увеличение capture-чанка;
- LLM-редактирование текста стенограммы;
- скрытая коррекция verbatim evidence;
- несвязанный рефакторинг очереди, UI или аудиоядра.
