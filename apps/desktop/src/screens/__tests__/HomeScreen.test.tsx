import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { HomeScreen } from '../HomeScreen';
import { listInterviews } from '../../services/api';
import type { PaginatedInterviews } from '../../types';

vi.mock('../../services/api', () => ({ listInterviews: vi.fn(), deleteInterview: vi.fn() }));

function result(candidate: string): PaginatedInterviews {
  return {
    total: 1, limit: 15, offset: 0,
    items: [{
      id: `id-${candidate}`, title: 'Техническое интервью', candidate_name: candidate,
      role: 'Backend Engineer', status: 'draft',
      created_at: '2026-09-30T10:00:00Z', updated_at: '2026-09-30T10:00:00Z',
    }],
  };
}

function deferred() {
  let resolve!: (response: PaginatedInterviews) => void;
  const promise = new Promise<PaginatedInterviews>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('HomeScreen filtering', () => {
  beforeEach(() => vi.clearAllMocks());

  it('preserves results and toolbar while refreshing and ignores older responses', async () => {
    const older = deferred();
    const newer = deferred();
    vi.mocked(listInterviews)
      .mockResolvedValueOnce(result('Исходный кандидат'))
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const { container } = render(
      <HomeScreen onOpenInterview={vi.fn()} onNewInterview={vi.fn()} hasActiveRecording={false} />
    );
    await screen.findByText('Исходный кандидат');
    fireEvent.click(screen.getByRole('button', { name: 'Фильтры' }));
    const toolbar = container.querySelector('.filter-toolbar');
    const originalClass = toolbar?.className;
    const trigger = screen.getByRole('combobox', { name: 'Статус' });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: 'Черновик' }));
    await waitFor(() => expect(listInterviews).toHaveBeenCalledTimes(2));
    expect(screen.getByText('Исходный кандидат')).toBeInTheDocument();
    expect(container.querySelector('.filter-toolbar')).toBe(toolbar);
    expect(toolbar?.className).toBe(originalClass);

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('option', { name: 'Готово к записи' }));
    await waitFor(() => expect(listInterviews).toHaveBeenCalledTimes(3));
    await act(async () => newer.resolve(result('Актуальный кандидат')));
    expect(screen.getByText('Актуальный кандидат')).toBeInTheDocument();
    await act(async () => older.resolve(result('Устаревший кандидат')));
    expect(screen.queryByText('Устаревший кандидат')).not.toBeInTheDocument();
    expect(screen.getByText('Актуальный кандидат')).toBeInTheDocument();
  });

  it('keeps the same toolbar when the result becomes empty', async () => {
    vi.mocked(listInterviews)
      .mockResolvedValueOnce(result('Исходный кандидат'))
      .mockResolvedValueOnce({ total: 0, items: [], limit: 15, offset: 0 });
    const { container } = render(
      <HomeScreen onOpenInterview={vi.fn()} onNewInterview={vi.fn()} hasActiveRecording={false} />
    );
    await screen.findByText('Исходный кандидат');
    const toolbar = container.querySelector('.filter-toolbar');
    const originalClass = toolbar?.className;
    fireEvent.change(screen.getByPlaceholderText('Поиск по собеседованиям'), { target: { value: 'Нет совпадений' } });
    await screen.findByText('Собеседования не найдены');
    expect(container.querySelector('.filter-toolbar')).toBe(toolbar);
    expect(toolbar?.className).toBe(originalClass);
    expect(container.querySelector('.interview-empty-icon')).toBeNull();
  });
});
