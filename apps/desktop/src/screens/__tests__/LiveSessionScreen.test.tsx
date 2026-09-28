import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import { LiveSessionScreen } from '../LiveSessionScreen';
import * as api from '../../services/api';
import { InterviewPlan, LiveInterviewState } from '../../types';
import { formatClock, proposalAverage } from '../../components/live/liveUtils';
import { pluralRu } from '../../utils/plural';

vi.mock('../../services/api', () => ({
  addQuestionMark: vi.fn(),
  enqueueJob: vi.fn(),
  getActiveSession: vi.fn(),
  getInterviewJobsStatus: vi.fn(),
  getInterviewReadiness: vi.fn(),
  getLiveState: vi.fn(),
  getUploadProgress: vi.fn(),
  pauseAudioCapture: vi.fn(),
  pauseInterview: vi.fn(),
  resumeAudioCapture: vi.fn(),
  resumeInterview: vi.fn(),
  setSegmentSpeakerRole: vi.fn(),
  stopAudioCapture: vi.fn(),
  stopInterview: vi.fn(),
  updateInterviewStatus: vi.fn(),
  getAudioLevels: vi.fn(),
  getFollowUpsState: vi.fn(),
  generateFollowUps: vi.fn(),
  patchFollowUpSuggestion: vi.fn(),
  retryFollowUpRequest: vi.fn(),
  retryFailedTranscription: vi.fn(),
}));

const criterion = (id: string, title: string) => ({ id, title, description: '', min_score: 1, max_score: 5, weight: 1 });

const plan: InterviewPlan = {
  id: 'plan-1',
  title: 'Backend',
  role: 'Backend Middle',
  questions: [
    { id: 'q1', title: 'Индексы', prompt: 'Как устроены индексы?', weight: 1, criteria: [criterion('c1', 'B-tree')] },
    { id: 'q2', title: 'Транзакции', prompt: 'Уровни изоляции?', weight: 1, criteria: [criterion('c2', 'Изоляция')] },
    { id: 'q3', title: 'Очереди', prompt: 'Kafka?', weight: 1, criteria: [criterion('c3', 'Очереди')] },
  ],
};

const liveState = (overrides: Partial<LiveInterviewState> = {}): LiveInterviewState => ({
  interview_id: 'inv-1',
  status: 'recording',
  transcript_segments: [
    { id: 's1', track_id: 'interviewer', start_time_ms: 1000, end_time_ms: 3000, text: 'Как устроены индексы?', is_final: true, speaker_role: 'interviewer' },
    { id: 's2', track_id: 'candidate', start_time_ms: 4000, end_time_ms: 9000, text: 'B-tree хранит ключи упорядоченно', is_final: true, speaker_role: 'candidate' },
  ],
  assessment_proposals: [],
  question_marks: [{ id: 'm1', question_id: 'q1', at_ms: 0, created_at: '2026-09-28T10:00:00Z' }],
  current_question_id: 'q1',
  segment_questions: { s1: { question_id: 'q1', is_ambiguous: false }, s2: { question_id: 'q1', is_ambiguous: false } },
  suggested_question: null,
  ...overrides,
});

const renderScreen = (props: Partial<React.ComponentProps<typeof LiveSessionScreen>> = {}) =>
  render(
    <LiveSessionScreen
      interviewId="inv-1"
      plan={plan}
      candidateName="Анна"
      role="Backend Middle"
      onFinishSession={vi.fn()}
      {...props}
    />
  );

