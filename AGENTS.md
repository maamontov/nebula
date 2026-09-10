# AGENTS.md

Инструкции действуют для всего репозитория Nebula. Цель — вносить минимальные,
проверяемые изменения, не ломая сквозные гарантии между Desktop, аудиоядром и backend.

## Что это за проект

Nebula — локальный кроссплатформенный AI-помощник интервьюера:

- `apps/desktop/` — React 19 + TypeScript + Vite, упакованные в Tauri 2;
- `apps/desktop/src-tauri/` — Tauri-команды, состояние аудиозахвата и uploader;
- `crates/audio-capture/` — Rust-ядро захвата, ресемплинга, clock/drift и spool;
- `apps/audio-spike/` — CLI для устройств и синтетических аудиопроверок;
- `backend/api/` — FastAPI HTTP API;
- `backend/workers/` — очередь STT/LLM-задач;
- `backend/core/` — доменные правила, evidence, scoring, revisions и state machine;
- `backend/db/` — SQLite schema, миграции и repository;
- `contracts/` — Pydantic-контракты домена, аудио и провайдеров;
- `tests/` — Python unit/integration/e2e tests;
- `evals/` — проверки провайдеров и stage-сценарии;
- `docs/` — спецификации и исторические планы реализации.

Код и тесты описывают текущее поведение точнее старых stage-документов. Если документация
расходится с реализацией, сначала установи ожидаемое поведение по актуальным контрактам,
тестам и вызывающему коду; не подгоняй код под устаревший отчёт без отдельного обоснования.

## Неприкосновенные продуктовые инварианты

- Это decision-support, а не автономный судья: AI предлагает, человек подтверждает.
- Итоговый score и coverage вычисляет backend детерминированно. Пропущенный вопрос или
  техническая ошибка AI не превращаются в нулевой балл.
- Любая оценка должна ссылаться на проверяемые evidence из речи кандидата. Реплики
  интервьюера и `unknown` из `single_source` нельзя использовать как evidence кандидата.
- Ручные решения и правки не перезаписываются результатами AI или retranscription.
  Изменение транскрипта создаёт ревизию и корректно помечает зависимые данные stale.
- Соблюдай изоляцию по `interview_id`, привязку к `rubric_revision_id` и
  `transcript_revision_id`, OCC/409-конфликты, идемпотентность чанков и ownership lease.
- `FINALIZED` — неизменяемый снимок отчёта; `DELETED` — терминальное состояние.
- Поддерживай оба режима захвата: `dual_source` и `single_source`. Для shared track роль
  по умолчанию — `unknown`, пока её явно не подтвердил человек.
- Обычный browser/Vite-режим не заменяет Tauri и не должен имитировать успешный capture.
  Mock допустим только в явно включённом demo-режиме `window.NEBULA_ENABLE_DEMO_MOCK`.
- OpenAI-compatible не означает GPT и не гарантирует STT, JSON Schema, reasoning, SSE или
  одинаковые параметры. Храни `provider_id` отдельно от точного upstream `model_id` и
  отправляй только подтверждённые capability конкретного профиля.
- Секреты остаются в переменных окружения. Не коммить `.env`, ключи, реальные токены,
  содержимое интервью, аудиофайлы, spool, SQLite DB или runtime logs.

## Как вносить изменения

1. Перед правкой найди полный путь данных: UI/API client → FastAPI/Tauri command →
   contract → repository/core/worker → storage или provider.
2. Меняй только нужные слои, но не оставляй рассинхронизированные контракты. При изменении
   API/IPC проверь Python-модели, endpoint, repository, TypeScript types/client и Rust
   command payload там, где они участвуют.
3. Для схемы БД обновляй и bootstrap schema, и последовательную миграцию. Миграция должна
   сохранять существующие данные, быть повторно безопасной и проходить integrity checks.
   Не редактируй пользовательскую `data/nebula.db` вручную.
4. Для новых фоновых операций сохраняй атомарный claim/lease, retry semantics,
   идемпотентность и явный terminal failure; не допускай тихой потери задания.
5. Для AI-ответов валидируй структуру и evidence программно. Не показывай reasoning traces
   и не маскируй ошибки провайдера правдоподобными результатами.
6. Не редактируй вручную `apps/desktop/src-tauri/gen/schemas/` и generated/build output.
7. Обновляй документацию, если меняется контракт, пользовательский workflow или реальная
   граница верификации. Не фиксируй в README счётчики тестов, если не пересчитал их сейчас.

## Проверки

Сначала запускай узкую проверку затронутого слоя, затем достаточный регрессионный набор.

### Python / backend / contracts

```bash
uv sync
uv run ruff check .
uv run pytest -q tests/test_<relevant_area>.py
uv run pytest -q
```

Полный `pytest` обязателен для изменений общих contracts, schema/migrations/repository,
state machine, scoring, revisions, worker pipeline или API-поведения.

### Rust / audio / Tauri

```bash
cargo fmt --all -- --check
cargo test --workspace
```

При изменении реального захвата дополнительно используй подходящий `audio-spike`, но чётко
отделяй unit/synthetic результат от проверки на физическом устройстве и конкретной ОС.
Сборка на macOS не доказывает работу Windows WASAPI или Linux-аудиостека.

### Desktop

```bash
npm --prefix apps/desktop ci
npm --prefix apps/desktop run build
```

Отдельного frontend test runner сейчас нет: `build` подтверждает typecheck и bundle, но не
доказывает корректность Tauri IPC, аудиоустройств или полного пользовательского сценария.
Для изменений сквозного пути добавь/обнови соответствующий Python e2e/contract test.

### Полная локальная проверка

```bash
uv run ruff check .
uv run pytest -q
cargo fmt --all -- --check
cargo test --workspace
npm --prefix apps/desktop run build
git diff --check
git status --short
```

Не запускай provider probes без явной необходимости: они обращаются к внешнему сервису,
могут расходовать квоту и требуют секретов. Проверяй нового провайдера через
`evals/provider_probe.py` и фиксируй конкретную пару provider/model и реально доказанные
capabilities.

## Запуск живого стека

Для обычной проверки предпочитай точечные команды. `./scripts/start.sh` и
`./scripts/stop.sh` управляют фоновыми процессами, портами 8000/1420, `.run/` и логами;
текущая реализация может принудительно завершать занявший порт процесс, `vite` или
`nebula-desktop`. Не запускай эти скрипты автоматически в общей среде разработки.

Когда интеграционный запуск действительно нужен:

```bash
./scripts/start.sh backend
./scripts/status.sh
./scripts/nebula.sh logs backend
./scripts/stop.sh backend
```

Полный desktop workflow требует backend + worker + Tauri, корректного `.env`, разрешений
ОС и физических аудиоустройств. В отчёте всегда разделяй: static/build evidence,
автоматические тесты, synthetic audio, внешний provider probe и ручную hardware/UI-проверку.

## Перед завершением

- Проверь `git diff` и `git status --short`, включая untracked-файлы.
- Не трогай несвязанные пользовательские изменения и runtime data.
- Перечисли выполненные проверки и всё, что осталось непроверенным.
- Не утверждай совместимость с ОС, устройством или провайдером без проверки именно в этом
  окружении.
