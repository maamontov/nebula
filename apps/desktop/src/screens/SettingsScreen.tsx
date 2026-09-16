import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  AiSettingsResponse,
  TranscriptionSettings,
  TextAnalysisSettings,
  AnalysisModelSettings,
  ProviderPreset,
  AuthMode,
  StructuredOutputMode,
  ReasoningPolicy,
  ApiKeyAction,
  TestAiSettingsResponse,
} from '../types';
import {
  getAiSettings,
  updateAiSettings,
  testAiSettings,
} from '../services/api';
import {
  Save,
  RotateCcw,
  AlertCircle,
  CheckCircle2,
  Key,
  ChevronDown,
  ChevronUp,
  Plus,
  Trash2,
  Activity,
  Layers,
  Lock,
  Sparkles,
  Clock,
} from 'lucide-react';

const PRESET_STT_CONFIGS: Record<ProviderPreset, Partial<TranscriptionSettings>> = {
  routerai: {
    preset: 'routerai',
    provider_id: 'routerai',
    provider_name: 'RouterAI (RF)',
    endpoint_url: 'https://routerai.ru/api/v1/audio/transcriptions',
    model_id: 'openai/gpt-transcribe',
    auth_mode: 'bearer',
    language: 'ru',
    timeout_seconds: 60,
    max_concurrency: 2,
  },
  plusvibe: {
    preset: 'plusvibe',
    provider_id: 'plusvibe',
    provider_name: 'PlusVibe API (RF)',
    endpoint_url: 'https://plusvibeapi.ru/v1/audio/transcriptions',
    model_id: 'whisper-large-v3-turbo',
    auth_mode: 'bearer',
    language: 'ru',
    timeout_seconds: 60,
    max_concurrency: 2,
  },
  custom: {
    preset: 'custom',
    provider_id: 'custom',
    provider_name: 'Custom STT',
    endpoint_url: 'http://127.0.0.1:8000/v1/audio/transcriptions',
    model_id: 'whisper-1',
    auth_mode: 'bearer',
    language: 'ru',
    timeout_seconds: 60,
    max_concurrency: 2,
  },
};

const PRESET_LLM_CONFIGS: Record<ProviderPreset, Partial<TextAnalysisSettings>> = {
  routerai: {
    preset: 'routerai',
    provider_id: 'routerai',
    provider_name: 'RouterAI (RF)',
    base_url: 'https://routerai.ru/api/v1',
    auth_mode: 'bearer',
    timeout_seconds: 60,
    max_concurrency: 10,
    primary_model: {
      model_id: 'qwen/qwen3.7-flash',
      structured_output_mode: 'json_object',
      reasoning_policy: 'disable_enable_thinking',
      supports_temperature: true,
      temperature: 0.0,
      max_output_tokens: 4096,
      context_window_tokens: 1000000,
    },
    fallback_models: [
      {
        model_id: 'qwen/qwen3.7-plus',
        structured_output_mode: 'json_object',
        reasoning_policy: 'disable_enable_thinking',
        supports_temperature: true,
        temperature: 0.0,
        max_output_tokens: 4096,
        context_window_tokens: 1000000,
      },
    ],
  },
  plusvibe: {
    preset: 'plusvibe',
    provider_id: 'plusvibe',
    provider_name: 'PlusVibe API (RF)',
    base_url: 'https://plusvibeapi.ru/v1',
    auth_mode: 'bearer',
    timeout_seconds: 60,
    max_concurrency: 10,
    primary_model: {
      model_id: 'deepseek/deepseek-v4-flash-0731',
      structured_output_mode: 'json_object',
      reasoning_policy: 'provider_default',
      supports_temperature: true,
      temperature: 0.0,
      max_output_tokens: 4096,
      context_window_tokens: 65536,
    },
    fallback_models: [
      {
        model_id: 'qwen/qwen3.7-plus',
        structured_output_mode: 'json_object',
        reasoning_policy: 'provider_default',
        supports_temperature: true,
        temperature: 0.0,
        max_output_tokens: 4096,
        context_window_tokens: 32768,
      },
      {
        model_id: 'google/gemini-3.8-flash',
        structured_output_mode: 'json_object',
        reasoning_policy: 'provider_default',
        supports_temperature: true,
        temperature: 0.0,
        max_output_tokens: 4096,
        context_window_tokens: 1048576,
      },
    ],
  },
  custom: {
    preset: 'custom',
    provider_id: 'custom',
    provider_name: 'Custom OpenAI-Compatible',
    base_url: 'http://127.0.0.1:8000/v1',
    auth_mode: 'bearer',
    timeout_seconds: 60,
    max_concurrency: 10,
    primary_model: {
      model_id: 'custom-model',
      structured_output_mode: 'json_object',
      reasoning_policy: 'provider_default',
      supports_temperature: true,
      temperature: 0.0,
      max_output_tokens: 4096,
      context_window_tokens: 32768,
    },
    fallback_models: [],
  },
};