describe('LiveSessionScreen', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getLiveState).mockResolvedValue(liveState());
    vi.mocked(api.getActiveSession).mockResolvedValue({ is_recording: true, is_paused: false, session_id: 'inv-1', elapsed_ms: 65000, epoch: 1 });
    vi.mocked(api.getInterviewJobsStatus).mockResolvedValue({ active_jobs: [] } as any);
    vi.mocked(api.getAudioLevels).mockResolvedValue({ interviewer_rms: 0, interviewer_peak: 0, candidate_rms: 0, candidate_peak: 0 });
    vi.mocked(api.getFollowUpsState).mockResolvedValue({ suggestions: [], status: 'idle' } as any);
  });

  it('shows the current question and groups transcript by question', async () => {
    renderScreen();
    expect(await screen.findByText('B-tree хранит ключи упорядоченно')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Индексы' })).toBeInTheDocument();
    expect(screen.getByText('Вопрос 1 · Индексы')).toBeInTheDocument();
    expect(screen.getByText(/Дальше: Транзакции/)).toBeInTheDocument();
  });

  it('marks the next question on the capture timeline', async () => {
    const marks = [
      { id: 'm1', question_id: 'q1', at_ms: 0, created_at: '' },
      { id: 'm2', question_id: 'q2', at_ms: 65000, created_at: '' },
    ];
    vi.mocked(api.addQuestionMark).mockImplementation(async () => {
      vi.mocked(api.getLiveState).mockResolvedValue(liveState({ question_marks: marks, current_question_id: 'q2' }));
      return { mark: marks[1], question_marks: marks, created: true };
    });
    renderScreen();
    await screen.findByText('B-tree хранит ключи упорядоченно');

    fireEvent.click(screen.getByRole('button', { name: /Следующий вопрос/ }));

    await waitFor(() => expect(api.addQuestionMark).toHaveBeenCalledWith('inv-1', 'q2', 65000));
    expect(await screen.findByRole('heading', { name: 'Транзакции' })).toBeInTheDocument();
  });

  it('opening another question previews it without changing the current one', async () => {
    renderScreen();
    await screen.findByText('B-tree хранит ключи упорядоченно');

    fireEvent.click(screen.getByText('Очереди'));

    expect(await screen.findByRole('heading', { name: 'Очереди' })).toBeInTheDocument();
    expect(screen.getByText(/Просмотр · Вопрос 3/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Задаю этот вопрос/ })).toBeInTheDocument();
    expect(api.addQuestionMark).not.toHaveBeenCalled();
  });

  it('asks for confirmation before stopping the recording', async () => {
    renderScreen();
    await screen.findByText('B-tree хранит ключи упорядоченно');

    fireEvent.click(screen.getByRole('button', { name: 'Завершить' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Завершить интервью?');
    expect(api.stopAudioCapture).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Продолжить запись' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api.stopAudioCapture).not.toHaveBeenCalled();
  });

  it('reports pause to the parent so the app chrome shows the paused state', async () => {
    vi.mocked(api.pauseAudioCapture).mockImplementation(async () => {
      vi.mocked(api.getActiveSession).mockResolvedValue({ is_recording: true, is_paused: true, session_id: 'inv-1', elapsed_ms: 65000, epoch: 1 });
      return { status: 'paused', session_id: 'inv-1', elapsed_ms: 65000 };
    });
    vi.mocked(api.pauseInterview).mockResolvedValue({ status: 'paused' });
    const onPauseChange = vi.fn();
    renderScreen({ onPauseChange });
    await screen.findByText('B-tree хранит ключи упорядоченно');

    fireEvent.click(screen.getByRole('button', { name: 'Пауза' }));

    await waitFor(() => expect(onPauseChange).toHaveBeenCalledWith(true));
    expect(await screen.findByRole('button', { name: 'Продолжить' })).toBeInTheDocument();
  });

  it('asks who is speaking only for segments without a role', async () => {
    vi.mocked(api.getLiveState).mockResolvedValue(
      liveState({
        transcript_segments: [
          { id: 's9', track_id: 'shared', start_time_ms: 1000, end_time_ms: 2000, text: 'Угу', is_final: true, speaker_role: 'unknown' },
        ],
        segment_questions: { s9: { question_id: 'q1', is_ambiguous: true } },
      })
    );
    vi.mocked(api.setSegmentSpeakerRole).mockResolvedValue({} as any);
    renderScreen();
    expect(await screen.findByText('Кто говорит?')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Кандидат' }));
    await waitFor(() => expect(api.setSegmentSpeakerRole).toHaveBeenCalledWith('inv-1', 's9', 'candidate'));
  });
});

describe('LiveSessionScreen finishing', () => {
  const sttFailed = {
    is_ready: false,
    state: 'STT_FAILED',
    details: '2 audio transcription / assembly jobs failed.',
    stt_jobs: { completed: 21, pending: 0, failed: 2 },
    failed_error: "Missing STT API key: provider 'RouterAI' requires Bearer authentication.",
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getLiveState).mockResolvedValue(liveState());
    vi.mocked(api.getActiveSession).mockResolvedValue({ is_recording: true, is_paused: false, session_id: 'inv-1', elapsed_ms: 65000, epoch: 1 });
    vi.mocked(api.getInterviewJobsStatus).mockResolvedValue({ active_jobs: [] } as any);
    vi.mocked(api.getAudioLevels).mockResolvedValue({ interviewer_rms: 0, interviewer_peak: 0, candidate_rms: 0, candidate_peak: 0 });
    vi.mocked(api.getFollowUpsState).mockResolvedValue({ suggestions: [], status: 'idle' } as any);
    vi.mocked(api.stopAudioCapture).mockResolvedValue({ manifests: [] } as any);
    vi.mocked(api.stopInterview).mockResolvedValue({ status: 'processing' });
    vi.mocked(api.getUploadProgress).mockResolvedValue({ total_chunks: 21, uploaded_chunks: 21 } as any);
    vi.mocked(api.getInterviewReadiness).mockResolvedValue(sttFailed as any);
    vi.mocked(api.updateInterviewStatus).mockResolvedValue({ status: 'review' });
    vi.mocked(api.retryFailedTranscription).mockResolvedValue({ requeued_jobs: 2 });
  });

  const stopAndConfirm = async () => {
    await screen.findByText('B-tree хранит ключи упорядоченно');
    fireEvent.click(screen.getByRole('button', { name: 'Завершить' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: /Завершить/ }));
  };

  it('reports a recognition failure at once and lets the user open review anyway', async () => {
    const onFinishSession = vi.fn();
    renderScreen({ onFinishSession });
    await stopAndConfirm();

    expect(await screen.findByText(/не задан или неверен API-ключ распознавания речи/)).toBeInTheDocument();
    expect(api.getInterviewReadiness).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Открыть проверку сейчас' }));
    await waitFor(() =>
      expect(api.updateInterviewStatus).toHaveBeenCalledWith('inv-1', 'processing', 'review', undefined, { allowIncompleteTranscript: true })
    );
    expect(onFinishSession).toHaveBeenCalledWith('inv-1');
    expect(api.stopAudioCapture).toHaveBeenCalledTimes(1);
  });

  it('retry re-queues failed recognition instead of waiting in vain', async () => {
    renderScreen();
    await stopAndConfirm();
    await screen.findByText(/API-ключ распознавания речи/);

    vi.mocked(api.getInterviewReadiness).mockResolvedValue({ is_ready: true, state: 'READY_FOR_REVIEW', details: '' } as any);
    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }));

    await waitFor(() => expect(api.retryFailedTranscription).toHaveBeenCalledWith('inv-1'));
    await waitFor(() =>
      expect(api.updateInterviewStatus).toHaveBeenCalledWith('inv-1', 'processing', 'review', undefined, { allowIncompleteTranscript: false })
    );
    expect(api.stopAudioCapture).toHaveBeenCalledTimes(1);
    expect(api.stopInterview).toHaveBeenCalledTimes(1);
  });

  it('lets the user leave and come back later once the recording is stopped', async () => {
    const onLeaveProcessing = vi.fn();
    renderScreen({ onLeaveProcessing });
    await stopAndConfirm();
    await screen.findByText(/API-ключ распознавания речи/);

    fireEvent.click(screen.getByRole('button', { name: 'Закрыть, вернуться позже' }));
    expect(onLeaveProcessing).toHaveBeenCalledWith('inv-1');
  });

  it('warns during recording when speech is not being recognised', async () => {
    vi.mocked(api.getLiveState).mockResolvedValue(
      liveState({ transcription_issue: { failed_jobs: 3, error: 'Missing STT API key' } })
    );
    const onOpenSettings = vi.fn();
    renderScreen({ onOpenSettings });
    expect(await screen.findByText(/Речь не распознаётся/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Открыть настройки' }));
    expect(onOpenSettings).toHaveBeenCalled();
  });
});

