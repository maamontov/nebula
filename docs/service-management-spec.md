# Спецификация: Управление сервисами Nebula
# Specification: Nebula Service Management CLI

---

## 1. Обзор / Overview

### Русский
Для удобства локальной разработки, тестирования и промышленной эксплуатации Nebula предоставляет набор легковесных bash-скриптов в каталоге `scripts/`. Скрипты обеспечивают управление жизненным циклом компонентов (`Backend API`, `Pipeline Worker`, `Desktop App`), ведение логов, мониторинг состояния и проверку здоровья сервисов без необходимости вручную открывать множество терминалов.

### English
For convenient local development, testing, and production operations, Nebula provides a suite of lightweight bash scripts in the `scripts/` directory. These scripts manage the lifecycle of all system components (`Backend API`, `Pipeline Worker`, `Desktop App`), handle process logging, track PID states, and verify service health without requiring multiple manual terminal sessions.

---

## 2. Архитектура процессов и каталоги / Process Architecture & Directories

- **Корневой каталог PID**: `.run/`
  - `backend.pid`: PID процесса FastAPI Uvicorn.
  - `worker.pid`: PID процесса фонового воркера (`PipelineWorker`).
  - `desktop.pid`: PID процесса десктопной оболочки Tauri 2.
- **Каталог логов**: `.run/logs/`
  - `backend.log`: Журнал обращений и событий API сервера.
  - `worker.log`: Журнал обработки очереди заданий (STT Whisper + LLM Gemini/Qwen).
  - `desktop.log`: Журнал компиляции и консоли десктопного приложения.

---

## 3. Команды управления / CLI Commands

### 3.1. Единый CLI: `scripts/nebula.sh`
```bash
./scripts/nebula.sh [command] [target] [options]
```

#### Доступные команды (`command`):
- `start [target]`: Запускает сервисы в фоне с проверкой готовности.
  - `all` (по умолчанию): запускает Backend $\to$ Worker $\to$ Desktop.
  - `backend`: только FastAPI сервер на `http://127.0.0.1:8000`.
  - `worker`: только фоновый воркер обработки очереди заданий.
  - `desktop`: только десктопное окно Tauri 2.
- `stop [target]`: Корректно останавливает сервисы (`SIGTERM`, при зависании `SIGKILL`).
- `restart [target]`: Перезапускает указанный сервис или все сервисы.
- `status`: Выводит текущий статус, PID процессов, доступность эндпойнтов и целостность SQLite WAL (`PRAGMA integrity_check`).
- `logs [backend|worker|desktop] [-f]`: Отображает журнал сервиса (с поддержкой live-стриминга `-f`).

### 3.2. Быстрые скрипты-обертки / Convenience Wrappers

Для максимального удобства созданы короткие исполняемые скрипты:
- **`./scripts/start.sh [target]`**: аналог `./scripts/nebula.sh start [target]`
- **`./scripts/stop.sh [target]`**: аналог `./scripts/nebula.sh stop [target]`
- **`./scripts/restart.sh [target]`**: аналог `./scripts/nebula.sh restart [target]`
- **`./scripts/status.sh`**: аналог `./scripts/nebula.sh status`

---

## 4. Примеры использования / Usage Examples

### Запуск полного стека для работы:
```bash
./scripts/start.sh
```

### Проверка состояния сервисов:
```bash
./scripts/status.sh
```

### Просмотр логов воркера в реальном времени:
```bash
./scripts/nebula.sh logs worker -f
```

### Перезапуск только бэкенда:
```bash
./scripts/restart.sh backend
```

### Полная остановка всех процессов:
```bash
./scripts/stop.sh
```
