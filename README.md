# Nebula: AI-помощник для проведения собеседований / AI Interview Copilot

[English version below](#english)

---

## Русский

Nebula — кроссплатформенный AI-помощник для проведения технических и профессиональных собеседований.

Программа слушает интервьюера и кандидата (по двум изолированным аудиоканалам), распознаёт вопросы и ответы, предлагает предварительные оценки по заранее утверждённым рубрикам и формирует подтверждённое итоговое резюме собеседования.

Ключевой продуктовый принцип: **Nebula является системой поддержки принятия решений, а не автономным судьёй**. Оценки AI носят исключительно рекомендательный характер, а итоговое решение всегда принимает и подтверждает интервьюер.

### Ключевые архитектурные принципы
1. **Независимость от OpenAI и GPT**: работа ведётся через стандартный протокол `POST /v1/chat/completions` с открытыми или локальными моделями (Qwen, Llama, DeepSeek и др.). Никаких скрытых зависимостей от проприетарных сервисов OpenAI.
2. **Детерминированный расчет баллов**: модель оценивает факты и соответствие критериям, но итоговый балл и покрытие рассчитываются детерминированной математической формулой бэкенда.
3. **Программная валидация цитат (Evidence)**: любая оценка требует верифицируемой ссылки на транскрипт. Галлюцинации, фейковые цитаты и попытки prompt injection отсекаются валидатором.
4. **Защита от дискриминации**: любые суждения по нерелевантным личным признакам (акцент, пол, тембр речи, возраст) блокируют утверждение оценки.

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
cargo test --workspace

# 3. Инспекция аудиоустройств ввода/вывода (CoreAudio / WASAPI)
cargo run -p audio-spike -- list-devices

# 4. Запуск часового стресс-теста аудиозахвата со сведением дрейфа
cargo run --release -p audio-spike -- synthetic-test --simulated-duration-sec 3600

# 5. Запуск диагностического пробника LLM-провайдера
uv run python evals/provider_probe.py --base-url http://localhost:11434/v1 --model qwen2.5:7b
```

### Управление сервисами (CLI)
```bash
./scripts/start.sh                    # Запуск всех компонентов (Backend + Worker + Desktop)
./scripts/start.sh backend            # Запуск только API сервера
./scripts/status.sh                   # Мониторинг PID, здоровья API и целостности SQLite WAL
./scripts/stop.sh                     # Корректная остановка всех сервисов
./scripts/restart.sh                  # Перезапуск системы
./scripts/nebula.sh logs worker -f    # Просмотр логов воркера в реальном времени
```

### Документация
- [Концепция продукта](docs/product-concept.md)
- [План реализации](docs/implementation-plan.md)
- [Спецификация контрактов и доменной модели](docs/contracts-spec.md)
- [Спецификация аудиозахвата и spooling](docs/audio-capture-spec.md)
- [Спецификация интеграции STT и LLM](docs/stt-llm-provider-spec.md)
- [Спецификация управления сервисами](docs/service-management-spec.md)
- [Спецификация надёжности и пилотной готовности](docs/stage7-reliability-and-pilot-spec.md)

---

<a name="english"></a>
## English

Nebula is a cross-platform AI copilot for conducting technical and professional interviews.

The application captures both the interviewer and candidate audio channels independently, transcribes speech, maps questions and answers, proposes rubric-based criterion scores with verbatim evidence, and prepares a human-confirmed final report.

Core product principle: **Nebula is a decision-support system, not an autonomous hiring judge**. AI proposals are strictly advisory, and the final decision is always made and approved by the human interviewer.

### Key Architectural Principles
1. **Vendor Independence (No GPT Lock-in)**: operates via standard `POST /v1/chat/completions` using open or local models (Qwen, Llama, DeepSeek, etc.). Zero proprietary dependencies on OpenAI GPT models.
2. **Deterministic Scoring Engine**: the LLM assesses content and criteria compliance, but question-level and final composite scores (along with coverage metrics) are computed deterministically on the backend.
3. **Programmatic Evidence Verification**: every score proposal requires verifiable verbatim quotes from the candidate transcript. Hallucinated quotes and prompt injection attempts are blocked.
4. **Bias Protection**: any scoring justifications referencing non-professional personal attributes (accent, vocal timbre, speech rate, gender, age) strictly block approval.

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
cargo test --workspace

# 3. Inspect system audio input and output devices
cargo run -p audio-spike -- list-devices

# 4. Run 1-hour 2-channel audio stress test with clock drift simulation
cargo run --release -p audio-spike -- synthetic-test --simulated-duration-sec 3600

# 5. Run LLM provider compatibility probe
uv run python evals/provider_probe.py --base-url http://localhost:11434/v1 --model qwen2.5:7b
```

### Service Management (CLI)
```bash
./scripts/start.sh                    # Start all components (Backend + Worker + Desktop)
./scripts/start.sh backend            # Start API server only
./scripts/status.sh                   # Check PIDs, API health & SQLite WAL integrity
./scripts/stop.sh                     # Gracefully stop all services
./scripts/restart.sh                  # Restart system services
./scripts/nebula.sh logs worker -f    # Live stream pipeline worker logs
```

### Documentation
- [Product Concept](docs/product-concept.md)
- [Implementation Plan](docs/implementation-plan.md)
- [Contracts & Domain Model Specification](docs/contracts-spec.md)
- [Audio Capture & Spooling Specification](docs/audio-capture-spec.md)
- [STT & LLM Integration Specification](docs/stt-llm-provider-spec.md)
- [Service Management Specification](docs/service-management-spec.md)
- [Reliability & Pilot Readiness Specification](docs/stage7-reliability-and-pilot-spec.md)
