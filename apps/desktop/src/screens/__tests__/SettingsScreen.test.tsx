import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { SettingsScreen } from '../SettingsScreen';
import { Header } from '../../components/Header';
import * as api from '../../services/api';
import { AiSettingsResponse, TestAiSettingsResponse } from '../../types';

vi.mock('../../services/api', () => ({
  getAiSettings: vi.fn(),
  updateAiSettings: vi.fn(),
  testAiSettings: vi.fn(),
}));

describe('SettingsScreen Component', () => {
  const mockInitialSettings: AiSettingsResponse = {
    revision: 1,
    source: 'database',
    transcription: {
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
    text_analysis: {
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
      fallback_models: [],
    },
    stt_credentials: {
      configured: true,
      source: 'runtime_file',
      editable: true,
    },
    llm_credentials: {
      configured: true,
      source: 'runtime_file',
      editable: true,
    },
    can_update: true,
    update_blocker: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads and renders settings with STT and LLM cards', async () => {
    vi.mocked(api.getAiSettings).mockResolvedValue(mockInitialSettings);

    render(<SettingsScreen />);

    expect(screen.getByText(/Загрузка настроек AI-провайдеров/i)).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText('Настройки AI-провайдеров')).toBeInTheDocument();
    });

    expect(screen.getByText(/Ревизия:/i)).toBeInTheDocument();
    expect(screen.getByText(/Источник:/i)).toBeInTheDocument();

    // STT Card
    expect(screen.getByText('Распознавание речи (STT)')).toBeInTheDocument();
    expect(screen.getByDisplayValue('openai/gpt-transcribe')).toBeInTheDocument();
    expect(screen.getByDisplayValue('https://routerai.ru/api/v1/audio/transcriptions')).toBeInTheDocument();

    // LLM Card
    expect(screen.getByText('Анализ текста (LLM)')).toBeInTheDocument();
    expect(screen.getByDisplayValue('qwen/qwen3.7-flash')).toBeInTheDocument();
    expect(screen.getByDisplayValue('https://routerai.ru/api/v1')).toBeInTheDocument();

    // Keys are not returned and inputs are empty, but status is shown
    expect(screen.getAllByText('Ключ настроен (в хранилище)').length).toBe(2);
  });

  it('disables save button and displays blocker warning when can_update is false', async () => {
    const blockedSettings: AiSettingsResponse = {
      ...mockInitialSettings,
      can_update: false,
      update_blocker: 'Идёт запись собеседования',
    };
    vi.mocked(api.getAiSettings).mockResolvedValue(blockedSettings);

    render(<SettingsScreen />);

    await waitFor(() => {
      expect(screen.getByText(/Идёт запись собеседования/i)).toBeInTheDocument();
    });

    const saveBtn = screen.getByRole('button', { name: /Сохранить/i });
    expect(saveBtn).toBeDisabled();
  });

  it('shows environment-managed notice when credential source is process_environment', async () => {
    const envSettings: AiSettingsResponse = {
      ...mockInitialSettings,
      stt_credentials: {
        configured: true,
        source: 'process_environment',
        editable: false,
      },
    };
    vi.mocked(api.getAiSettings).mockResolvedValue(envSettings);

    render(<SettingsScreen />);

    await waitFor(() => {
      expect(screen.getByText('Задан через переменные окружения (.env)')).toBeInTheDocument();
    });
  });

  it('handles API key replacement workflow and clears secret after successful save', async () => {
    vi.mocked(api.getAiSettings).mockResolvedValue(mockInitialSettings);
    const updatedResponse: AiSettingsResponse = {
      ...mockInitialSettings,
      revision: 2,
    };
    vi.mocked(api.updateAiSettings).mockResolvedValue(updatedResponse);

    render(<SettingsScreen />);

    await waitFor(() => {
      expect(screen.getByText('Настройки AI-провайдеров')).toBeInTheDocument();
    });

    // Click "Заменить ключ" in STT section
    const replaceButtons = screen.getAllByRole('button', { name: 'Заменить ключ' });
    fireEvent.click(replaceButtons[0]);

    // Input field should now appear
    const passwordInput = screen.getByPlaceholderText('Введите новый API-ключ...');
    expect(passwordInput).toBeInTheDocument();

    fireEvent.change(passwordInput, { target: { value: 'sk-new-stt-key-123' } });

    // Save button should become enabled
    const saveBtn = screen.getByRole('button', { name: /Сохранить/i });
    expect(saveBtn).not.toBeDisabled();

    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(api.updateAiSettings).toHaveBeenCalledWith({
        expected_revision: 1,
        transcription: mockInitialSettings.transcription,
        text_analysis: mockInitialSettings.text_analysis,
        stt_api_key: {
          action: 'replace',
          value: 'sk-new-stt-key-123',
        },
        llm_api_key: {
          action: 'preserve',
          value: undefined,
        },
      });
    });

    // Success banner should show new revision
    await waitFor(() => {
      expect(screen.getByText(/Настройки успешно сохранены \(Ревизия 2\)/i)).toBeInTheDocument();
    });

    // Password input should be removed and secret cleared
    expect(screen.queryByPlaceholderText('Введите новый API-ключ...')).not.toBeInTheDocument();
  });

  it('handles API key clear action', async () => {
    vi.mocked(api.getAiSettings).mockResolvedValue(mockInitialSettings);
    vi.mocked(api.updateAiSettings).mockResolvedValue({ ...mockInitialSettings, revision: 2 });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<SettingsScreen />);

    await waitFor(() => {
      expect(screen.getByText('Настройки AI-провайдеров')).toBeInTheDocument();
    });

    // Click "Удалить" on STT key
    const deleteButtons = screen.getAllByRole('button', { name: 'Удалить' });
    fireEvent.click(deleteButtons[0]);

    expect(screen.getByText('Ключ будет удалён при сохранении')).toBeInTheDocument();

    const saveBtn = screen.getByRole('button', { name: /Сохранить/i });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(api.updateAiSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          stt_api_key: {
            action: 'clear',
            value: undefined,
          },
        })
      );
    });
  });

  it('runs STT and LLM connection probe without saving', async () => {
    vi.mocked(api.getAiSettings).mockResolvedValue(mockInitialSettings);
    const mockProbeRes: TestAiSettingsResponse = {
      target: 'stt',
      success: true,
      provider_id: 'routerai',
      model_id: 'openai/gpt-transcribe',
      latency_ms: 154.2,
      message: 'STT endpoint accepts synthetic audio',
    };
    vi.mocked(api.testAiSettings).mockResolvedValue(mockProbeRes);

    render(<SettingsScreen />);

    await waitFor(() => {
      expect(screen.getByText('Настройки AI-провайдеров')).toBeInTheDocument();
    });

    const testSttBtn = screen.getByRole('button', { name: /Проверить подключение STT/i });
    fireEvent.click(testSttBtn);

    await waitFor(() => {
      expect(api.testAiSettings).toHaveBeenCalledWith({
        target: 'stt',
        transcription: mockInitialSettings.transcription,
        api_key: undefined,
      });
    });

    expect(screen.getByText(/Успешно \(154мс\) — STT endpoint accepts synthetic audio/i)).toBeInTheDocument();
    // Probe must NOT trigger updateAiSettings
    expect(api.updateAiSettings).not.toHaveBeenCalled();
  });

  it('allows adding and removing fallback models for LLM', async () => {
    vi.mocked(api.getAiSettings).mockResolvedValue(mockInitialSettings);

    render(<SettingsScreen />);

    await waitFor(() => {
      expect(screen.getByText('Настройки AI-провайдеров')).toBeInTheDocument();
    });

    // Check "Использовать резервные модели"
    const checkbox = screen.getByLabelText(/Использовать резервные модели \(Fallback\) при сбоях/i);
    fireEvent.click(checkbox);

    // Fallback model #1 should appear
    expect(screen.getByText('Резервная модель #1')).toBeInTheDocument();

    // Add another fallback model
    const addBtn = screen.getByRole('button', { name: /Добавить модель/i });
    fireEvent.click(addBtn);

    expect(screen.getByText('Резервная модель #2')).toBeInTheDocument();

    // Max 2 fallbacks reached, add button should disappear
    expect(screen.queryByRole('button', { name: /Добавить модель/i })).not.toBeInTheDocument();

    // Remove the first fallback
    const removeButtons = screen.getAllByTitle('Удалить резервную модель');
    expect(removeButtons.length).toBe(2);
    fireEvent.click(removeButtons[0]);

    // Now only 1 fallback remains
    expect(screen.queryByText('Резервная модель #2')).not.toBeInTheDocument();
    expect(screen.getByText('Резервная модель #1')).toBeInTheDocument();
  });

  it('displays 409 conflict and refreshes settings', async () => {
    vi.mocked(api.getAiSettings).mockResolvedValue(mockInitialSettings);
    vi.mocked(api.updateAiSettings).mockRejectedValue(
      new Error('409: Конфликт ревизий: текущая ревизия 2, ожидалась 1')
    );

    render(<SettingsScreen />);

    await waitFor(() => {
      expect(screen.getByText('Настройки AI-провайдеров')).toBeInTheDocument();
    });

    // Modify a field to make form dirty
    const modelInput = screen.getByDisplayValue('openai/gpt-transcribe');
    fireEvent.change(modelInput, { target: { value: 'custom-whisper' } });

    const saveBtn = screen.getByRole('button', { name: /Сохранить/i });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(screen.getByText(/409: Конфликт ревизий/i)).toBeInTheDocument();
    });

    // On 409, fetchSettings should be triggered again to refresh state
    expect(api.getAiSettings).toHaveBeenCalledTimes(2);
  });

  it('renders Header with Settings button and triggers navigation to settings', () => {
    const onNavigate = vi.fn();
    render(
      <Header
        status="draft"
        currentScreen="home"
        onNavigate={onNavigate}
      />
    );

    const settingsBtn = screen.getByRole('button', { name: /Настройки/i });
    expect(settingsBtn).toBeInTheDocument();
    fireEvent.click(settingsBtn);
    expect(onNavigate).toHaveBeenCalledWith('settings');
  });
});
