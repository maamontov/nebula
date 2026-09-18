# Nebula: AI-помощник для проведения собеседований / AI Interview Copilot

[English version below](#english)

---

## Русский

Nebula — кроссплатформенный AI-помощник для проведения технических и профессиональных собеседований.

Программа поддерживает два режима аудиозахвата:
- **Двухканальный (`dual_source`)**: раздельный захват интервьюера и кандидата по двум изолированным каналам с отслеживанием дрейфа тактовых генераторов (drift/skew).
- **Одноканальный (`single_source`)**: захват с одного физического устройства (микрофон совещания или системный микс) в общую дорожку `shared` с маркировкой `unknown` и обязательной человеческой верификацией ролей говорящих перед отправкой в AI-оценку.

Ключевой продуктовый принцип: **Nebula является системой поддержки принятия решений, а не автономным судьёй**. Оценки AI носят исключительно рекомендательный характер, а итоговое решение всегда принимает и подтверждает интервьюер.

### Ключевые архитектурные принципы
1. **Независимость от OpenAI и GPT**: работа ведётся через стандартный протокол `POST /v1/chat/completions` с открытыми или локальными моделями (Qwen, Llama, DeepSeek и др.). Никаких скрытых зависимостей от проприетарных сервисов OpenAI.
2. **Детерминированный расчет баллов**: модель оценивает факты и соответствие критериям, но итоговый балл и покрытие рассчитываются детерминированной математической формулой бэкенда.
3. **Программная валидация цитат (Evidence)**: любая оценка требует верифицируемой ссылки на транскрипт. Галлюцинации, фейковые цитаты и попытки prompt injection отсекаются валидатором.
4. **Изоляция авторства речи**: в режиме `single_source` неразмеченная общая речь (`unknown`) или реплики интервьюера категорически исключены из контекста оценки ответов кандидата.
5. **Защита от дискриминации**: любые суждения по нерелевантным личным признакам (акцент, пол, тембр речи, возраст) блокируют утверждение оценки.
6. **Независимая настройка AI-провайдеров и горячая перезагрузка**: независимое конфигурирование STT и LLM через экран Desktop и REST API. Секреты хранятся изолированно от БД в версионированных защищённых файлах, ключи write-only и никогда не возвращаются в API. Обновление конфигурации воркера происходит на лету без перезапуска приложения с транзакционной защитой от конфликтов (OCC).

### Структура репозитория
```text
crates/
  audio-capture/  # Нативное ядро захвата звука (Rust, CoreAudio/WASAPI, ringbuffer, spool)
apps/
  audio-spike/    # CLI-утилита для спайка и стресс-тестирования аудиозахвата
  desktop/        # Tauri 2 + React десктопное приложение (Этап 3+)
contracts/        # Pydantic v2 и JSON Schema контракты (провайдеры, аудио, домен)
backend/
  core/           # Доменная логика: детерминированный скоринг, state machine, evidence validator
  adapters/       # OpenAI-compatible LLM адаптер без завязки на GPT
  api/            # FastAPI приложение и эндпоинты
evals/            # Provider probe CLI для проверки любых non-GPT провайдеров
tests/            # Набор модульных и интеграционных тестов (pytest)
docs/             # Концепция, план реализации, спецификации контрактов и аудио
```

### Быстрый старт
```bash
# 1. Установка зависимостей и запуск тестов Python (бэкенд и контракты)
uv sync
uv run pytest -v
uv run ruff check .

# 2. Запуск тестов нативного ядра захвата звука (Rust)
cargo test --all

# 3. Инспекция аудиоустройств ввода/вывода (CoreAudio / WASAPI)
cargo run -p audio-spike -- list-devices

# 4. Запуск часового стресс-теста аудиозахвата со сведением дрейфа
cargo run --release -p audio-spike -- synthetic-test --simulated-duration-sec 3600

# 5. Запуск диагностического пробника LLM-провайдера
uv run python evals/provider_probe.py --base-url http://localhost:11434/v1 --model qwen2.5:7b

# 6. Сборка цельного Desktop-приложения (FastAPI и worker встраиваются внутрь .app)
npm --prefix apps/desktop ci
npm --prefix apps/desktop run tauri -- build --no-sign
```

После запуска собранного приложения FastAPI и pipeline worker поднимаются автоматически;
отдельный запуск `scripts/start.sh backend` или `scripts/start.sh worker` не требуется.

На Windows та же команда создаёт `target/release/bundle/nsis/*-setup.exe` и MSI,
на Linux — AppImage и DEB. Python sidecar должен собираться на целевой ОС:
кросс-сборка одного и того же sidecar между macOS, Windows и Linux не поддерживается.

### Управление сервисами (CLI)
```bash
./scripts/start.sh                    # Dev: Backend + Worker + Desktop отдельными процессами
./scripts/start.sh backend            # Запуск только API сервера
./scripts/status.sh                   # Мониторинг PID, здоровья API и целостности SQLite WAL
./scripts/stop.sh                     # Корректная остановка всех сервисов
./scripts/restart.sh                  # Перезапуск системы
./scripts/nebula.sh logs worker -f    # Просмотр логов воркера в реальном времени
```

### Статус верификации и границы готовности
- **Автоматически проверено (100% Green)**:
  - 212 тестов Python/pytest (FastAPI эндпоинты, изоляция SQLite, детерминированный скоринг, валидатор доказательств, optimistic concurrency control, конечный автомат сессий, атомарный лизинг воркера, полная сквозная регрессионная матрица из 14 сценариев, поддержка single-source, сквозной тест десктопного пайплайна `test_desktop_pipeline_e2e.py`, адаптивные follow-up вопросы и 27 тестов устранения задержек AI и дедупликации).
  - 24 теста Rust (`cargo test --workspace`), включая lock-free ringbuffer, WAV spooling, IPC-контракты Tauri desktop и синтетическую часовую 2-канальную запись с компенсацией дрейфа тактовых генераторов (3600с симулированы за 57с без потерь).
  - Production-сборка десктопного фронтенда (`tsc && vite build` — 0 ошибок сборки и типизации).
- **Проверено на физическом окружении**:
  - macOS Sonoma/Sequoia (Apple Silicon, нативный CoreAudio capture).
  - Локальные OpenAI-совместимые эндпоинты (Ollama/vLLM) проверены через диагностический инструмент `evals/provider_probe.py`.
- **Платформенные ограничения и дальнейшие требования**:
  - Windows WASAPI loopback и Linux ALSA/PulseAudio требуют валидации захвата на физических машинах соответствующих ОС.
  - Перед боевым использованием сторонних моделей обязателен запуск `evals/provider_probe.py` для подтверждения строгого следования JSON Schema и отсутствия GPT-специфичных зависимостей.

### Документация
- [Концепция продукта](docs/product-concept.md)
- [План реализации](docs/implementation-plan.md)
- [Аудит задержек AI и дефектов очереди](docs/ai-latency-audit.md)
- [План устранения задержек AI (Этапы 1–6)](docs/ai-latency-implementation-plan.md)
- [План: Адаптивные уточняющие и наводящие вопросы](docs/adaptive-followup-questions-plan.md)
- [Отчёт: Адаптивные уточняющие и наводящие вопросы](docs/adaptive-followup-questions-report.md)
- [План исправлений по результатам ревью](docs/remediation-plan.md)
- [Доработка рабочего пути Desktop → STT](docs/desktop-pipeline-remediation-plan.md)
- [Исправление запуска и режим одного источника](docs/startup-and-single-source-plan.md)
- [Отчёт об устранении дефектов десктопного пайплайна (Этапы A-G)](docs/desktop-pipeline-remediation-report.md)
- [Отчёт о сквозной верификации, матрице регрессий и аудите (Этап 9)](docs/stage9-verification-and-audit.md)
- [Спецификация контрактов и доменной модели](docs/contracts-spec.md)
- [Спецификация аудиозахвата и spooling](docs/audio-capture-spec.md)
- [Спецификация интеграции STT и LLM](docs/stt-llm-provider-spec.md)
- [Спецификация и план настроек AI-провайдеров](docs/ai-provider-settings-implementation-plan.md)
- [Спецификация управления сервисами](docs/service-management-spec.md)
- [Спецификация надёжности и пилотной готовности](docs/stage7-reliability-and-pilot-spec.md)

---

<a name="english"></a>
## English

Nebula is a cross-platform AI copilot for conducting technical and professional interviews.

The application supports two distinct audio capture modes:
- **Dual-Channel Mode (`dual_source`)**: captures interviewer and candidate speech across two isolated hardware/software channels with continuous clock drift and skew tracking.
- **Single-Channel Mode (`single_source`)**: captures a single hardware input (e.g., room microphone or conference mix) onto a shared track `shared` tagged as `unknown`, requiring explicit human speaker verification before sending candidate statements to AI evaluation.

Core product principle: **Nebula is a decision-support system, not an autonomous hiring judge**. AI proposals are strictly advisory, and the final decision is always made and approved by the human interviewer.

### Key Architectural Principles
1. **Vendor Independence (No GPT Lock-in)**: operates via standard `POST /v1/chat/completions` using open or local models (Qwen, Llama, DeepSeek, etc.). Zero proprietary dependencies on OpenAI GPT models.
2. **Deterministic Scoring Engine**: the LLM assesses content and criteria compliance, but question-level and final composite scores (along with coverage metrics) are computed deterministically on the backend.
3. **Programmatic Evidence Verification**: every score proposal requires verifiable verbatim quotes from the candidate transcript. Hallucinated quotes and prompt injection attempts are blocked.
4. **Speaker Attribution Isolation**: in `single_source` mode, unassigned speech (`unknown`) or interviewer speech is strictly excluded from candidate answer evaluation.
5. **Bias Protection**: any scoring justifications referencing non-professional personal attributes (accent, vocal timbre, speech rate, gender, age) strictly block approval.
6. **Independent AI Provider Configuration & Hot Reload**: independent configuration of STT and LLM providers via Desktop UI and REST API. Secrets are stored in versioned protected files outside SQLite, write-only and never exposed via API. Background worker reloads configuration on the fly without application restart, guarded by transactional OCC.

### Repository Layout
```text
crates/
  audio-capture/  # Native audio capture core (Rust, CoreAudio/WASAPI, ringbuffer, spool)
apps/
  audio-spike/    # CLI tool for audio spike & stress testing
  desktop/        # Tauri 2 + React desktop application (Stage 3+)
contracts/        # Pydantic v2 and JSON Schema data contracts (providers, audio, domain)
backend/
  core/           # Domain logic: deterministic scoring, state machine, evidence validator
  adapters/       # OpenAI-compatible LLM adapter without GPT assumptions
  api/            # FastAPI application and HTTP routes
evals/            # Provider probe CLI tool to validate non-GPT LLM endpoints
tests/            # Unit and contract test suite (pytest)
docs/             # Product concept, implementation plan, contracts and audio specs
```

### Quickstart
```bash
# 1. Install dependencies and run Python test suite (backend & contracts)
uv sync
uv run pytest -v
uv run ruff check .

# 2. Run native audio capture test suite (Rust)
cargo test --all

# 3. Inspect system audio input and output devices
cargo run -p audio-spike -- list-devices

# 4. Run 1-hour 2-channel audio stress test with clock drift simulation
cargo run --release -p audio-spike -- synthetic-test --simulated-duration-sec 3600

# 5. Run LLM provider compatibility probe
uv run python evals/provider_probe.py --base-url http://localhost:11434/v1 --model qwen2.5:7b

# 6. Build the self-contained Desktop app (FastAPI and worker are embedded in .app)
npm --prefix apps/desktop ci
npm --prefix apps/desktop run tauri -- build --no-sign
```

When the packaged application starts, it launches FastAPI and the pipeline worker automatically;
running `scripts/start.sh backend` or `scripts/start.sh worker` separately is not required.

On Windows the same command produces `target/release/bundle/nsis/*-setup.exe` and MSI;
on Linux it produces AppImage and DEB. The Python sidecar must be built on its target OS:
cross-building the same sidecar between macOS, Windows, and Linux is not supported.

### Service Management (CLI)
```bash
./scripts/start.sh                    # Dev: Backend + Worker + Desktop as separate processes
./scripts/start.sh backend            # Start API server only
./scripts/status.sh                   # Check PIDs, API health & SQLite WAL integrity
./scripts/stop.sh                     # Gracefully stop all services
./scripts/restart.sh                  # Restart system services
./scripts/nebula.sh logs worker -f    # Live stream pipeline worker logs
```

### Verification Status & Readiness Boundaries
- **Automated Verification (100% Green)**:
  - 212 Python/pytest tests (FastAPI endpoints, SQLite isolation, deterministic scoring engine, evidence validator, optimistic concurrency control, session state machine, atomic worker lease/lock handling, full 14-scenario end-to-end regression matrix, single-source mode, desktop pipeline e2e test `test_desktop_pipeline_e2e.py`, adaptive follow-up questions, and 27 AI latency elimination & dedup tests).
  - 24 Rust tests (`cargo test --workspace`), covering lock-free ringbuffer, WAV spooling, Tauri desktop IPC contracts, and synthetic 1-hour 2-channel recording with clock drift compensation (3600s simulated in 57s without sample loss).
  - Desktop frontend production build verified (`tsc && vite build` — 0 errors, full TypeScript type safety).
- **Physical Environment Verification**:
  - macOS Sonoma/Sequoia (Apple Silicon, native CoreAudio capture).
  - Local OpenAI-compatible endpoints (Ollama/vLLM) probed and validated via `evals/provider_probe.py`.
- **Platform Limitations & Operational Requirements**:
  - Windows WASAPI loopback and Linux ALSA/PulseAudio require device validation on physical machines running target operating systems.
  - Upstream LLM providers must pass `evals/provider_probe.py` before live deployment to verify strict JSON Schema compliance without proprietary OpenAI quirks.

### Documentation
- [Product Concept](docs/product-concept.md)
- [Implementation Plan](docs/implementation-plan.md)
- [AI Latency Audit and Queue Bottlenecks](docs/ai-latency-audit.md)
- [AI Latency Remediation Plan (Stages 1–6)](docs/ai-latency-implementation-plan.md)
- [Plan: Adaptive Follow-Up Questions (Russian)](docs/adaptive-followup-questions-plan.md)
- [Report: Adaptive Follow-Up Questions](docs/adaptive-followup-questions-report.md)
- [Remediation Plan (Russian)](docs/remediation-plan.md)
- [Desktop → STT Integration Remediation (Russian)](docs/desktop-pipeline-remediation-plan.md)
- [Startup and Single-Source Plan (Russian)](docs/startup-and-single-source-plan.md)
- [Desktop Pipeline Remediation Report (Stages A-G)](docs/desktop-pipeline-remediation-report.md)
- [End-to-End Verification, Regression Matrix & Audit Report (Stage 9)](docs/stage9-verification-and-audit.md)
- [Contracts & Domain Model Specification](docs/contracts-spec.md)
- [Audio Capture & Spooling Specification](docs/audio-capture-spec.md)
- [STT & LLM Integration Specification](docs/stt-llm-provider-spec.md)
- [AI Provider Settings Specification & Implementation Plan](docs/ai-provider-settings-implementation-plan.md)
- [Service Management Specification](docs/service-management-spec.md)
- [Reliability & Pilot Readiness Specification](docs/stage7-reliability-and-pilot-spec.md)
