import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { JobTemplatesScreen } from '../JobTemplatesScreen';
import * as api from '../../services/api';
import { JobTemplate } from '../../types';

vi.mock('../../services/api', () => ({
  listJobTemplates: vi.fn(),
  createJobTemplate: vi.fn(),
  updateJobTemplate: vi.fn(),
  duplicateJobTemplate: vi.fn(),
  archiveJobTemplate: vi.fn(),
  unarchiveJobTemplate: vi.fn(),
  deleteJobTemplate: vi.fn(),
  copyQuestionToTemplate: vi.fn(),
}));

describe('JobTemplatesScreen Component', () => {
  const mockTemplates: JobTemplate[] = [
    {
      id: 'tpl-spider',
      title: 'Паук',
      role: 'старший паук',
      level: 'Senior',
      description: 'Пауки',
      version: 4,
      is_archived: false,
      created_at: '2026-09-01T10:00:00Z',
      updated_at: '2026-09-01T10:00:00Z',
      questions: [
        {
          id: 'q-spider-1',
          title: 'Плетение паутины',
          prompt: 'Как вы строите круговые паутины?',
          weight: 1.0,
          criteria: [
            {
              id: 'c-spider-1',
              title: 'Прочность нити',
              description: 'Понимание состава шелка',
              min_score: 1.0,
              max_score: 5.0,
              weight: 1.0,
            },
          ],
        },
      ],
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads and displays initial template in editing mode', async () => {
    vi.mocked(api.listJobTemplates).mockResolvedValue(mockTemplates);

    render(<JobTemplatesScreen />);

    // Wait for templates to load
    await waitFor(() => {
      expect(screen.getByText('Редактирование должности')).toBeInTheDocument();
    });

    const titleInput = screen.getByDisplayValue('Паук');
    expect(titleInput).toBeInTheDocument();
    expect(screen.getByDisplayValue('старший паук')).toBeInTheDocument();
  });

  it('switches to create mode when clicking "+ Создать" without resetting back to first template', async () => {
    vi.mocked(api.listJobTemplates).mockResolvedValue(mockTemplates);

    render(<JobTemplatesScreen />);

    // Wait for initial load
    await waitFor(() => {
      expect(screen.getByText('Редактирование должности')).toBeInTheDocument();
    });

    // Click "+ Создать" button in sidebar header
    const createBtn = screen.getByRole('button', { name: /Создать/i });
    fireEvent.click(createBtn);

    // Header must change to "Создание новой должности"
    await waitFor(() => {
      expect(screen.getByText('Создание новой должности')).toBeInTheDocument();
    });

    // Inputs must be reset to new template default values
    expect(screen.getByDisplayValue('Новая должность')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Software Engineer')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Создать должность/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Отмена/i })).toBeInTheDocument();

    // Ensure listJobTemplates was not called again to overwrite state
    // (Only the initial mount call should have been made)
    expect(api.listJobTemplates).toHaveBeenCalledTimes(1);
  });

  it('allows canceling template creation and returning to existing template', async () => {
    vi.mocked(api.listJobTemplates).mockResolvedValue(mockTemplates);

    render(<JobTemplatesScreen />);

    await waitFor(() => {
      expect(screen.getByText('Редактирование должности')).toBeInTheDocument();
    });

    // Enter creation mode
    const createBtn = screen.getByRole('button', { name: /Создать/i });
    fireEvent.click(createBtn);

    await waitFor(() => {
      expect(screen.getByText('Создание новой должности')).toBeInTheDocument();
    });

    // Click cancel
    const cancelBtn = screen.getByRole('button', { name: /Отмена/i });
    fireEvent.click(cancelBtn);

    // Must revert back to editing "Паук"
    await waitFor(() => {
      expect(screen.getByText('Редактирование должности')).toBeInTheDocument();
      expect(screen.getByDisplayValue('Паук')).toBeInTheDocument();
    });
  });

  it('creates and saves a new job template successfully', async () => {
    vi.mocked(api.listJobTemplates).mockResolvedValue(mockTemplates);

    const createdTemplate: JobTemplate = {
      id: 'tpl-new-frontend',
      title: 'Frontend React Lead',
      role: 'Frontend',
      level: 'Lead',
      description: 'Архитектура и производительность',
      version: 1,
      is_archived: false,
      created_at: '2026-09-16T01:00:00Z',
      updated_at: '2026-09-16T01:00:00Z',
      questions: [
        {
          id: 'q-new-1',
          title: 'Вопрос по React',
          prompt: 'Архитектура React 19',
          weight: 1.0,
          criteria: [
            {
              id: 'c-new-1',
              title: 'Знание хуков',
              description: 'useActionState, Server Actions',
              min_score: 1.0,
              max_score: 5.0,
              weight: 1.0,
            },
          ],
        },
      ],
    };

    vi.mocked(api.createJobTemplate).mockResolvedValue(createdTemplate);

    render(<JobTemplatesScreen />);

    await waitFor(() => {
      expect(screen.getByText('Редактирование должности')).toBeInTheDocument();
    });

    // Click create
    fireEvent.click(screen.getByRole('button', { name: /Создать/i }));

    await waitFor(() => {
      expect(screen.getByText('Создание новой должности')).toBeInTheDocument();
    });

    // Change title and role
    const titleInput = screen.getByDisplayValue('Новая должность');
    fireEvent.change(titleInput, { target: { value: 'Frontend React Lead' } });

    const roleInput = screen.getByDisplayValue('Software Engineer');
    fireEvent.change(roleInput, { target: { value: 'Frontend' } });

    // When save is clicked, mock the second listJobTemplates call to include createdTemplate
    vi.mocked(api.listJobTemplates).mockResolvedValue([...mockTemplates, createdTemplate]);

    // Click "Создать должность"
    const saveBtn = screen.getByRole('button', { name: /Создать должность/i });
    fireEvent.click(saveBtn);

    // Verify createJobTemplate was called with the entered values
    await waitFor(() => {
      expect(api.createJobTemplate).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Frontend React Lead',
          role: 'Frontend',
        })
      );
    });

    // Should display success message and switch to editing mode for the new template
    await waitFor(() => {
      expect(screen.getByText('Должность успешно создана')).toBeInTheDocument();
      expect(screen.getByText('Редактирование должности')).toBeInTheDocument();
      expect(screen.getByDisplayValue('Frontend React Lead')).toBeInTheDocument();
    });
  });

  it('opens in-app delete modal when clicking trash icon and closes on "Отмена"', async () => {
    vi.mocked(api.listJobTemplates).mockResolvedValue(mockTemplates);

    render(<JobTemplatesScreen />);

    await waitFor(() => {
      expect(screen.getByText('Редактирование должности')).toBeInTheDocument();
    });

    // Click trash can button on card
    const deleteBtn = screen.getByTitle('Удалить должность');
    fireEvent.click(deleteBtn);

    // Confirmation modal must appear
    await waitFor(() => {
      expect(screen.getByText('Удаление должности')).toBeInTheDocument();
      expect(screen.getByText(/Вы уверены, что хотите удалить должность/i)).toBeInTheDocument();
    });

    // Click "Отмена" inside the modal
    const cancelBtn = screen.getByRole('button', { name: 'Отмена' });
    fireEvent.click(cancelBtn);

    // Modal must close and deleteJobTemplate was not called
    await waitFor(() => {
      expect(screen.queryByText('Удаление должности')).not.toBeInTheDocument();
    });
    expect(api.deleteJobTemplate).not.toHaveBeenCalled();
  });

  it('deletes job template when confirmed in modal', async () => {
    vi.mocked(api.listJobTemplates).mockResolvedValue(mockTemplates);
    vi.mocked(api.deleteJobTemplate).mockResolvedValue({ status: 'deleted', template_id: 'tpl-spider' });

    render(<JobTemplatesScreen />);

    await waitFor(() => {
      expect(screen.getByText('Редактирование должности')).toBeInTheDocument();
    });

    // Click trash button
    const deleteBtn = screen.getByTitle('Удалить должность');
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(screen.getByText('Удаление должности')).toBeInTheDocument();
    });

    // Mock next listJobTemplates call as empty
    vi.mocked(api.listJobTemplates).mockResolvedValue([]);

    // Click "Да, удалить"
    const confirmBtn = screen.getByRole('button', { name: /Да, удалить/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(api.deleteJobTemplate).toHaveBeenCalledWith('tpl-spider');
    });

    // Modal closes and switches to create new template since no templates remain
    await waitFor(() => {
      expect(screen.queryByText('Удаление должности')).not.toBeInTheDocument();
      expect(screen.getByText('Создание новой должности')).toBeInTheDocument();
    });
  });

  it('displays backend error and offers archiving when deletion fails due to interview references', async () => {
    vi.mocked(api.listJobTemplates).mockResolvedValue(mockTemplates);
    vi.mocked(api.deleteJobTemplate).mockRejectedValue(
      new Error('Нельзя удалить должность tpl-spider: на неё ссылаются существующие интервью (2). Используйте архивирование.')
    );
    vi.mocked(api.archiveJobTemplate).mockResolvedValue({ ...mockTemplates[0], is_archived: true });

    render(<JobTemplatesScreen />);

    await waitFor(() => {
      expect(screen.getByText('Редактирование должности')).toBeInTheDocument();
    });

    // Click trash button
    const deleteBtn = screen.getByTitle('Удалить должность');
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(screen.getByText('Удаление должности')).toBeInTheDocument();
    });

    // Click "Да, удалить"
    const confirmBtn = screen.getByRole('button', { name: /Да, удалить/i });
    fireEvent.click(confirmBtn);

    // Error message must be rendered inside modal
    await waitFor(() => {
      expect(
        screen.getByText(/на неё ссылаются существующие интервью/i)
      ).toBeInTheDocument();
    });

    // "Архивировать вместо удаления" button must appear
    const archiveBtn = screen.getByRole('button', { name: /Архивировать вместо удаления/i });
    expect(archiveBtn).toBeInTheDocument();

    // Click archive instead
    fireEvent.click(archiveBtn);

    await waitFor(() => {
      expect(api.archiveJobTemplate).toHaveBeenCalledWith('tpl-spider');
    });
  });
});