describe('live helpers', () => {
  it('formats capture time', () => {
    expect(formatClock(0)).toBe('00:00');
    expect(formatClock(65_400)).toBe('01:05');
    expect(formatClock(3_725_000)).toBe('1:02:05');
  });

  it('pluralises Russian nouns', () => {
    expect(pluralRu(1, ['вопрос', 'вопроса', 'вопросов'])).toBe('вопрос');
    expect(pluralRu(3, ['вопрос', 'вопроса', 'вопросов'])).toBe('вопроса');
    expect(pluralRu(11, ['вопрос', 'вопроса', 'вопросов'])).toBe('вопросов');
    expect(pluralRu(22, ['вопрос', 'вопроса', 'вопросов'])).toBe('вопроса');
  });

  it('averages proposal scores on the criteria scale and ignores rejected proposals', () => {
    const q = { id: 'q', weight: 1, criteria: [criterion('a', 'A'), { ...criterion('b', 'B'), max_score: 10 }] };
    const base = { id: 'p', interview_id: 'i', question_id: 'q', model_profile_id: 'm', critical_errors: [], is_approved: false, created_at: '' };
    const proposal = {
      ...base,
      scores: [
        { criterion_id: 'a', score: 4, explanation: '', evidence: [] },
        { criterion_id: 'b', score: 6, explanation: '', evidence: [] },
      ],
    };
    expect(proposalAverage(proposal, q)).toEqual({ value: 3.5, max: 5 });
    expect(proposalAverage({ ...proposal, is_rejected: true }, q)).toBeNull();
  });
});
