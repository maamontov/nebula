import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { ReviewScreen } from '../ReviewScreen';
import * as api from '../../services/api';

vi.mock('../../services/api', () => ({
  getInterview: vi.fn(),
  getTranscriptRevisions: vi.fn(),
  getSummary: vi.fn(),
  getInterviewJobsStatus: vi.fn(),
  startBatchRetranscribe: vi.fn(),
  getTranscriptDiff: vi.fn(),
  reviewAssessment: vi.fn(),
  getReportRevisions: vi.fn(),
  enqueueJob: vi.fn(),
}));

const criterion = (id: string) => ({ id, title: id, description: '', min_score: 1, max_score: 5, weight: 1 });
const plan = {
  id: 'plan',
  title: 'Python',
  role: 'Junior Python',
  questions: [
    { id: 'q1', title: 'Типы', text: 'Типы данных', weight: 1, criteria: [criterion('c1')] },
    { id: 'q2', title: 'Функции', text: 'Функции и аргументы', weight: 1, criteria: [criterion('c2')] },
  ],
};

const interviewData = (rev: string) => ({
  interview: { id: 'inv-1', status: 'review', active_transcript_revision_id: rev, role: 'Junior Python' },
  // Only q2 has an AI proposal: q1's evaluation failed.
  assessment_proposals: [
    {
      id: 'p2', interview_id: 'inv-1', question_id: 'q2', model_profile_id: 'm', created_at: '2026-09-28T10:00:00Z',
      scores: [{ criterion_id: 'c2', score: 4, explanation: '', evidence: [] }], critical_errors: [], is_approved: false,
    },
  ],
  human_assessments: [],
  transcript_segments: [
    { id: 's1', track_id: 'candidate', speaker_role: 'candidate', text: 'Ответ', start_time_ms: 0, end_time_ms: 1000, is_final: true },
  ],
});

const openQuestionsTab = async () => {
  render(<ReviewScreen interviewId="inv-1" plan={plan as any} onNewInterview={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: /Вопросы и ответы/ }));
};

