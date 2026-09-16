# План реализации настроек AI-провайдеров и моделей

Статус: готово к реализации другим агентом. Все пункты ниже пока не выполнены.

## 1. Цель

Добавить в Desktop отдельный раздел «Настройки», в котором пользователь может независимо
настроить:

1. модель транскрибации (STT);
2. модель анализа текста (LLM), используемую для оценки ответов, follow-up-подсказок и итогового
   summary.

Настройки должны реально управлять backend worker, а не быть только локальным состоянием UI.
Изменение конфигурации не должно раскрывать API-ключи, смешивать провайдеров, подменять неизвестные
модели встроенными значениями или менять параметры уже выполняющегося задания.

## 2. Границы первой версии

В первую версию входят:

- один глобально активный профиль STT;
- один глобально активный профиль LLM;
- до двух необязательных fallback-моделей LLM на том же провайдере и с теми же credentials;
- встроенные presets RouterAI и PlusVibe;
- режим Custom с ручным вводом URL и точного upstream model ID;
- Bearer-аутентификация и режим без ключа для локальных OpenAI-compatible серверов;
- сохранение не секретной конфигурации в SQLite;
- write-only сохранение отдельных ключей STT и LLM вне SQLite;
- применение сохранённых настроек worker без перезапуска;
- явная, запускаемая пользователем проверка подключения;
- обратная совместимость с текущими `.env`-переменными.

В первую версию не входят:

- отдельная LLM-модель для каждого типа задачи;
- несколько именованных пользовательских профилей и переключение между ними;
- получение каталога моделей через `/models`;
- streaming STT;
- произвольные HTTP headers или arbitrary JSON request parameters;
- разные провайдеры для primary и fallback LLM;
- синхронизация настроек между компьютерами;
- сохранение API-ключей в SQLite, `localStorage` или frontend state дольше текущего ввода.

Эти ограничения намеренные. OpenAI-compatible провайдеры не гарантируют `/models`, одинаковый
reasoning API, одинаковую JSON Schema поддержку или одинаковые параметры запроса.

## 3. Текущее состояние и проблемы

### 3.1. Конфигурация

Сейчас `backend/core/profiles.py` собирает профили из environment:

- `NEBULA_LLM_PROVIDER` одновременно влияет на LLM и STT;
- `NEBULA_LLM_BASE_URL` и `NEBULA_LLM_MODEL` относятся к анализу;
- `NEBULA_STT_ENDPOINT` и `NEBULA_STT_MODEL` частично настраивают STT;
- отдельного STT provider ID и отдельного STT credential slot нет;
- `NEBULA_STT_LANGUAGE` объявлен в `.env.example`, но pipeline использует язык из job payload
  либо литерал `ru`.

`get_default_llm_model()` выбирает профиль эвристически по подстроке model ID. Неизвестное имя
модели молча превращается в RouterAI Qwen Flash. Для пользовательского поля «Модель» это
недопустимо: точный upstream ID должен передаваться без подмены.

### 3.2. Worker

`PipelineWorker.__init__()` один раз создаёт provider, STT adapter, resilient LLM adapter и
follow-up adapter. После сохранения настроек уже запущенный worker продолжит использовать старые
объекты.

Worker передаёт `self.provider.api_key_env` в STT adapter, то есть STT фактически получает ключ
LLM-провайдера. Один `_provider_semaphore` также используется для обоих типов запросов, хотя после
разделения они могут идти к разным провайдерам.

### 3.3. Credentials

Оба адаптера при отсутствии своего ключа перебирают `ROUTERAI_API_KEY`, `PLUSVIBE_API_KEY`,
`OPENAI_API_KEY` и `NEBULA_API_KEY`. После разделения STT и LLM такой fallback может незаметно
отправить credential не тому upstream.

Ключи нельзя возвращать через API даже в маскированном виде: последние символы тоже являются
частью секрета. Ответ API должен содержать только `api_key_configured: true/false` и источник
credentials (`runtime_file`, `process_environment`, `none`).

### 3.4. Reasoning и structured output

`ModelProfile.thinking_disable_payload` правильно моделирует текущую реальность: Qwen использует
`{"enable_thinking": false}`, DeepSeek — `{"reasoning_effort": "none"}`, а неизвестный параметр
может быть проигнорирован или ухудшить поведение. Обычный checkbox «Reasoning on/off» эту разницу
скрывает и поэтому не подходит.

