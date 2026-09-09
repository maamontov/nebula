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
contracts/        # Pydantic v2 и JSON Schema контракты (провайдеры, аудио, домен)
backend/
  core/           # Доменная логика: детерминированный скоринг, state machine, evidence validator
  adapters/       # OpenAI-compatible LLM адаптер без завязки на GPT
  api/            # FastAPI приложение и эндпоинты
evals/            # Provider probe CLI для проверки любых non-GPT провайдеров
apps/desktop/     # Tauri 2 + React десктопное приложение (Этап 1-3)
tests/            # Набор модульных и интеграционных тестов (pytest)
docs/             # Концепция, план реализации, спецификация контрактов
```

### Быстрый старт
```bash
# Установка зависимостей через uv
uv sync

# Запуск тестов
uv run pytest -v

# Проверка качества кода (ruff)
uv run ruff check .

# Запуск диагностического пробника LLM-провайдера
uv run python evals/provider_probe.py --base-url http://localhost:11434/v1 --model qwen2.5:7b
```

### Документация
- [Концепция продукта](docs/product-concept.md)
- [План реализации](docs/implementation-plan.md)
- [Спецификация контрактов и доменной модели](docs/contracts-spec.md)

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
contracts/        # Pydantic v2 and JSON Schema data contracts (providers, audio, domain)
backend/
  core/           # Domain logic: deterministic scoring, state machine, evidence validator
  adapters/       # OpenAI-compatible LLM adapter without GPT assumptions
  api/            # FastAPI application and HTTP routes
evals/            # Provider probe CLI tool to validate non-GPT LLM endpoints
apps/desktop/     # Tauri 2 + React desktop shell (Stages 1-3)
tests/            # Unit and contract test suite (pytest)
docs/             # Product concept, implementation plan, contracts spec
```

### Quickstart
```bash
# Install dependencies using uv
uv sync

# Run test suite
uv run pytest -v

# Run linter
uv run ruff check .

# Run provider compatibility probe
uv run python evals/provider_probe.py --base-url http://localhost:11434/v1 --model qwen2.5:7b
```

### Documentation
- [Product Concept](docs/product-concept.md)
- [Implementation Plan](docs/implementation-plan.md)
- [Contracts & Domain Model Specification](docs/contracts-spec.md)