export const SettingsScreen: React.FC = () => {
  const [serverSettings, setServerSettings] = useState<AiSettingsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [saveSuccessMsg, setSaveSuccessMsg] = useState<string | null>(null);

  // Form State
  const [transcription, setTranscription] = useState<TranscriptionSettings | null>(null);
  const [textAnalysis, setTextAnalysis] = useState<TextAnalysisSettings | null>(null);

  // Secret update actions
  const [sttKeyAction, setSttKeyAction] = useState<ApiKeyAction>('preserve');
  const [sttKeyValue, setSttKeyValue] = useState<string>('');
  const [llmKeyAction, setLlmKeyAction] = useState<ApiKeyAction>('preserve');
  const [llmKeyValue, setLlmKeyValue] = useState<string>('');

  // UI toggles
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [enableFallbacks, setEnableFallbacks] = useState(false);

  // Probe testing state
  const [sttTesting, setSttTesting] = useState(false);
  const [sttTestResult, setSttTestResult] = useState<TestAiSettingsResponse | null>(null);
  const [sttTestError, setSttTestError] = useState<string | null>(null);

  const [llmTesting, setLlmTesting] = useState(false);
  const [llmTestResult, setLlmTestResult] = useState<TestAiSettingsResponse | null>(null);
  const [llmTestError, setLlmTestError] = useState<string | null>(null);

  const fetchSettings = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const data = await getAiSettings();
      setServerSettings(data);
      setTranscription(data.transcription);
      setTextAnalysis(data.text_analysis);
      setEnableFallbacks(data.text_analysis.fallback_models.length > 0);
      setSttKeyAction('preserve');
      setSttKeyValue('');
      setLlmKeyAction('preserve');
      setLlmKeyValue('');
    } catch (err: any) {
      console.error('Failed to load AI settings:', err);
      setLoadError(err.message || 'Ошибка загрузки настроек AI');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSettings();
  }, [fetchSettings]);

  // Dirty detection
  const isDirty = useMemo(() => {
    if (!serverSettings || !transcription || !textAnalysis) return false;
    if (sttKeyAction !== 'preserve' || llmKeyAction !== 'preserve') return true;
    if (JSON.stringify(transcription) !== JSON.stringify(serverSettings.transcription)) return true;
    if (JSON.stringify(textAnalysis) !== JSON.stringify(serverSettings.text_analysis)) return true;
    return false;
  }, [serverSettings, transcription, textAnalysis, sttKeyAction, llmKeyAction]);

  const handleReset = () => {
    if (!serverSettings) return;
    setTranscription(serverSettings.transcription);
    setTextAnalysis(serverSettings.text_analysis);
    setEnableFallbacks(serverSettings.text_analysis.fallback_models.length > 0);
    setSttKeyAction('preserve');
    setSttKeyValue('');
    setLlmKeyAction('preserve');
    setLlmKeyValue('');
    setActionError(null);
    setSaveSuccessMsg(null);
  };

  const handlePresetChangeStt = (newPreset: ProviderPreset) => {
    if (!transcription) return;
    if (isDirty) {
      const confirmed = window.confirm(
        'Смена пресета заменит параметры провайдера STT на значения по умолчанию. Продолжить?'
      );
      if (!confirmed) return;
    }
    const defaults = PRESET_STT_CONFIGS[newPreset];
    setTranscription((prev) => (prev ? { ...prev, ...defaults } : null));
  };

  const handlePresetChangeLlm = (newPreset: ProviderPreset) => {
    if (!textAnalysis) return;
    if (isDirty) {
      const confirmed = window.confirm(
        'Смена пресета заменит параметры провайдера LLM на значения по умолчанию. Продолжить?'
      );
      if (!confirmed) return;
    }
    const defaults = PRESET_LLM_CONFIGS[newPreset];
    setTextAnalysis((prev) => {
      if (!prev) return null;
      const updated: TextAnalysisSettings = {
        ...prev,
        ...defaults,
        primary_model: {
          ...prev.primary_model,
          ...(defaults.primary_model || {}),
        },
        fallback_models: defaults.fallback_models || [],
      };
      setEnableFallbacks((defaults.fallback_models || []).length > 0);
      return updated;
    });
  };

  const handleTestStt = async () => {
    if (!transcription) return;
    setSttTesting(true);
    setSttTestResult(null);
    setSttTestError(null);
    try {
      const res = await testAiSettings({
        target: 'stt',
        transcription,
        api_key: sttKeyAction === 'replace' && sttKeyValue.trim() ? sttKeyValue.trim() : undefined,
      });
      setSttTestResult(res);
    } catch (err: any) {
      setSttTestError(err.message || 'Ошибка проверки подключения STT');
    } finally {
      setSttTesting(false);
    }
  };

  const handleTestLlm = async () => {
    if (!textAnalysis) return;
    setLlmTesting(true);
    setLlmTestResult(null);
    setLlmTestError(null);
    try {
      const res = await testAiSettings({
        target: 'llm',
        text_analysis: textAnalysis,
        api_key: llmKeyAction === 'replace' && llmKeyValue.trim() ? llmKeyValue.trim() : undefined,
      });
      setLlmTestResult(res);
    } catch (err: any) {
      setLlmTestError(err.message || 'Ошибка проверки подключения LLM');
    } finally {
      setLlmTesting(false);
    }
  };

  const handleSave = async () => {
    if (!serverSettings || !transcription || !textAnalysis) return;
    if (!serverSettings.can_update) {
      setActionError(`Сохранение заблокировано: ${serverSettings.update_blocker || 'активная операция'}`);
      return;
    }

    setIsSaving(true);
    setActionError(null);
    setSaveSuccessMsg(null);

    try {
      const updated = await updateAiSettings({
        expected_revision: serverSettings.revision,
        transcription,
        text_analysis: textAnalysis,
        stt_api_key: {
          action: sttKeyAction,
          value: sttKeyAction === 'replace' && sttKeyValue.trim() ? sttKeyValue.trim() : undefined,
        },
        llm_api_key: {
          action: llmKeyAction,
          value: llmKeyAction === 'replace' && llmKeyValue.trim() ? llmKeyValue.trim() : undefined,
        },
      });

      // Clear secrets from memory immediately
      setSttKeyValue('');
      setLlmKeyValue('');
      setSttKeyAction('preserve');
      setLlmKeyAction('preserve');

      setServerSettings(updated);
      setTranscription(updated.transcription);
      setTextAnalysis(updated.text_analysis);
      setEnableFallbacks(updated.text_analysis.fallback_models.length > 0);
      setSaveSuccessMsg(`Настройки успешно сохранены (Ревизия ${updated.revision})`);
    } catch (err: any) {
      console.error('Failed to update AI settings:', err);
      setActionError(err.message || 'Ошибка при сохранении настроек');
      // If OCC conflict (409), reload current settings to refresh revision and blockers
      if (err.message && err.message.includes('409')) {
        await fetchSettings();
      }
    } finally {
      setIsSaving(false);
    }
  };

  const handleAddFallbackModel = () => {
    if (!textAnalysis || textAnalysis.fallback_models.length >= 2) return;
    const newFallback: AnalysisModelSettings = {
      model_id: 'qwen/qwen3.7-plus',
      structured_output_mode: 'json_object',
      reasoning_policy: 'provider_default',
      supports_temperature: true,
      temperature: 0.0,
      max_output_tokens: 4096,
      context_window_tokens: 32768,
    };
    setTextAnalysis({
      ...textAnalysis,
      fallback_models: [...textAnalysis.fallback_models, newFallback],
    });
  };

  const handleRemoveFallbackModel = (index: number) => {
    if (!textAnalysis) return;
    const updated = textAnalysis.fallback_models.filter((_, i) => i !== index);
    setTextAnalysis({
      ...textAnalysis,
      fallback_models: updated,
    });
    if (updated.length === 0) {
      setEnableFallbacks(false);
    }
  };

  const handleUpdateFallbackModel = (index: number, patch: Partial<AnalysisModelSettings>) => {
    if (!textAnalysis) return;
    const updated = textAnalysis.fallback_models.map((m, i) => (i === index ? { ...m, ...patch } : m));
    setTextAnalysis({
      ...textAnalysis,
      fallback_models: updated,
    });
  };

  if (isLoading) {
    return (
      <div className="h-full flex flex-col items-center justify-center p-6 space-y-4 text-slate-400">
        <Activity className="w-8 h-8 animate-spin text-indigo-500" />
        <p className="text-sm">Загрузка настроек AI-провайдеров...</p>
      </div>
    );
  }

  if (loadError || !serverSettings || !transcription || !textAnalysis) {
    return (
      <div className="h-full flex flex-col items-center justify-center p-6 space-y-4 max-w-lg mx-auto">
        <div className="p-3 bg-rose-950/60 border border-rose-800 text-rose-300 rounded-xl flex items-center space-x-3 w-full">
          <AlertCircle className="w-5 h-5 shrink-0 text-rose-400" />
          <span className="text-sm leading-relaxed">{loadError || 'Не удалось загрузить конфигурацию'}</span>
        </div>
        <button
          type="button"
          onClick={fetchSettings}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold rounded-lg shadow transition cursor-pointer"
        >
          Повторить попытку
        </button>
      </div>
    );
  }

  const renderKeyControl = (
    status: { configured: boolean; source: string; editable: boolean },
    action: ApiKeyAction,
    setAction: (a: ApiKeyAction) => void,
    val: string,
    setVal: (s: string) => void
  ) => {
    const isEnv = status.source === 'process_environment';

    if (isEnv) {
      return (
        <div className="space-y-1.5">
          <div className="flex items-center space-x-2">
            <span className="inline-flex items-center space-x-1 px-2 py-0.5 text-xs font-medium text-sky-300 bg-sky-950/70 border border-sky-800 rounded-md">
              <Lock className="w-3 h-3 text-sky-400" />
              <span>Задан через переменные окружения (.env)</span>
            </span>
          </div>
          <p className="text-[11px] text-slate-400">
            Значение управляется системным окружением процесса и не может быть изменено через интерфейс.
          </p>
        </div>
      );
    }

    if (action === 'replace') {
      return (
        <div className="space-y-2">
          <div className="flex items-center space-x-2">
            <input
              type="password"
              placeholder="Введите новый API-ключ..."
              value={val}
              onChange={(e) => setVal(e.target.value)}
              className="flex-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-indigo-500"
              autoComplete="off"
            />
            <button
              type="button"
              onClick={() => {
                setAction('preserve');
                setVal('');
              }}
              className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded-lg transition"
            >
              Отмена
            </button>
          </div>
          <p className="text-[11px] text-amber-400/90">
            Новый ключ будет сохранён в защищённом хранилище и никогда не будет возвращён обратно в браузер.
          </p>
        </div>
      );
    }

    if (action === 'clear') {
      return (
        <div className="flex items-center justify-between p-2 bg-rose-950/40 border border-rose-800/80 rounded-lg">
          <span className="text-xs text-rose-300">Ключ будет удалён при сохранении</span>
          <button
            type="button"
            onClick={() => setAction('preserve')}
            className="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded-md transition"
          >
            Отменить удаление
          </button>
        </div>
      );
    }

    // Default 'preserve' state
    return (
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center space-x-2">
          {status.configured ? (
            <span className="inline-flex items-center space-x-1 px-2.5 py-1 text-xs font-semibold text-emerald-300 bg-emerald-950/70 border border-emerald-800 rounded-md">
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
              <span>Ключ настроен (в хранилище)</span>
            </span>
          ) : (
            <span className="inline-flex items-center space-x-1 px-2.5 py-1 text-xs font-semibold text-amber-300 bg-amber-950/70 border border-amber-800 rounded-md">
              <AlertCircle className="w-3.5 h-3.5 text-amber-400" />
              <span>Ключ отсутствует</span>
            </span>
          )}
        </div>

        <div className="flex items-center space-x-2">
          <button
            type="button"
            onClick={() => setAction('replace')}
            className="px-3 py-1 bg-indigo-950/80 hover:bg-indigo-900 border border-indigo-700 text-indigo-200 text-xs font-medium rounded-lg transition"
          >
            {status.configured ? 'Заменить ключ' : 'Ввести ключ'}
          </button>
          {status.configured && (
            <button
              type="button"
              onClick={() => {
                if (window.confirm('Вы уверены, что хотите удалить сохранённый API-ключ?')) {
                  setAction('clear');
                }
              }}
              className="px-3 py-1 bg-rose-950/80 hover:bg-rose-900 border border-rose-800 text-rose-200 text-xs font-medium rounded-lg transition"
            >
              Удалить
            </button>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className="h-full flex flex-col overflow-hidden bg-[var(--app-bg)]">
      {/* Header */}
      <div className="p-6 border-b border-[var(--border)] shrink-0 bg-[var(--surface)]">
        <div className="max-w-5xl mx-auto flex items-start justify-between gap-4">
          <div>
            <h1 className="text-xl font-bold text-[var(--text-primary)] flex items-center space-x-2">
              <Sparkles className="w-5 h-5 text-indigo-500" />
              <span>Настройки AI-провайдеров</span>
            </h1>
            <p className="text-xs text-[var(--text-muted)] mt-1">
              Глобальная независимая конфигурация распознавания речи (STT) и текстового анализа (LLM). Изменения применяются
              без перезапуска приложения.
            </p>
          </div>

          <div className="flex items-center space-x-3 text-right">
            <div className="text-xs">
              <div className="text-[var(--text-muted)]">
                Ревизия:{' '}
                <span className="font-semibold text-[var(--text-primary)]">{serverSettings.revision}</span>
              </div>
              <div className="text-[11px] text-[var(--text-faint)]">
                Источник: {serverSettings.source === 'database' ? 'База данных' : 'Переменные окружения'}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        <div className="max-w-5xl mx-auto space-y-6">
          {/* Active Blocker Warning */}
          {!serverSettings.can_update && (
            <div className="p-3.5 bg-amber-950/50 border border-amber-800 rounded-xl flex items-center space-x-3 text-amber-200 text-xs">
              <AlertCircle className="w-5 h-5 shrink-0 text-amber-400" />
              <div>
                <span className="font-semibold">Сохранение временно недоступно: </span>
                <span>{serverSettings.update_blocker || 'выполняются активные задачи или идёт запись интервью.'}</span>
              </div>
            </div>
          )}

          {/* Action Error Banner */}
          {actionError && (
            <div className="p-3 bg-rose-950/60 border border-rose-800 text-rose-300 text-xs rounded-xl flex items-center space-x-2">
              <AlertCircle className="w-4 h-4 shrink-0 text-rose-400" />
              <span>{actionError}</span>
            </div>
          )}

          {/* Success Banner */}
          {saveSuccessMsg && (
            <div className="p-3 bg-emerald-950/60 border border-emerald-800 text-emerald-300 text-xs rounded-xl flex items-center space-x-2">
              <CheckCircle2 className="w-4 h-4 shrink-0 text-emerald-400" />
              <span>{saveSuccessMsg}</span>
            </div>
          )}

          {/* Card 1: Transcription (STT) */}
          <div className="p-5 bg-[var(--surface)] border border-[var(--border)] rounded-2xl space-y-4 shadow-sm">
            <div className="flex items-center justify-between border-b border-[var(--border)] pb-3">
              <div className="flex items-center space-x-2">
                <Layers className="w-4 h-4 text-indigo-500" />
                <h2 className="text-sm font-bold text-[var(--text-primary)]">Распознавание речи (STT)</h2>
              </div>

              {/* Preset Selector */}
              <div className="flex items-center space-x-2">
                <span className="text-xs text-[var(--text-muted)]">Пресет:</span>
                <select
                  value={transcription.preset}
                  onChange={(e) => handlePresetChangeStt(e.target.value as ProviderPreset)}
                  className="bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 cursor-pointer"
                >
                  <option value="routerai">RouterAI (RF)</option>
                  <option value="plusvibe">PlusVibe API (RF)</option>
                  <option value="custom">Пользовательский (Custom)</option>
                </select>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {/* Provider ID & Name */}
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)]">Название провайдера</label>
                <input
                  type="text"
                  value={transcription.provider_name}
                  onChange={(e) => setTranscription({ ...transcription, provider_name: e.target.value })}
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                />
              </div>

              {/* Model ID */}
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)]">Идентификатор модели</label>
                <input
                  type="text"
                  value={transcription.model_id}
                  onChange={(e) => setTranscription({ ...transcription, model_id: e.target.value })}
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                />
              </div>

              {/* Endpoint URL */}
              <div className="space-y-1.5 md:col-span-2">
                <label className="text-xs font-medium text-[var(--text-secondary)]">URL эндпоинта транскрибации</label>
                <input
                  type="url"
                  value={transcription.endpoint_url}
                  onChange={(e) => setTranscription({ ...transcription, endpoint_url: e.target.value })}
                  placeholder="https://..."
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 font-mono"
                />
              </div>

              {/* Auth Mode */}
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)]">Режим авторизации</label>
                <select
                  value={transcription.auth_mode}
                  onChange={(e) => setTranscription({ ...transcription, auth_mode: e.target.value as AuthMode })}
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 cursor-pointer"
                >
                  <option value="bearer">Bearer Token (Authorization Header)</option>
                  <option value="none">Без авторизации (None)</option>
                </select>
              </div>

              {/* Language */}
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)]">Язык по умолчанию</label>
                <input
                  type="text"
                  value={transcription.language}
                  onChange={(e) => setTranscription({ ...transcription, language: e.target.value })}
                  placeholder="ru"
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                />
              </div>
            </div>

            {/* API Key Box */}
            {transcription.auth_mode === 'bearer' && (
              <div className="p-3.5 bg-slate-950/70 border border-slate-800 rounded-xl space-y-2 mt-2">
                <div className="flex items-center space-x-1.5 text-xs font-semibold text-slate-300">
                  <Key className="w-3.5 h-3.5 text-indigo-400" />
                  <span>API-ключ STT</span>
                </div>
                {renderKeyControl(
                  serverSettings.stt_credentials,
                  sttKeyAction,
                  setSttKeyAction,
                  sttKeyValue,
                  setSttKeyValue
                )}
              </div>
            )}

            {/* Test Connection STT */}
            <div className="pt-2 flex items-center justify-between flex-wrap gap-2 border-t border-[var(--border)]">
              <button
                type="button"
                onClick={handleTestStt}
                disabled={sttTesting}
                className="px-3.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-lg transition flex items-center space-x-1.5 cursor-pointer disabled:opacity-50"
              >
                <Activity className={`w-3.5 h-3.5 ${sttTesting ? 'animate-spin text-indigo-400' : ''}`} />
                <span>{sttTesting ? 'Проверка...' : 'Проверить подключение STT'}</span>
              </button>

              {sttTestResult && (
                <div
                  className={`text-xs px-2.5 py-1 rounded-md flex items-center space-x-1.5 ${
                    sttTestResult.success
                      ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-800'
                      : 'bg-rose-950/80 text-rose-300 border border-rose-800'
                  }`}
                >
                  {sttTestResult.success ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                  ) : (
                    <AlertCircle className="w-3.5 h-3.5 text-rose-400" />
                  )}
                  <span>
                    {sttTestResult.success ? 'Успешно' : 'Ошибка'} ({Math.round(sttTestResult.latency_ms)}мс) —{' '}
                    {sttTestResult.message}
                  </span>
                </div>
              )}

              {sttTestError && (
                <div className="text-xs px-2.5 py-1 rounded-md bg-rose-950/80 text-rose-300 border border-rose-800 flex items-center space-x-1.5">
                  <AlertCircle className="w-3.5 h-3.5 text-rose-400" />
                  <span>{sttTestError}</span>
                </div>
              )}
            </div>
          </div>

          {/* Card 2: Text Analysis (LLM) */}
          <div className="p-5 bg-[var(--surface)] border border-[var(--border)] rounded-2xl space-y-4 shadow-sm">
            <div className="flex items-center justify-between border-b border-[var(--border)] pb-3">
              <div className="flex items-center space-x-2">
                <Sparkles className="w-4 h-4 text-indigo-500" />
                <h2 className="text-sm font-bold text-[var(--text-primary)]">Анализ текста (LLM)</h2>
              </div>

              {/* Preset Selector */}
              <div className="flex items-center space-x-2">
                <span className="text-xs text-[var(--text-muted)]">Пресет:</span>
                <select
                  value={textAnalysis.preset}
                  onChange={(e) => handlePresetChangeLlm(e.target.value as ProviderPreset)}
                  className="bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 cursor-pointer"
                >
                  <option value="routerai">RouterAI (RF)</option>
                  <option value="plusvibe">PlusVibe API (RF)</option>
                  <option value="custom">Пользовательский (Custom)</option>
                </select>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {/* Provider ID & Name */}
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)]">Название провайдера</label>
                <input
                  type="text"
                  value={textAnalysis.provider_name}
                  onChange={(e) => setTextAnalysis({ ...textAnalysis, provider_name: e.target.value })}
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                />
              </div>

              {/* Auth Mode */}
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)]">Режим авторизации</label>
                <select
                  value={textAnalysis.auth_mode}
                  onChange={(e) => setTextAnalysis({ ...textAnalysis, auth_mode: e.target.value as AuthMode })}
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 cursor-pointer"
                >
                  <option value="bearer">Bearer Token (Authorization Header)</option>
                  <option value="none">Без авторизации (None)</option>
                </select>
              </div>

              {/* Base URL */}
              <div className="space-y-1.5 md:col-span-2">
                <label className="text-xs font-medium text-[var(--text-secondary)]">Базовый URL (без /chat/completions)</label>
                <input
                  type="url"
                  value={textAnalysis.base_url}
                  onChange={(e) => setTextAnalysis({ ...textAnalysis, base_url: e.target.value })}
                  placeholder="https://..."
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 font-mono"
                />
              </div>
            </div>

            {/* API Key Box */}
            {textAnalysis.auth_mode === 'bearer' && (
              <div className="p-3.5 bg-slate-950/70 border border-slate-800 rounded-xl space-y-2 mt-2">
                <div className="flex items-center space-x-1.5 text-xs font-semibold text-slate-300">
                  <Key className="w-3.5 h-3.5 text-indigo-400" />
                  <span>API-ключ LLM</span>
                </div>
                {renderKeyControl(
                  serverSettings.llm_credentials,
                  llmKeyAction,
                  setLlmKeyAction,
                  llmKeyValue,
                  setLlmKeyValue
                )}
              </div>
            )}

            {/* Primary Model Section */}
            <div className="p-4 bg-slate-950/40 border border-slate-800/80 rounded-xl space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-bold text-indigo-300 uppercase tracking-wider">Основная модель (Primary)</h3>
                <span className="text-[11px] text-slate-400">Используется по умолчанию для всех задач</span>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div className="space-y-1">
                  <label className="text-[11px] text-slate-400">Идентификатор модели</label>
                  <input
                    type="text"
                    value={textAnalysis.primary_model.model_id}
                    onChange={(e) =>
                      setTextAnalysis({
                        ...textAnalysis,
                        primary_model: { ...textAnalysis.primary_model, model_id: e.target.value },
                      })
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 font-mono"
                  />
                </div>

                <div className="space-y-1">
                  <label className="text-[11px] text-slate-400">Структурированный вывод</label>
                  <select
                    value={textAnalysis.primary_model.structured_output_mode}
                    onChange={(e) =>
                      setTextAnalysis({
                        ...textAnalysis,
                        primary_model: {
                          ...textAnalysis.primary_model,
                          structured_output_mode: e.target.value as StructuredOutputMode,
                        },
                      })
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 cursor-pointer"
                  >
                    <option value="json_object">JSON Object (response_format)</option>
                    <option value="json_schema">JSON Schema (Strict Structured Output)</option>
                  </select>
                </div>

                <div className="space-y-1">
                  <label className="text-[11px] text-slate-400">Политика reasoning</label>
                  <select
                    value={textAnalysis.primary_model.reasoning_policy}
                    onChange={(e) =>
                      setTextAnalysis({
                        ...textAnalysis,
                        primary_model: {
                          ...textAnalysis.primary_model,
                          reasoning_policy: e.target.value as ReasoningPolicy,
                        },
                      })
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 cursor-pointer"
                  >
                    <option value="provider_default">По умолчанию провайдера</option>
                    <option value="disable_enable_thinking">Отключить enable_thinking: false</option>
                    <option value="disable_reasoning_effort">Отключить reasoning_effort: none</option>
                  </select>
                </div>

                <div className="space-y-1">
                  <label className="text-[11px] text-slate-400">Температура ({textAnalysis.primary_model.temperature})</label>
                  <input
                    type="range"
                    min="0"
                    max="2"
                    step="0.05"
                    value={textAnalysis.primary_model.temperature}
                    onChange={(e) =>
                      setTextAnalysis({
                        ...textAnalysis,
                        primary_model: {
                          ...textAnalysis.primary_model,
                          temperature: parseFloat(e.target.value),
                        },
                      })
                    }
                    className="w-full accent-indigo-500 cursor-pointer"
                  />
                </div>
              </div>
            </div>

            {/* Fallback Models Section */}
            <div className="space-y-3 pt-2">
              <div className="flex items-center justify-between">
                <label className="flex items-center space-x-2 text-xs font-semibold text-[var(--text-primary)] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={enableFallbacks}
                    onChange={(e) => {
                      const enabled = e.target.checked;
                      setEnableFallbacks(enabled);
                      if (!enabled) {
                        setTextAnalysis({ ...textAnalysis, fallback_models: [] });
                      } else if (textAnalysis.fallback_models.length === 0) {
                        handleAddFallbackModel();
                      }
                    }}
                    className="rounded bg-slate-950 border-slate-700 text-indigo-600 focus:ring-0"
                  />
                  <span>Использовать резервные модели (Fallback) при сбоях</span>
                </label>

                {enableFallbacks && textAnalysis.fallback_models.length < 2 && (
                  <button
                    type="button"
                    onClick={handleAddFallbackModel}
                    className="px-2.5 py-1 bg-indigo-950 hover:bg-indigo-900 border border-indigo-700 text-indigo-200 text-xs font-medium rounded-md flex items-center space-x-1 transition cursor-pointer"
                  >
                    <Plus className="w-3 h-3" />
                    <span>Добавить модель</span>
                  </button>
                )}
              </div>

              {enableFallbacks && (
                <div className="space-y-3 pl-4 border-l-2 border-indigo-900/60">
                  {textAnalysis.fallback_models.map((fb, idx) => (
                    <div
                      key={idx}
                      className="p-3 bg-slate-950/50 border border-slate-800 rounded-xl space-y-3 relative"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-xs font-semibold text-slate-300">
                          Резервная модель #{idx + 1}
                        </span>
                        <button
                          type="button"
                          onClick={() => handleRemoveFallbackModel(idx)}
                          className="text-slate-400 hover:text-rose-400 p-1 transition"
                          title="Удалить резервную модель"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>

                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        <div className="space-y-1">
                          <label className="text-[11px] text-slate-400">Идентификатор модели</label>
                          <input
                            type="text"
                            value={fb.model_id}
                            onChange={(e) => handleUpdateFallbackModel(idx, { model_id: e.target.value })}
                            className="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 font-mono"
                          />
                        </div>

                        <div className="space-y-1">
                          <label className="text-[11px] text-slate-400">Структурированный вывод</label>
                          <select
                            value={fb.structured_output_mode}
                            onChange={(e) =>
                              handleUpdateFallbackModel(idx, {
                                structured_output_mode: e.target.value as StructuredOutputMode,
                              })
                            }
                            className="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 cursor-pointer"
                          >
                            <option value="json_object">JSON Object</option>
                            <option value="json_schema">JSON Schema</option>
                          </select>
                        </div>

                        <div className="space-y-1">
                          <label className="text-[11px] text-slate-400">Политика reasoning</label>
                          <select
                            value={fb.reasoning_policy}
                            onChange={(e) =>
                              handleUpdateFallbackModel(idx, {
                                reasoning_policy: e.target.value as ReasoningPolicy,
                              })
                            }
                            className="w-full bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 cursor-pointer"
                          >
                            <option value="provider_default">По умолчанию провайдера</option>
                            <option value="disable_enable_thinking">Отключить enable_thinking</option>
                            <option value="disable_reasoning_effort">Отключить reasoning_effort</option>
                          </select>
                        </div>

                        <div className="space-y-1">
                          <label className="text-[11px] text-slate-400">Температура ({fb.temperature})</label>
                          <input
                            type="range"
                            min="0"
                            max="2"
                            step="0.05"
                            value={fb.temperature}
                            onChange={(e) =>
                              handleUpdateFallbackModel(idx, { temperature: parseFloat(e.target.value) })
                            }
                            className="w-full accent-indigo-500 cursor-pointer"
                          />
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Test Connection LLM */}
            <div className="pt-2 flex items-center justify-between flex-wrap gap-2 border-t border-[var(--border)]">
              <button
                type="button"
                onClick={handleTestLlm}
                disabled={llmTesting}
                className="px-3.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-lg transition flex items-center space-x-1.5 cursor-pointer disabled:opacity-50"
              >
                <Activity className={`w-3.5 h-3.5 ${llmTesting ? 'animate-spin text-indigo-400' : ''}`} />
                <span>{llmTesting ? 'Проверка...' : 'Проверить подключение LLM'}</span>
              </button>

              {llmTestResult && (
                <div
                  className={`text-xs px-2.5 py-1 rounded-md flex items-center space-x-1.5 ${
                    llmTestResult.success
                      ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-800'
                      : 'bg-rose-950/80 text-rose-300 border border-rose-800'
                  }`}
                >
                  {llmTestResult.success ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                  ) : (
                    <AlertCircle className="w-3.5 h-3.5 text-rose-400" />
                  )}
                  <span>
                    {llmTestResult.success ? 'Успешно' : 'Ошибка'} ({Math.round(llmTestResult.latency_ms)}мс) [
                    {llmTestResult.model_id || textAnalysis.primary_model.model_id}] — {llmTestResult.message}
                  </span>
                </div>
              )}

              {llmTestError && (
                <div className="text-xs px-2.5 py-1 rounded-md bg-rose-950/80 text-rose-300 border border-rose-800 flex items-center space-x-1.5">
                  <AlertCircle className="w-3.5 h-3.5 text-rose-400" />
                  <span>{llmTestError}</span>
                </div>
              )}
            </div>
          </div>

          {/* Collapsible Advanced Settings */}
          <div className="p-4 bg-[var(--surface)] border border-[var(--border)] rounded-2xl shadow-sm">
            <button
              type="button"
              onClick={() => setShowAdvanced(!showAdvanced)}
              className="w-full flex items-center justify-between text-xs font-bold text-[var(--text-primary)] hover:text-indigo-400 transition cursor-pointer"
            >
              <div className="flex items-center space-x-2">
                <Clock className="w-4 h-4 text-indigo-500" />
                <span>Дополнительные параметры (таймауты и параллельность)</span>
              </div>
              {showAdvanced ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
            </button>

            {showAdvanced && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4 pt-4 border-t border-[var(--border)]">
                <div className="space-y-1.5">
                  <label className="text-xs text-[var(--text-secondary)]">STT таймаут запроса (сек)</label>
                  <input
                    type="number"
                    min="1"
                    max="300"
                    value={transcription.timeout_seconds}
                    onChange={(e) =>
                      setTranscription({ ...transcription, timeout_seconds: parseFloat(e.target.value) || 60 })
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs text-[var(--text-secondary)]">STT параллельных потоков</label>
                  <input
                    type="number"
                    min="1"
                    max="20"
                    value={transcription.max_concurrency}
                    onChange={(e) =>
                      setTranscription({ ...transcription, max_concurrency: parseInt(e.target.value, 10) || 2 })
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs text-[var(--text-secondary)]">LLM таймаут запроса (сек)</label>
                  <input
                    type="number"
                    min="1"
                    max="300"
                    value={textAnalysis.timeout_seconds}
                    onChange={(e) =>
                      setTextAnalysis({ ...textAnalysis, timeout_seconds: parseFloat(e.target.value) || 60 })
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                  />
                </div>

                <div className="space-y-1.5">
                  <label className="text-xs text-[var(--text-secondary)]">LLM параллельных запросов</label>
                  <input
                    type="number"
                    min="1"
                    max="100"
                    value={textAnalysis.max_concurrency}
                    onChange={(e) =>
                      setTextAnalysis({ ...textAnalysis, max_concurrency: parseInt(e.target.value, 10) || 10 })
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                  />
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Footer Actions */}
      <div className="p-4 border-t border-[var(--border)] bg-[var(--surface)] shrink-0">
        <div className="max-w-5xl mx-auto flex items-center justify-between">
          <div className="text-xs text-[var(--text-muted)] flex items-center space-x-2">
            {isDirty ? (
              <span className="text-amber-400 font-medium">Есть несохранённые изменения</span>
            ) : (
              <span>Все изменения сохранены</span>
            )}
          </div>

          <div className="flex items-center space-x-3">
            <button
              type="button"
              onClick={handleReset}
              disabled={!isDirty || isSaving}
              className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded-xl transition cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed flex items-center space-x-1.5"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              <span>Сбросить</span>
            </button>

            <button
              type="button"
              onClick={handleSave}
              disabled={!isDirty || isSaving || !serverSettings.can_update}
              className={`px-5 py-2 text-xs font-semibold rounded-xl shadow transition cursor-pointer flex items-center space-x-1.5 ${
                !isDirty || isSaving || !serverSettings.can_update
                  ? 'bg-slate-800 text-slate-500 cursor-not-allowed'
                  : 'bg-indigo-600 hover:bg-indigo-500 text-white shadow-indigo-600/30'
              }`}
            >
              <Save className="w-3.5 h-3.5" />
              <span>{isSaving ? 'Сохранение...' : 'Сохранить'}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