Fallback-модели сейчас скрыто захардкожены в `backend/adapters/resilient_llm.py`. Если primary
модель настраивается через UI, fallback также должен быть видим и иметь собственные capability
параметры.

### 3.5. Desktop и API

В `apps/desktop/src/App.tsx` нет screen `settings`, а в `Header.tsx` — соответствующего пункта
sidebar. Текущий `GET /api/v1/system/models` повторно вычисляет environment profiles и сообщает
один provider для STT и LLM. После разделения это будет неверно.

## 4. Зафиксированные продуктовые решения

### 4.1. Область действия

Настройки глобальные для локальной установки Nebula, не для отдельного интервью.

Один LLM-профиль обслуживает:

- `EVALUATE_QUESTION`;
- `GENERATE_FOLLOWUPS`;
- `GENERATE_SUMMARY`;
- другие существующие текстовые AI-вызовы, которые сейчас используют default LLM profile.

Не добавлять per-task routing UI в этой задаче.

### 4.2. Когда изменения вступают в силу

Новая revision вступает в силу для следующего задания, которое worker собирается claim/execute.
Однако сохранение настроек должно быть запрещено, если существует хотя бы одно из условий:

- интервью в статусе `recording` или `paused`;
- job в статусе `PENDING` или `PROCESSING`.

Backend проверяет это в той же SQLite-транзакции, в которой обновляет настройки. UI-блокировка
служит только удобством; backend остаётся авторитетным и возвращает 409.

Такое ограничение исключает смешивание разных STT/LLM-конфигураций в одном незавершённом
pipeline без введения истории конфигураций и `settings_revision` в каждом job.

### 4.3. Preset и Custom

Preset заполняет поля, но сохранённый объект остаётся обычной явной конфигурацией. Runtime не
должен снова угадывать параметры по имени provider или model ID.

Предусмотреть presets:

- RouterAI с текущими проверенными значениями;
- PlusVibe с текущими значениями;
- Custom.

Выбор Custom не должен менять введённый model ID или автоматически подключать built-in fallbacks.

### 4.4. Reasoning policy

В контракте использовать enum:

- `provider_default` — не отправлять дополнительный параметр;
- `disable_enable_thinking` — добавить `enable_thinking=false`;
- `disable_reasoning_effort` — добавить `reasoning_effort=none`.

Не добавлять режим «принудительно включить»: универсального OpenAI-compatible параметра для этого
нет. Не принимать произвольный JSON из UI.

Каждая LLM-модель, включая fallback, имеет собственный `reasoning_policy`. Built-in preset
выставляет только проверенные значения. Custom по умолчанию использует `provider_default`.

### 4.5. Secrets

Не секретная конфигурация хранится в SQLite. API-ключи — нет.

Использовать два фиксированных credential slot:

- `NEBULA_STT_API_KEY`;
- `NEBULA_LLM_API_KEY`.

UI не должен позволять вводить произвольное имя env-переменной: иначе локальная конфигурация
может заставить backend прочитать другой секрет и отправить его на произвольный URL.

Для UI-managed ключей использовать versioned dotenv snapshots в каноническом
`NEBULA_DATA_DIR`, например `provider-secrets-v3.env`. Номер файла равен revision строки
`ai_settings`. Каталог `data/` уже исключён из Git. Snapshot сначала полностью записывается через
временный файл и atomic replace и только после этого новая DB revision делает его активным. На
Unix выставить `0600`. На Windows файл должен создаваться в user-owned app data directory, без
расширения ACL на других пользователей.

Приоритет источников:

1. явно заданный process environment;
2. UI-managed `provider-secrets-v<revision>.env`, на который указывает активная DB revision;
3. legacy provider-specific variable (`ROUTERAI_API_KEY` или `PLUSVIBE_API_KEY`) только для
   соответствующего built-in preset;
4. отсутствие ключа.

Если ключ пришёл из process environment, UI показывает «Управляется окружением» и не пытается
перезаписать process environment. Пользователь может изменить не секретные поля, но replace/clear
ключа через UI должен быть отклонён понятной ошибкой.

Удалить общий перебор несвязанных fallback env names из обоих adapters.

## 5. Целевые контракты

