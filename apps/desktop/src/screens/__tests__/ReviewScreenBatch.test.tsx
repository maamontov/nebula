import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
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
}));

describe('ReviewScreen Batch Retranscribe (R6)', () => {
  const mockPlan = {
    role: 'Backend Engineer',
    questions: [
      {
        id: 'q1',
        title: 'Архитектура сервисов',
        criteria: [{ id: 'c1', name: 'Масштабируемость' }],
      },
    ],
  };

  const baseInterviewData = {
    interview: {
      id: 'int-test-r6',
      status: 'completed',
      active_transcript_revision_id: 'trans-rev-1',
      role: 'Backend Engineer',
    },
    assessment_proposals: [],
    human_assessments: [],
    transcript_segments: [
      {
        id: 'seg-1',
        speaker_role: 'candidate',
        text: 'Мы использовали шардирование',
        start_time_sec: 10.0,
        end_time_sec: 20.0,
      },
    ],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getInterview).mockResolvedValue(baseInterviewData as any);
    vi.mocked(api.getTranscriptRevisions).mockResolvedValue({
      interview_id: 'int-test-r6',
      active_revision_id: 'trans-rev-1',
      revisions: [
        { revision_id: 'trans-rev-1', segment_count: 1, is_active: true },
      ],
    } as any);
    vi.mocked(api.getSummary).mockResolvedValue({ has_summary: false } as any);
    vi.mocked(api.getReportRevisions).mockResolvedValue([] as any);
    vi.mocked(api.getInterviewJobsStatus).mockResolvedValue({
      interview_id: 'int-test-r6',
      counts: {},
      pending_or_processing: 0,
      is_pipeline_idle: true,
      active_jobs: [],
    } as any);
  });

  it('restores active BATCH_RETRANSCRIBE job on mount and displays progress', async () => {
    vi.mocked(api.getInterviewJobsStatus).mockResolvedValueOnce({
      interview_id: 'int-test-r6',
      counts: {},
      pending_or_processing: 1,
      is_pipeline_idle: false,
      active_jobs: [
        {
          id: 'job-batch-restore-1',
          type: 'BATCH_RETRANSCRIBE',
          status: 'PROCESSING',
          attempts: 1,
          max_attempts: 3,
          elapsed_sec: 14.5,
          transcript_revision_id: 'trans-rev-2',
        },
      ],
    } as any);

    render(
      <ReviewScreen
        interviewId="int-test-r6"
        plan={mockPlan as any}
        onNewInterview={vi.fn()}
      />
    );

    // Switch to transcript tab
    await waitFor(() => {
      expect(screen.getByText(/Транскрипт/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Транскрипт/));

    // Verify progress banner is displayed with job ID
    await waitFor(() => {
      expect(screen.getByText('job-batch-restore-1')).toBeInTheDocument();
      expect(screen.getByText(/Пакетная перестенограмма выполняется/)).toBeInTheDocument();
    });
  });

  it('tracks queued batch job to completion and activates revision without premature success', async () => {
    vi.mocked(api.startBatchRetranscribe).mockResolvedValue({
      status: 'enqueued',
      job_id: 'job-batch-999',
      new_revision_id: 'trans-rev-2',
    });

    render(
      <ReviewScreen
        interviewId="int-test-r6"
        plan={mockPlan as any}
        onNewInterview={vi.fn()}
      />
    );

    // Switch to transcript tab
    await waitFor(() => {
      expect(screen.getByText(/Транскрипт/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText(/Транскрипт/));

    const retranscribeBtn = await screen.findByRole('button', { name: /Пакетная перестенограмма/ });
    expect(retranscribeBtn).toBeInTheDocument();

    // Click start
    await act(async () => {
      fireEvent.click(retranscribeBtn);
    });

    expect(api.startBatchRetranscribe).toHaveBeenCalledWith('int-test-r6', 'trans-rev-2', 'trans-rev-1');

    // Immediately shows banner and does NOT show premature success
    await waitFor(() => {
      expect(screen.getByText('job-batch-999')).toBeInTheDocument();
      expect(screen.queryByText(/Пакетная перестенограмма успешно завершена/)).toBeNull();
    });

    // Mock polling returns COMPLETED
    vi.mocked(api.getInterviewJobsStatus).mockResolvedValue({
      interview_id: 'int-test-r6',
      counts: {},
      pending_or_processing: 0,
      is_pipeline_idle: true,
      active_jobs: [
        {
          id: 'job-batch-999',
          type: 'BATCH_RETRANSCRIBE',
          status: 'COMPLETED',
          attempts: 1,
          max_attempts: 3,
          transcript_revision_id: 'trans-rev-2',
        },
      ],
    } as any);

    // After polling ticks, should show actual completion
    await waitFor(
      () => {
        expect(
          screen.getByText(/Пакетная перестенограмма успешно завершена! Активная ревизия переключена на: trans-rev-2/)
        ).toBeInTheDocument();
      },
      { timeout: 4000 }
    );
  });
});