describe('ReviewScreen evaluation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getInterview).mockResolvedValue(interviewData('trans-rev-8') as any);
    vi.mocked(api.getTranscriptRevisions).mockResolvedValue({ active_revision_id: 'trans-rev-8', revisions: [] } as any);
    vi.mocked(api.getSummary).mockResolvedValue({ has_summary: false } as any);
    vi.mocked(api.getReportRevisions).mockResolvedValue([] as any);
    vi.mocked(api.reviewAssessment).mockResolvedValue({ status: 'confirmed' } as any);
  });

  it('shows which question failed and why, and retries only that question', async () => {
    vi.mocked(api.enqueueJob).mockImplementation(async (_i, _t, payload: any) => ({ status: 'enqueued', job_id: `job-${payload.question_id}` }));
    vi.mocked(api.getInterviewJobsStatus).mockImplementation(async (_id, opts?: any) => {
      if (!opts?.jobIds) return { active_jobs: [] } as any;
      return {
        active_jobs: [
          { id: 'job-q1', type: 'EVALUATE_QUESTION', status: 'FAILED', question_id: 'q1', error_message: '1 validation error for AssessmentProposal\ncritical_errors.0' },
          { id: 'job-q2', type: 'EVALUATE_QUESTION', status: 'COMPLETED', question_id: 'q2' },
        ],
      } as any;
    });

    await openQuestionsTab();
    fireEvent.click(screen.getByRole('button', { name: /Автооценка всех ответов/ }));

    expect(await screen.findByText(/Вопрос №1 — модель вернула ответ в неожиданном формате/, {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Автооценка всех ответов/ })).not.toBeDisabled();

    vi.mocked(api.enqueueJob).mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    await waitFor(() => expect(api.enqueueJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.enqueueJob).mock.calls[0][2]).toMatchObject({ question_id: 'q1' });
  });

  it('"confirm all" never invents a score for a question without one', async () => {
    vi.mocked(api.getInterviewJobsStatus).mockResolvedValue({ active_jobs: [] } as any);
    await openQuestionsTab();

    fireEvent.click(screen.getByRole('button', { name: /Подтвердить все оценки/ }));

    await waitFor(() => expect(api.reviewAssessment).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.reviewAssessment).mock.calls[0][1]).toBe('q2');
    expect(vi.mocked(api.reviewAssessment).mock.calls[0][2]).toMatchObject({ expected_transcript_revision: 'trans-rev-8' });
    expect(await screen.findByText(/Без оценки остались вопросы № 1/)).toBeInTheDocument();
  });

  it('excluding a question survives a transcript revision change', async () => {
    vi.mocked(api.getInterviewJobsStatus).mockResolvedValue({ active_jobs: [] } as any);
    await openQuestionsTab();
    // Transcript edited elsewhere: the backend now has a newer revision.
    vi.mocked(api.getInterview).mockResolvedValue(interviewData('trans-rev-9') as any);
    vi.mocked(api.reviewAssessment)
      .mockRejectedValueOnce(new Error("409: Revision conflict: active transcript revision is 'trans-rev-9'"))
      .mockResolvedValue({ status: 'confirmed' } as any);

    fireEvent.click(screen.getAllByRole('button', { name: /Исключить вопрос/ })[0]);
    fireEvent.change(screen.getByPlaceholderText(/Опишите причину исключения/), { target: { value: 'Не задавался' } });
    const dialogButtons = screen.getAllByRole('button', { name: /Исключить вопрос/ });
    fireEvent.click(dialogButtons[dialogButtons.length - 1]);

    await waitFor(() => expect(api.reviewAssessment).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.reviewAssessment).mock.calls[1][2]).toMatchObject({
      expected_transcript_revision: 'trans-rev-9',
      is_excluded: true,
      exclusion_reason: 'Не задавался',
    });
  });

  it('a criterion score is the single place to change the question score and saves exactly what was chosen', async () => {
    vi.mocked(api.getInterviewJobsStatus).mockResolvedValue({ active_jobs: [] } as any);
    await openQuestionsTab();

    // q2 has an AI proposal of 4: the question score is shown read-only.
    const cards = await screen.findAllByText('Балл вопроса');
    const q2Card = cards[1].closest('.glass-panel') as HTMLElement;
    expect(cards[1].parentElement).toHaveTextContent('4 / 5');

    fireEvent.click(within(q2Card).getByRole('button', { name: '2' }));
    expect(within(q2Card).getByText('Есть несохранённые изменения')).toBeInTheDocument();
    expect(cards[1].parentElement).toHaveTextContent('2 / 5');

    fireEvent.click(within(q2Card).getByRole('button', { name: /Подтвердить оценку/ }));
    await waitFor(() => expect(api.reviewAssessment).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.reviewAssessment).mock.calls[0][1]).toBe('q2');
    expect(vi.mocked(api.reviewAssessment).mock.calls[0][2]).toMatchObject({
      scores: [{ criterion_id: 'c2', score: 2 }],
    });
  });

  it('confirm button stays disabled until every criterion has a score', async () => {
    vi.mocked(api.getInterviewJobsStatus).mockResolvedValue({ active_jobs: [] } as any);
    await openQuestionsTab();
    const q1Card = (await screen.findAllByText('Балл вопроса'))[0].closest('.glass-panel') as HTMLElement;
    expect(within(q1Card).getByText('не выставлен')).toBeInTheDocument();
    expect(within(q1Card).getByRole('button', { name: /Подтвердить оценку/ })).toBeDisabled();

    fireEvent.click(within(q1Card).getByRole('button', { name: '3' }));
    expect(within(q1Card).getByRole('button', { name: /Подтвердить оценку/ })).not.toBeDisabled();
  });
});