Контракты разместить в `contracts/provider.py` либо в новом `contracts/settings.py`, если
`provider.py` становится слишком большим. Не дублировать Pydantic-модели в API module.

### 5.1. Enum

```python
class ProviderPreset(str, Enum):
    ROUTERAI = "routerai"
    PLUSVIBE = "plusvibe"
    CUSTOM = "custom"

class AuthMode(str, Enum):
    BEARER = "bearer"
    NONE = "none"

class ReasoningPolicy(str, Enum):
    PROVIDER_DEFAULT = "provider_default"
    DISABLE_ENABLE_THINKING = "disable_enable_thinking"
    DISABLE_REASONING_EFFORT = "disable_reasoning_effort"

class ApiKeyAction(str, Enum):
    PRESERVE = "preserve"
    REPLACE = "replace"
    CLEAR = "clear"
```

### 5.2. STT

```python
class TranscriptionSettings(BaseModel):
    preset: ProviderPreset
    provider_id: str
    provider_name: str
    endpoint_url: str
    model_id: str
    auth_mode: AuthMode = AuthMode.BEARER
    language: str = "ru"
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    max_concurrency: int = Field(default=2, ge=1, le=20)
```

В первой версии protocol всегда `batch`; не показывать неработающий streaming selector.

### 5.3. LLM model и provider

```python
class AnalysisModelSettings(BaseModel):
    model_id: str
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.JSON_OBJECT
    reasoning_policy: ReasoningPolicy = ReasoningPolicy.PROVIDER_DEFAULT
    supports_temperature: bool = True
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=4096, ge=256, le=65536)
    context_window_tokens: int = Field(default=32768, ge=2048)

class TextAnalysisSettings(BaseModel):
    preset: ProviderPreset
    provider_id: str
    provider_name: str
    base_url: str
    auth_mode: AuthMode = AuthMode.BEARER
    timeout_seconds: float = Field(default=60.0, ge=1.0, le=300.0)
    max_concurrency: int = Field(default=10, ge=1, le=100)
    primary_model: AnalysisModelSettings
    fallback_models: list[AnalysisModelSettings] = Field(default_factory=list, max_length=2)
```

`model_id` — точный upstream ID. Не преобразовывать регистр и не выбирать профиль по substring.

### 5.4. Read/write API models

```python
class ApiKeyUpdate(BaseModel):
    action: ApiKeyAction = ApiKeyAction.PRESERVE
    value: SecretStr | None = None

class UpdateAiSettingsRequest(BaseModel):
    expected_revision: int
    transcription: TranscriptionSettings
    text_analysis: TextAnalysisSettings
    stt_api_key: ApiKeyUpdate = Field(default_factory=ApiKeyUpdate)
    llm_api_key: ApiKeyUpdate = Field(default_factory=ApiKeyUpdate)

class CredentialStatus(BaseModel):
    configured: bool
    source: Literal["process_environment", "runtime_file", "legacy_environment", "none"]
    editable: bool

class AiSettingsResponse(BaseModel):
    revision: int
    source: Literal["environment", "database"]
    transcription: TranscriptionSettings
    text_analysis: TextAnalysisSettings
    stt_credentials: CredentialStatus
    llm_credentials: CredentialStatus
    can_update: bool
    update_blocker: str | None
```

Правила `ApiKeyUpdate`:

- `preserve`: `value` обязан быть `None`;
- `replace`: требуется непустой `value` после trim;
- `clear`: `value` обязан быть `None`;
- при `auth_mode=none` сохранённый ключ можно оставить, но он не отправляется;
- `SecretStr` не должен сериализоваться в логи или error detail.

## 6. Хранение и миграция

### 6.1. Bootstrap schema

Добавить в `backend/db/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS ai_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    transcription_config_json TEXT NOT NULL,
    text_analysis_config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Не хранить здесь API keys, их hashes, маски или последние символы.

### 6.2. Migration 13

В `backend/db/migrations.py`:

- увеличить `TARGET_VERSION` с 12 до 13;
- создать таблицу `ai_settings`, если её нет;
- не вставлять environment values в таблицу во время migration;
- записать migration `013_ai_settings`;
- выполнить существующий integrity check.

Если строки `id=1` нет, resolver возвращает effective environment configuration с `revision=0`
и `source=environment`. Первый успешный PUT создаёт строку с revision 1.

### 6.3. Repository

Добавить методы:

```python
get_ai_settings() -> dict[str, Any] | None

update_ai_settings(
    expected_revision: int,
    transcription_config: dict[str, Any],
    text_analysis_config: dict[str, Any],
) -> dict[str, Any]

get_ai_settings_update_blocker() -> str | None
```

`update_ai_settings()` в одной transaction должен:

1. проверить отсутствие `recording`/`paused` interviews;
2. проверить отсутствие `PENDING`/`PROCESSING` jobs;
3. прочитать текущую revision (`0`, если строки нет);
4. сравнить её с `expected_revision`;
5. вставить либо обновить singleton row и увеличить revision ровно на 1.

При нарушении условий бросать `RepositoryConflictError`, который API преобразует в 409.

Проверка blockers и запись должны быть в одной transaction. Отдельная предварительная проверка
из endpoint оставляет гонку.

## 7. Secret store

Создать небольшой backend-модуль, например `backend/core/credential_store.py`.

Требования:

- путь выводится из `NEBULA_DATA_DIR`, а не из cwd;
- поддерживаются только два фиксированных key name;
- чтение не логирует значение;
- каждый successful settings update создаёт полный snapshot обоих runtime credentials для новой
  revision; `preserve` копирует прежнее effective UI-managed значение, но не раскрывает его API;
- replace и clear попадают только в новый versioned snapshot;
- ошибка записи не должна оставить частично записанный файл;
- ответ/exception не должен содержать ключ;
- значения с переводами строк отклоняются;
- пустые значения не считаются настроенным ключом;
- process environment имеет приоритет и помечается `editable=false`.

SQLite и filesystem нельзя обновить одной транзакцией. Использовать DB revision как commit pointer
на заранее подготовленный versioned snapshot:

1. прочитать current revision и effective credentials, не возвращая их наружу;
2. полностью провалидировать request;
3. собрать полный snapshot для `expected_revision + 1` во временном файле;
4. atomically rename его в `provider-secrets-v<next_revision>.env`; пока DB не обновлена, этот файл
   никем не используется;
5. выполнить `update_ai_settings()` с повторной проверкой blockers и OCC в одной DB transaction;
6. если transaction не прошла, удалить неактивный новый snapshot; старая DB revision продолжает
   ссылаться на старый файл;
7. после успешного commit можно удалить snapshots старше предыдущей revision. Текущий и предыдущий
   оставить для безопасного восстановления/диагностики.

Если job был enqueue между шагами 4 и 5, transactional blocker обнаружит `PENDING` job и обновление
не состоится. Worker по-прежнему читает старую DB revision и старый snapshot. Если worker увидел
новую DB revision, соответствующий secrets file уже полностью существует. Это устраняет окно, в
котором новый config мог бы использовать старый key или наоборот.

Тестами покрыть ошибку подготовки/rename, OCC conflict после подготовки файла, появление job между
подготовкой и commit, DB failure и очистку orphan snapshot. Не оставлять согласованность как
best-effort.

Нельзя возвращать содержимое secrets file через `/system/config`, backup API или diagnostics.

## 8. Resolver и runtime profiles

Создать `backend/core/ai_settings.py` с единой ответственностью:

- получить effective settings;
- применить environment defaults при отсутствии DB row;
- определить credential status;
- преобразовать сохранённые settings в runtime profiles/adapters.

Предлагаемые структуры:

```python
@dataclass(frozen=True)
class ResolvedCredentials:
    api_key: str | None
    source: str

@dataclass(frozen=True)
class RuntimeAiConfiguration:
    revision: int
    stt_provider: ProviderProfile
    stt_profile: STTProfile
    stt_credentials: ResolvedCredentials
    llm_provider: ProviderProfile
    llm_primary: ModelProfile
    llm_fallbacks: tuple[ModelProfile, ...]
    llm_credentials: ResolvedCredentials
    language: str
```

### 8.1. Profile mapping

`ReasoningPolicy` преобразуется в payload только в resolver:

```python
provider_default -> None
disable_enable_thinking -> {"enable_thinking": False}
disable_reasoning_effort -> {"reasoning_effort": "none"}
```

`ModelProfile.id` должен быть стабильным и различать provider/model/configuration. Допустимо
строить его как безопасный deterministic hash от provider ID, exact model ID, structured mode и
reasoning policy. Не использовать только строку `active-model`, иначе historical provenance станет
неоднозначным.

В результаты по-прежнему записывать фактически использованный upstream model ID и provider ID.

### 8.2. URL validation

На уровне Pydantic/resolver проверить:

- scheme только `http` или `https`;
- URL не содержит username/password;
- URL не содержит fragment;
- LLM `base_url` не заканчивается на `/chat/completions`;
- STT использует полный endpoint URL;
- Bearer key запрещено отправлять по plain HTTP, кроме loopback (`localhost`, `127.0.0.1`, `::1`);
- redirects остаются выключенными;
- в error message не включать Authorization header.

Не запрещать `auth_mode=none` для HTTP на LAN: это нужно локальным Ollama/vLLM deployments.

## 9. Изменения adapters

### 9.1. LLM adapter

Изменить `OpenAICompatibleAdapter`:

- принимать resolved credential или credential provider явно;
- не искать произвольные fallback env variables;
- при `auth_mode=none` не добавлять Authorization;
- при `auth_mode=bearer` без ключа бросать `LLMAuthenticationError` до HTTP-вызова;
- оставить фильтрацию reserved keys;
- не логировать request headers или полный request object.

Не изменять правила structured output и очистки `<think>` за пределами необходимого mapping.

### 9.2. STT adapter

Аналогично:

- explicit credential;
- `auth_mode=none`;
- никакого перебора LLM/provider keys;
- exact `profile.endpoint_url` и `profile.model_id`.

Язык по умолчанию должен приходить из active transcription settings, но job payload с явно
зафиксированным language остаётся приоритетным для уже сформированного задания.

### 9.3. Resilient LLM

`ResilientLLMAdapter` должен принимать список `fallback_models`, а не самостоятельно выбирать их
по `provider.id`.

Поведение:

- пустой список — только primary;
- 1–2 элемента — последовательный fallback;
- authentication errors не запускают fallback на том же provider;
- `actual_model_id` и fallback metadata сохраняются как сейчас;
- reasoning/structured output берутся из конкретного model profile, а не из primary.

## 10. Перезагрузка worker

Добавить immutable `WorkerAiBundle`, содержащий:

- settings revision;
- STT provider/profile/adapter/semaphore;
- LLM provider/resilient adapter/semaphore;
- follow-up adapter;
- default language.

Worker хранит current bundle и `asyncio.Lock` для refresh.

Алгоритм перед выполнением каждого job:

1. claim job;
2. под refresh lock повторно прочитать settings revision;
3. если revision изменилась — собрать новый bundle;
4. захватить локальную ссылку на immutable bundle;
5. передать bundle или нужный adapter в handler;
6. не читать mutable `self.stt_adapter`/`self.llm_adapter` посреди handler.

Повторная проверка после claim обязательна. Возможна гонка: consumer прочитал старую revision,
настройки сохранились, затем consumer claim’нул первый новый job. Проверка только до claim в этом
случае использует старый профиль.

PUT блокируется при `PROCESSING` jobs, поэтому конфигурация не может смениться у уже выполняемого
задания. `PENDING` тоже блокирует сохранение по решению раздела 4.2.

Разделить semaphores:

- STT concurrency берётся из transcription settings;
- LLM concurrency берётся из text analysis settings;
- существующее резервирование assessment/follow-up/background пересчитать только внутри LLM cap;
- live STT больше не занимает LLM permit.

Для unit tests, которые явно передают mock adapters в `PipelineWorker`, сохранить dependency
injection. В таком режиме automatic settings reload не должен неожиданно заменять mocks; добавить
явный флаг либо считать переданные adapters fixed.

## 11. Backend API

Добавить endpoints в `backend/api/app.py`.

### 11.1. GET `/api/v1/settings/ai`

Возвращает `AiSettingsResponse` с effective values. Никогда не возвращает key value.

Если DB row отсутствует:

- `revision=0`;
- `source=environment`;
- поля заполнены текущими effective defaults;
- credential status отражает реальный источник.

### 11.2. PUT `/api/v1/settings/ai`

Принимает `UpdateAiSettingsRequest`.

Ответы:

- 200 — сохранённый `AiSettingsResponse` с новой revision;
- 409 — stale `expected_revision` либо активное интервью/job;
- 422 — некорректный URL, model ID, reasoning policy или key action;
- 500 — безопасная ошибка persistence без секрета в detail.

После успешного ответа GET обязан возвращать тот же non-secret config.

### 11.3. POST `/api/v1/settings/ai/test`

Проверяет candidate config без сохранения. Request содержит target `stt`/`llm`, соответствующую
секцию настроек и optional write-only test key.

LLM probe:

- минимальный prompt;
- минимальная JSON schema;
- проверка выбранного structured output mode;
- короткий timeout;
- вернуть latency, provider ID, фактический model ID и result status;
- не возвращать raw model response или reasoning trace.

STT probe:

- программно сгенерированный короткий валидный WAV без пользовательской речи;
- цель — проверить endpoint/auth/model acceptance, а не качество распознавания;
- пустой transcript допустим;
- явно указать в ответе, что quality/hardware не проверены.

Probe запускается только по кнопке пользователя, так как расходует квоту. Не вызывать его при GET,
PUT, запуске приложения или тестовом suite без mock transport.

### 11.4. `/system/models`

Перевести endpoint на resolver и вернуть отдельно:

- фактический STT provider/model;
- primary LLM provider/model;
- configured fallback model IDs;
- settings revision.

Сохранить текущие поля, которые использует `LiveSessionScreen`, либо синхронно обновить TypeScript
types/client. Не оставлять endpoint вычислять старые environment factories независимо от worker.

## 12. Desktop

### 12.1. Навигация

Изменить:

- `apps/desktop/src/App.tsx` — добавить `settings` в `Screen`, импорт и render;
- `apps/desktop/src/components/Header.tsx` — добавить пункт «Настройки» с `Settings` icon;
- расширить `currentScreen` и `onNavigate` types.

Settings не является interview screen и не должен показывать candidate/status context. Баннер
возврата к активной записи остаётся видимым.

### 12.2. API client/types

В `apps/desktop/src/services/api.ts` добавить:

- TypeScript types, соответствующие Pydantic contracts;
- `getAiSettings()`;
- `updateAiSettings()`;
- `testAiSettings()`;
- нормализованное извлечение FastAPI `detail` для 409/422.

Не хранить API key в общем types/store и не логировать request body.

### 12.3. Settings screen

Создать `apps/desktop/src/screens/SettingsScreen.tsx`.

Структура:

1. заголовок и описание области действия;
2. карточка «Транскрибация»;
3. карточка «Анализ текста»;
4. сворачиваемый блок «Дополнительные параметры»;
5. нижняя панель Save/Reset и статус несохранённых изменений.

Поля STT:

- preset;
- provider name/ID;
- endpoint URL;
- model ID;
- auth mode;
- API key control;
- language;
- timeout;
- concurrency в advanced block.

Поля LLM:

- preset;
- provider name/ID;
- base URL;
- auth mode;
- API key control;
- primary model ID;
- structured output mode;
- reasoning policy;
- temperature;
- max output tokens;
- timeout/concurrency в advanced block;
- enable/disable fallback и до двух model cards с их capability fields.

### 12.4. API key UX

- При загрузке password input пуст.
- Показать только статус «Ключ настроен», «Ключ отсутствует» или «Управляется окружением».
- Кнопка «Заменить» включает новый password input.
- Кнопка «Удалить» требует локального подтверждения и отправляет explicit `clear`.
- Cancel возвращает action к `preserve` и очищает введённую строку.
- После успешного save немедленно очистить значение из component state.
- Не сохранять ключ в `localStorage`, URL, toast text или console.

### 12.5. Состояния и ошибки

- Loading skeleton при первом GET.
- Retry при недоступном backend.
- Save disabled при отсутствии изменений, invalid form или `can_update=false`.
- На 409 перезагрузить current settings и показать конкретный blocker.
- На 422 показать field-level validation error.
- Test connection имеет отдельные loading/result states для STT и LLM.
- Успешный probe не сохраняет настройки автоматически.
- Смена preset при dirty form требует подтверждения, потому что перезаписывает URL/model/capability
  поля.

Стили добавить в существующий `apps/desktop/src/index.css`; не вводить новый UI framework.

## 13. Обратная совместимость

До первого PUT поведение должно совпадать с текущим environment configuration.

Поддержать чтение:

- `NEBULA_LLM_PROVIDER`;
- `NEBULA_LLM_BASE_URL`;
- `NEBULA_LLM_MODEL`;
- `NEBULA_STT_ENDPOINT`;
- `NEBULA_STT_MODEL`;
- `NEBULA_STT_LANGUAGE`;
- `ROUTERAI_API_KEY`;
- `PLUSVIBE_API_KEY`.

Исправить при этом текущие ошибки:

- STT provider вычисляется отдельно от LLM provider;
- unknown LLM model ID не заменяется встроенной моделью;
- `NEBULA_STT_LANGUAGE` действительно становится default language;
- STT не получает LLM key через общий fallback chain.

После появления DB row не смешивать отдельные non-secret поля из DB и `.env`: вся не секретная
конфигурация берётся из DB как единый snapshot. Environment остаётся источником credentials и
fallback только при отсутствии row.

Обновить `.env.example`: добавить новые credential slots, но оставить legacy variables с пометкой
compatibility.

## 14. Тестовый план

### 14.1. Contracts

Добавить tests для:

- enum values;
- bounds timeout/concurrency/tokens/temperature;
- максимум двух fallbacks;
- invalid key actions;
- URL с credentials/fragment;
- Bearer через небезопасный remote HTTP;
- exact preservation model ID и регистра.

### 14.2. Database и repository

Проверить:

- bootstrap schema содержит `ai_settings`;
- migration 12 → 13 сохраняет все существующие данные;
- повторный запуск migration безопасен;
- first write с expected revision 0 создаёт revision 1;
- последующие записи инкрементируют revision;
- stale revision даёт conflict;
- запись блокируется для recording/paused interview;
- запись блокируется для PENDING/PROCESSING job;
- COMPLETED/FAILED jobs не блокируют;
- integrity check проходит.

### 14.3. Credential store

Использовать только temporary directories. Проверить:

- отдельные STT/LLM keys;
- preserve/replace/clear;
- process environment precedence;
- environment-managed key нельзя изменить через UI path;
- newline injection отклоняется;
- atomic replace сохраняет старый файл при ошибке;
- file mode на Unix;
- ни один response/log/exception не содержит secret fixture value.

### 14.4. Resolver/adapters

Проверить:

- STT и LLM могут иметь разные provider, URL и keys;
- `auth_mode=none` не отправляет Authorization;
- Bearer без ключа падает до HTTP call;
- custom model ID передаётся exact;
- каждый reasoning policy создаёт только свой параметр;
- reserved request keys нельзя переопределить;
- каждый fallback использует собственные model capabilities;
- auth error не запускает fallback;
- `/system/models` соответствует resolver.

### 14.5. Worker

Проверить:

- revision 0 использует environment defaults;
- новая revision подхватывается после save без restart;
- refresh после claim закрывает описанную race;
- in-flight handler использует immutable bundle;
- STT и LLM semaphores независимы;
- explicit mock adapters в старых tests не заменяются resolver;
- telemetry/audit записывает фактические STT и LLM provider/model;
- retry/lease/idempotency существующих jobs не меняются.

### 14.6. API

Проверить:

- GET/PUT round trip;
- GET revision 0 до первой записи;
- 409 OCC;
- 409 active work blocker;
- 422 validation;
- API key отсутствует во всех JSON responses;
- API key отсутствует в captured logs;
- test endpoint использует mock HTTP transport и не выходит в сеть;
- upstream errors санитизированы.

### 14.7. Desktop

Добавить Vitest/Testing Library tests:

- пункт sidebar открывает Settings;
- загрузка двух независимых секций;
- preset заполняет поля;
- custom model ID не меняется;
- key input остаётся пустым при GET;
- preserve/replace/clear формируют правильный request;
- ключ очищается после save;
- active-work blocker отключает Save;
- 409/422 отображаются пользователю;
- probe не вызывает save;
- Settings не уничтожает persistent LiveSession component.

## 15. Порядок реализации

- [x] Шаг 1. Добавить Pydantic contracts и unit tests без изменения runtime.
- [x] Шаг 2. Добавить `ai_settings` в bootstrap schema и migration 13.
- [x] Шаг 3. Реализовать repository methods, OCC и blockers с tests.
- [x] Шаг 4. Реализовать credential store и failure-path tests.
- [x] Шаг 5. Реализовать effective settings resolver и environment compatibility tests.
- [x] Шаг 6. Перевести adapters и `ResilientLLMAdapter` на explicit config/credentials.
- [x] Шаг 7. Ввести immutable worker bundle, refresh и отдельные semaphores.
- [x] Шаг 8. Добавить GET/PUT/test API и обновить `/system/models`.
- [x] Шаг 9. Добавить TypeScript contracts/client и Settings screen.
- [x] Шаг 10. Добавить navigation/CSS/frontend tests.
- [x] Шаг 11. Обновить `.env.example`, provider spec и README при необходимости.
- [x] Шаг 12. Выполнить узкие проверки, затем полный regression suite.

После каждого шага обновлять checkbox сразу, до перехода к следующему.

## 16. Проверки

Сначала запускать узкие тесты затронутого слоя. После изменений schema/repository/worker/API
обязателен полный Python suite.

```bash
uv run ruff check .
uv run pytest -q tests/test_<new_settings_contracts>.py
uv run pytest -q tests/test_<new_settings_repository>.py
uv run pytest -q tests/test_<new_settings_api>.py
uv run pytest -q tests/test_routerai_provider.py tests/test_llm_adapter.py tests/test_stt_adapter.py
uv run pytest -q tests/test_pipeline_worker.py
uv run pytest -q

cargo fmt --all -- --check
cargo test --workspace

npm --prefix apps/desktop run test
npm --prefix apps/desktop run build

git diff --check
git status --short
```

Не запускать реальный provider probe автоматически. После автоматических tests допускается одна
ручная проверка каждой секции только по явному решению пользователя, с тестовыми данными и без
вывода ключа.

## 17. Критерии приёмки

Реализация считается завершённой, когда одновременно выполнено следующее:

1. В sidebar есть рабочий пункт «Настройки».
2. STT и LLM можно настроить на разные provider, URL, model и key.
3. Custom model ID передаётся upstream без подмены.
4. Ключ никогда не возвращается API, не хранится в SQLite/localStorage и не попадает в логи.
5. Reasoning policy преобразуется в правильный vendor-specific payload.
6. Built-in fallback поведение видно и настраивается; custom profile не получает скрытые
   fallback-модели.
7. Worker подхватывает новую revision без restart и не меняет adapter у уже выполняющегося job.
8. Сохранение блокируется при активной записи или незавершённом job.
9. `/system/models` и Live Screen показывают фактически активные provider/model.
10. Старый `.env` продолжает работать до первого сохранения через UI.
11. Миграция сохраняет существующую БД и проходит integrity check.
12. Полный regression suite и desktop build проходят.

## 18. Файлы, которые, вероятно, будут изменены

Основные:

- `contracts/provider.py` или новый `contracts/settings.py`;
- `backend/db/schema.sql`;
- `backend/db/migrations.py`;
- `backend/db/repository.py`;
- новый `backend/core/credential_store.py`;
- новый `backend/core/ai_settings.py`;
- `backend/core/profiles.py`;
- `backend/adapters/llm.py`;
- `backend/adapters/stt.py`;
- `backend/adapters/resilient_llm.py`;
- `backend/workers/pipeline.py`;
- `backend/api/app.py`;
- `apps/desktop/src/services/api.ts`;
- `apps/desktop/src/App.tsx`;
- `apps/desktop/src/components/Header.tsx`;
- новый `apps/desktop/src/screens/SettingsScreen.tsx`;
- `apps/desktop/src/index.css`;
- `.env.example`;
- `docs/stt-llm-provider-spec.md`.

Тесты добавлять отдельными файлами по слоям, не складывать всю новую матрицу в один крупный test
module. Generated Tauri schemas и build output вручную не редактировать.

## 19. Риски, которые нельзя скрывать в отчёте

- Успешный connection probe не доказывает качество STT, корректность evidence или пригодность
  модели для оценки.
- OpenAI-compatible URL не гарантирует JSON Schema, reasoning usage и одинаковые token fields.
- Настройка через UI не доказывает работу физического аудиозахвата или конкретной ОС.
- Plain HTTP с Bearer key за пределами loopback должен быть запрещён, иначе ключ передаётся без
  шифрования.
- Ручная замена reasoning policy без provider probe может увеличить latency или расход токенов.
- Ошибка внешнего провайдера не должна превращаться в нулевой score кандидата или правдоподобный
  синтетический результат.
