import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Pause, Play, Loader2, Square, WifiOff, Lightbulb, X, MicOff } from 'lucide-react';
import {
  AssessmentProposal,
  CaptureMode,
  InterviewPlan,
  QuestionMark,
  SpeakerRole,
  TranscriptSegment,
} from '../types';
import {
  addQuestionMark,
  enqueueJob,
  getActiveSession,
  getInterviewJobsStatus,
  getLiveState,
  pauseAudioCapture,
  pauseInterview,
  resumeAudioCapture,
  resumeInterview,
  setSegmentSpeakerRole,
} from '../services/api';
import { AudioMeters } from '../components/AudioMeters';
import { FollowUpSuggestions } from '../components/FollowUpSuggestions';
import { QuestionList, EvalStatus } from '../components/live/QuestionList';
import { QuestionFocus } from '../components/live/QuestionFocus';
import { LiveTranscript, LiveTranscriptHandle } from '../components/live/LiveTranscript';
import { StopSessionDialog } from '../components/live/StopSessionDialog';
import { useInterviewFinalizer } from '../components/live/useInterviewFinalizer';
import { describeSttError, formatClock, questionTitle, resolveRole } from '../components/live/liveUtils';
import '../styles/live.css';

interface QuestionEvalJobState {
  jobId: string;
  status: 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED';
  errorMessage?: string | null;
}

interface LiveSessionScreenProps {
  interviewId: string;
  plan: InterviewPlan;
  candidateName?: string;
  role?: string;
  captureMode?: CaptureMode;
  onFinishSession: (interviewId: string) => void;
  onPauseChange?: (isPaused: boolean) => void;
  /** Recording stopped but processing is not finished; the user leaves to come back later. */
  onLeaveProcessing?: (interviewId: string) => void;
  onOpenSettings?: () => void;
}

const LIVE_POLL_MS = 1000;
const JOBS_POLL_MS = 1200;
const CLOCK_SYNC_MS = 10000;
// After the interviewer moves on, the tail of the answer still needs to be transcribed.
// После перехода к следующему вопросу ждём распознавания конца ответа.
const AUTO_EVAL_DELAY_MS = 12000;

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

const segmentsSignature = (segs: TranscriptSegment[]) =>
  segs.map((s) => `${s.id}:${s.speaker_role ?? ''}:${s.text.length}`).join('|');

export const LiveSessionScreen: React.FC<LiveSessionScreenProps> = ({
  interviewId,
  plan,
  candidateName,
  role,
  captureMode,
  onFinishSession,
  onPauseChange,
  onLeaveProcessing,
  onOpenSettings,
}) => {
  const questions = plan.questions;

  // ------------------------------------------------------------------ Clock & pause
  const [isPaused, setIsPaused] = useState(false);
  const [isTogglingPause, setIsTogglingPause] = useState(false);
  const [, setClockTick] = useState(0);
  const clockRef = useRef({ baseMs: 0, syncedAt: performance.now() });
  const isPausedRef = useRef(false);
  // Bumped on every local pause/resume so a session read started earlier cannot undo it.
  const pauseVersionRef = useRef(0);

  const readElapsed = useCallback(() => {
    const { baseMs, syncedAt } = clockRef.current;
    return isPausedRef.current ? baseMs : baseMs + (performance.now() - syncedAt);
  }, []);

  const applyPaused = useCallback(
    (paused: boolean) => {
      clockRef.current = { baseMs: readElapsed(), syncedAt: performance.now() };
      isPausedRef.current = paused;
      setIsPaused(paused);
      onPauseChange?.(paused);
    },
    [readElapsed, onPauseChange]
  );

  // Capture clock is the source of truth for elapsed time (pauses excluded), same as segment timestamps.
  const syncClock = useCallback(async () => {
    const version = pauseVersionRef.current;
    const session = await getActiveSession().catch(() => null);
    if (session && session.session_id === interviewId && version === pauseVersionRef.current) {
      clockRef.current = { baseMs: session.elapsed_ms, syncedAt: performance.now() };
      if (session.is_paused !== isPausedRef.current) applyPaused(session.is_paused);
      return session.elapsed_ms;
    }
    return null;
  }, [interviewId, applyPaused]);

  useEffect(() => {
    void syncClock();
    const sync = setInterval(() => void syncClock(), CLOCK_SYNC_MS);
    const tick = setInterval(() => setClockTick((t) => t + 1), 500);
    return () => {
      clearInterval(sync);
      clearInterval(tick);
    };
  }, [syncClock]);

  const captureElapsed = useCallback(async () => (await syncClock()) ?? readElapsed(), [syncClock, readElapsed]);

  // ------------------------------------------------------------------ Live state polling
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [proposals, setProposals] = useState<AssessmentProposal[]>([]);
  const [marks, setMarks] = useState<QuestionMark[]>([]);
  const [segmentQuestions, setSegmentQuestions] = useState<Record<string, { question_id: string; is_ambiguous: boolean }>>({});
  const [suggested, setSuggested] = useState<{ question_id: string; segment_id: string } | null>(null);
  const [isLoaded, setIsLoaded] = useState(false);
  const [connectionLost, setConnectionLost] = useState(false);
  const [transcriptionIssue, setTranscriptionIssue] = useState<{ failed_jobs: number; error: string | null } | null>(null);
  const failuresRef = useRef(0);
  const inFlightRef = useRef(false);
  // Bumped on every local question mark so a poll started earlier cannot roll the question back.
  const marksVersionRef = useRef(0);
  const signaturesRef = useRef({ segments: '', proposals: '', links: '', marks: '' });

  const refreshLiveState = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    const marksVersion = marksVersionRef.current;
    try {
      const state = await getLiveState(interviewId);
      const marksFresh = marksVersion === marksVersionRef.current;
      failuresRef.current = 0;
      setConnectionLost(false);
      const sig = signaturesRef.current;

      const segSig = segmentsSignature(state.transcript_segments);
      if (segSig !== sig.segments) {
        sig.segments = segSig;
        setSegments(state.transcript_segments);
      }
      const propSig = state.assessment_proposals.map((p) => `${p.id}:${p.created_at}`).join('|');
      if (propSig !== sig.proposals) {
        sig.proposals = propSig;
        setProposals(state.assessment_proposals);
      }
      if (marksFresh) {
        const linkSig = JSON.stringify(state.segment_questions);
        if (linkSig !== sig.links) {
          sig.links = linkSig;
          setSegmentQuestions(state.segment_questions);
        }
        const markSig = state.question_marks.map((m) => m.id).join('|');
        if (markSig !== sig.marks) {
          sig.marks = markSig;
          setMarks(state.question_marks);
        }
      }
      setSuggested((prev) =>
        prev?.segment_id === state.suggested_question?.segment_id && prev?.question_id === state.suggested_question?.question_id
          ? prev
          : state.suggested_question
      );
      setTranscriptionIssue((prev) =>
        prev?.failed_jobs === state.transcription_issue?.failed_jobs && prev?.error === state.transcription_issue?.error
          ? prev
          : state.transcription_issue ?? null
      );
      setIsLoaded(true);
    } catch {
      failuresRef.current += 1;
      if (failuresRef.current >= 3) setConnectionLost(true);
    } finally {
      inFlightRef.current = false;
    }
  }, [interviewId]);

  useEffect(() => {
    void refreshLiveState();
    const poll = setInterval(() => void refreshLiveState(), LIVE_POLL_MS);
    return () => clearInterval(poll);
  }, [refreshLiveState]);

  // ------------------------------------------------------------------ Questions
  const currentQuestionId = marks.length > 0 ? marks[marks.length - 1].question_id : questions[0]?.id ?? null;
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [isMarking, setIsMarking] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [dismissedHint, setDismissedHint] = useState<string | null>(null);

  const firstMarkMs = useMemo(() => {
    const map: Record<string, number> = {};
    for (const m of marks) if (map[m.question_id] === undefined) map[m.question_id] = m.at_ms;
    return map;
  }, [marks]);

  const proposalByQuestion = useMemo(() => {
    const map: Record<string, AssessmentProposal | undefined> = {};
    for (const p of proposals) {
      const prev = map[p.question_id];
      if (!prev || p.created_at > prev.created_at) map[p.question_id] = p;
    }
    return map;
  }, [proposals]);

  const segmentsFor = useCallback(
    (qid: string) => segments.filter((s) => segmentQuestions[s.id]?.question_id === qid),
    [segments, segmentQuestions]
  );

  const currentIndex = questions.findIndex((q) => q.id === currentQuestionId);
  const nextQuestion = useMemo(() => {
    const unasked = (q: { id: string }) => firstMarkMs[q.id] === undefined && q.id !== currentQuestionId;
    return questions.slice(currentIndex + 1).find(unasked) ?? questions.find(unasked) ?? null;
  }, [questions, currentIndex, firstMarkMs, currentQuestionId]);

  const focusedQuestionId = focusedId && focusedId !== currentQuestionId ? focusedId : currentQuestionId;
  const focusedIndex = questions.findIndex((q) => q.id === focusedQuestionId);
  const focusedQuestion = focusedIndex >= 0 ? questions[focusedIndex] : undefined;

  // The interview always has a current question: the first one is marked at the start.
  const autoMarkedRef = useRef(false);
  useEffect(() => {
    if (!isLoaded || autoMarkedRef.current || marks.length > 0 || !questions[0]) return;
    autoMarkedRef.current = true;
    addQuestionMark(interviewId, questions[0].id, 0)
      .then((res) => {
        marksVersionRef.current += 1;
        setMarks(res.question_marks);
      })
      .catch((e) => console.warn('Could not mark the first question:', e));
  }, [isLoaded, marks.length, questions, interviewId]);

  // ------------------------------------------------------------------ Evaluation
  const [evalJobs, setEvalJobs] = useState<Record<string, QuestionEvalJobState>>({});
  const [scheduledEvals, setScheduledEvals] = useState<Record<string, boolean>>({});
  const evalTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const segmentsForRef = useRef(segmentsFor);
  segmentsForRef.current = segmentsFor;
  const evalJobsRef = useRef(evalJobs);
  evalJobsRef.current = evalJobs;

  useEffect(() => {
    let active = true;
    getInterviewJobsStatus(interviewId, { limit: 100 })
      .then((res) => {
        if (!active || !res.active_jobs) return;
        const restored: Record<string, QuestionEvalJobState> = {};
        for (const j of res.active_jobs) {
          if (j.type === 'EVALUATE_QUESTION' && j.question_id && (j.status === 'PENDING' || j.status === 'PROCESSING')) {
            restored[j.question_id] = { jobId: j.id, status: j.status, errorMessage: j.error_message };
          }
        }
        if (Object.keys(restored).length > 0) setEvalJobs((prev) => ({ ...restored, ...prev }));
      })
      .catch((err) => console.warn('Could not restore evaluation jobs:', err));
    return () => {
      active = false;
    };
  }, [interviewId]);

  const activeJobIds = Object.values(evalJobs)
    .filter((j) => j.status === 'PENDING' || j.status === 'PROCESSING')
    .map((j) => j.jobId)
    .join(',');

  useEffect(() => {
    if (!activeJobIds) return;
    let active = true;
    const timer = setInterval(async () => {
      try {
        const res = await getInterviewJobsStatus(interviewId, { jobIds: activeJobIds.split(',') });
        if (!active) return;
        const updated: Record<string, QuestionEvalJobState> = {};
        let completed = false;
        for (const job of res.active_jobs || []) {
          if (!job.question_id) continue;
          updated[job.question_id] = { jobId: job.id, status: job.status as QuestionEvalJobState['status'], errorMessage: job.error_message };
          if (job.status === 'COMPLETED') completed = true;
        }
        if (Object.keys(updated).length > 0) setEvalJobs((prev) => ({ ...prev, ...updated }));
        if (completed) void refreshLiveState();
      } catch (err) {
        console.warn('Evaluation jobs poll error:', err);
      }
    }, JOBS_POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [activeJobIds, interviewId, refreshLiveState]);

  const evaluateQuestion = useCallback(
    async (qid: string, auto = false) => {
      const running = evalJobsRef.current[qid];
      if (running && (running.status === 'PENDING' || running.status === 'PROCESSING')) return;
      const question = questions.find((q) => q.id === qid);
      if (!question) return;

      const qSegments = segmentsForRef.current(qid);
      const hasCandidate = qSegments.some((s) => resolveRole(s) === 'candidate');
      if (!hasCandidate) {
        if (auto) return;
        const hasUnknown = qSegments.some((s) => resolveRole(s) === 'unknown');
        setEvalJobs((prev) => ({
          ...prev,
          [qid]: {
            jobId: prev[qid]?.jobId || 'no-answer',
            status: 'FAILED',
            errorMessage: hasUnknown
              ? 'отметьте в стенограмме, какие реплики принадлежат кандидату'
              : 'в стенограмме пока нет ответа кандидата на этот вопрос',
          },
        }));
        return;
      }

      try {
        const res = await enqueueJob(interviewId, 'EVALUATE_QUESTION', {
          question_id: qid,
          rubric_description: question.criteria.map((c) => c.title).join(', '),
        });
        setEvalJobs((prev) => ({ ...prev, [qid]: { jobId: res.job_id, status: 'PENDING' } }));
      } catch (e) {
        setEvalJobs((prev) => ({
          ...prev,
          [qid]: { jobId: prev[qid]?.jobId || 'failed-job', status: 'FAILED', errorMessage: errorText(e) },
        }));
      }
    },
    [interviewId, questions]
  );

  const scheduleEvaluation = useCallback(
    (qid: string) => {
      clearTimeout(evalTimersRef.current[qid]);
      setScheduledEvals((prev) => ({ ...prev, [qid]: true }));
      evalTimersRef.current[qid] = setTimeout(() => {
        delete evalTimersRef.current[qid];
        setScheduledEvals((prev) => {
          const next = { ...prev };
          delete next[qid];
          return next;
        });
        void evaluateQuestion(qid, true);
      }, AUTO_EVAL_DELAY_MS);
    },
    [evaluateQuestion]
  );

  const cancelScheduledEvaluations = useCallback(() => {
    Object.values(evalTimersRef.current).forEach(clearTimeout);
    evalTimersRef.current = {};
    setScheduledEvals({});
  }, []);

  useEffect(() => cancelScheduledEvaluations, [cancelScheduledEvaluations]);

  const evalStatus = useMemo(() => {
    const map: Record<string, EvalStatus> = {};
    for (const q of questions) {
      const job = evalJobs[q.id];
      if (scheduledEvals[q.id]) map[q.id] = 'scheduled';
      else if (job?.status === 'PENDING' || job?.status === 'PROCESSING') map[q.id] = 'running';
      else if (job?.status === 'FAILED') map[q.id] = 'failed';
      else map[q.id] = 'idle';
    }
    return map;
  }, [questions, evalJobs, scheduledEvals]);

  // ------------------------------------------------------------------ Question marking
  const markQuestion = useCallback(
    async (qid: string, atMs?: number) => {
      if (isMarking) return;
      if (qid === currentQuestionId) {
        setFocusedId(null);
        return;
      }
      setIsMarking(true);
      setActionError(null);
      const previous = currentQuestionId;
      try {
        const at = atMs ?? (await captureElapsed());
        const res = await addQuestionMark(interviewId, qid, at);
        marksVersionRef.current += 1;
        signaturesRef.current.marks = res.question_marks.map((m) => m.id).join('|');
        setMarks(res.question_marks);
        setFocusedId(null);
        if (previous && previous !== qid) scheduleEvaluation(previous);
        // Re-evaluating a returned-to question happens when the interviewer leaves it again.
        clearTimeout(evalTimersRef.current[qid]);
        void refreshLiveState();
      } catch (e) {
        setActionError(`Не удалось переключить вопрос: ${errorText(e)}`);
      } finally {
        setIsMarking(false);
      }
    },
    [isMarking, currentQuestionId, captureElapsed, interviewId, scheduleEvaluation, refreshLiveState]
  );

  // ------------------------------------------------------------------ Transcript interactions
  const transcriptRef = useRef<LiveTranscriptHandle>(null);
  const [highlightId, setHighlightId] = useState<string | null>(null);
  const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [updatingRoleId, setUpdatingRoleId] = useState<string | null>(null);

  useEffect(() => () => clearTimeout(highlightTimerRef.current), []);

  const locateSegment = useCallback((segmentId: string) => {
    transcriptRef.current?.scrollToSegment(segmentId);
    setHighlightId(segmentId);
    clearTimeout(highlightTimerRef.current);
    highlightTimerRef.current = setTimeout(() => setHighlightId(null), 2600);
  }, []);

  const handleSelectQuestion = (qid: string) => {
    setFocusedId(qid === currentQuestionId ? null : qid);
    if (qid !== currentQuestionId && segmentsFor(qid).length > 0) {
      transcriptRef.current?.scrollToQuestion(qid);
    }
  };

  const handleSetRole = async (segId: string, newRole: SpeakerRole) => {
    setUpdatingRoleId(segId);
    setActionError(null);
    setSegments((prev) => prev.map((s) => (s.id === segId ? { ...s, speaker_role: newRole } : s)));
    try {
      await setSegmentSpeakerRole(interviewId, segId, newRole);
    } catch (e) {
      setActionError(`Не удалось изменить говорящего: ${errorText(e)}`);
      signaturesRef.current.segments = '';
      void refreshLiveState();
    } finally {
      setUpdatingRoleId(null);
    }
  };

  // ------------------------------------------------------------------ Pause
  const handleTogglePause = async () => {
    if (isTogglingPause) return;
    setIsTogglingPause(true);
    setActionError(null);
    pauseVersionRef.current += 1;
    try {
      if (!isPaused) {
        await pauseAudioCapture();
        await pauseInterview(interviewId);
        applyPaused(true);
      } else {
        await resumeAudioCapture();
        await resumeInterview(interviewId);
        applyPaused(false);
      }
      pauseVersionRef.current += 1;
      void syncClock();
    } catch (e) {
      setActionError(`Не удалось ${isPaused ? 'продолжить запись' : 'поставить запись на паузу'}: ${errorText(e)}`);
    } finally {
      setIsTogglingPause(false);
    }
  };

  // ------------------------------------------------------------------ Stop
  const [stopPhase, setStopPhase] = useState<'idle' | 'confirm' | 'progress'>('idle');
  const finalizer = useInterviewFinalizer({ interviewId, onFinished: onFinishSession });
  const isStopping = stopPhase === 'progress';

  const startStop = (options?: { acceptIncomplete?: boolean }) => {
    setStopPhase('progress');
    cancelScheduledEvaluations();
    void finalizer.run(options);
  };

  const cancelStop = () => {
    if (finalizer.captureStopped) return;
    finalizer.reset();
    setStopPhase('idle');
  };

  const leaveProcessing = () => {
    finalizer.cancel();
    onLeaveProcessing?.(interviewId);
  };

  // ------------------------------------------------------------------ Render
  const hint =
    suggested && suggested.segment_id !== dismissedHint && suggested.question_id !== currentQuestionId
      ? suggested
      : null;
  const hintIndex = hint ? questions.findIndex((q) => q.id === hint.question_id) : -1;
  const hintSegment = hint ? segments.find((s) => s.id === hint.segment_id) : undefined;

  const currentQuestion = questions.find((q) => q.id === currentQuestionId);
  const currentSegments = currentQuestionId ? segmentsFor(currentQuestionId) : [];
  const evaluatedCount = Object.values(proposalByQuestion).filter(Boolean).length;
  const elapsedMs = readElapsed();

  return (
    <div className="live">
      <header className="live-bar">
        <div className={`live-clock${isPaused ? ' live-clock-paused' : ''}`}>
          <span className={`live-rec-dot${isPaused ? '' : ' recording-pulse'}`} aria-hidden="true" />
          <span>{formatClock(elapsedMs)}</span>
          <span className="live-clock-state">{isPaused ? 'Пауза' : 'Запись'}</span>
        </div>

        <div className="live-identity">
          <span className="live-identity-name">{candidateName || plan.candidate_name || 'Кандидат'}</span>
          <span className="live-identity-role">{role || plan.role || 'Должность не указана'}</span>
        </div>

        <AudioMeters isCapturing={!isStopping} captureMode={captureMode} />

        <div className="live-bar-actions">
          <button type="button" className="btn btn-secondary" onClick={handleTogglePause} disabled={isTogglingPause || isStopping}>
            {isTogglingPause ? <Loader2 className="spin" /> : isPaused ? <Play /> : <Pause />}
            <span>{isPaused ? 'Продолжить' : 'Пауза'}</span>
          </button>
          <button type="button" className="btn btn-danger" onClick={() => setStopPhase('confirm')} disabled={isStopping || isTogglingPause}>
            <Square />
            <span>Завершить</span>
          </button>
        </div>
      </header>

      {transcriptionIssue && !connectionLost && (
        <div className="live-banner" role="alert">
          <MicOff />
          <span>
            Речь не распознаётся: {describeSttError(transcriptionIssue.error)}. Запись продолжается — стенограмму можно будет
            восстановить после исправления настроек.
          </span>
          {onOpenSettings && (
            <button type="button" className="btn btn-secondary btn-sm live-banner-action" onClick={onOpenSettings}>
              Открыть настройки
            </button>
          )}
        </div>
      )}

      {connectionLost && (
        <div className="live-banner" role="alert">
          <WifiOff />
          <span>Нет связи с сервисом обработки. Запись продолжается, текст появится после восстановления связи.</span>
        </div>
      )}

      <div className="live-grid">
        <QuestionList
          questions={questions}
          currentQuestionId={currentQuestionId}
          focusedQuestionId={focusedQuestionId}
          firstMarkMs={firstMarkMs}
          proposals={proposalByQuestion}
          evalStatus={evalStatus}
          nextQuestion={nextQuestion}
          isMarking={isMarking}
          disabled={isStopping}
          onSelect={handleSelectQuestion}
          onNext={() => nextQuestion && void markQuestion(nextQuestion.id)}
          onFinish={() => setStopPhase('confirm')}
        />

        <main className="live-pane" aria-label="Текущий вопрос и стенограмма">
          {focusedQuestion && (
            <QuestionFocus
              key={focusedQuestion.id}
              question={focusedQuestion}
              index={focusedIndex}
              isCurrent={focusedQuestion.id === currentQuestionId}
              firstMarkMs={firstMarkMs[focusedQuestion.id]}
              proposal={proposalByQuestion[focusedQuestion.id]}
              evalStatus={evalStatus[focusedQuestion.id]}
              evalError={evalJobs[focusedQuestion.id]?.errorMessage}
              isMarking={isMarking}
              disabled={isStopping}
              onMarkAsCurrent={() => void markQuestion(focusedQuestion.id)}
              onBackToCurrent={() => setFocusedId(null)}
              onEvaluate={() => void evaluateQuestion(focusedQuestion.id)}
              onLocateSegment={locateSegment}
            />
          )}

          {hint && hintIndex >= 0 && (
            <div className="notice notice-info live-notice" role="status">
              <Lightbulb />
              <div className="notice-body">
                Похоже, вы задаёте вопрос {hintIndex + 1}: <strong>{questionTitle(questions[hintIndex])}</strong>
                <div className="notice-actions">
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    disabled={isMarking || isStopping}
                    onClick={() => void markQuestion(hint.question_id, hintSegment?.start_time_ms)}
                  >
                    Да, перейти к нему
                  </button>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => setDismissedHint(hint.segment_id)}>
                    Нет
                  </button>
                </div>
              </div>
            </div>
          )}

          {actionError && (
            <div className="notice notice-danger live-notice" role="alert">
              <AlertTriangle />
              <div className="notice-body">{actionError}</div>
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setActionError(null)} aria-label="Скрыть">
                <X />
              </button>
            </div>
          )}

          <LiveTranscript
            ref={transcriptRef}
            segments={segments}
            segmentQuestions={segmentQuestions}
            questions={questions}
            currentQuestionId={currentQuestionId}
            highlightedSegmentId={highlightId}
            updatingRoleId={updatingRoleId}
            isPaused={isPaused}
            onSetRole={handleSetRole}
          />
        </main>

        <aside className="live-pane live-assist">
          <div className="live-pane-scroll">
            {currentQuestionId && (
              <FollowUpSuggestions
                interviewId={interviewId}
                questionId={currentQuestionId}
                questionTitle={questionTitle(currentQuestion)}
                segments={currentSegments}
                onLocateSegment={locateSegment}
                isPaused={isPaused}
                disabled={isStopping}
              />
            )}
          </div>
        </aside>
      </div>

      {stopPhase !== 'idle' && (
        <StopSessionDialog
          phase={stopPhase === 'confirm' ? 'confirm' : 'progress'}
          elapsedMs={elapsedMs}
          askedCount={Object.keys(firstMarkMs).length}
          totalQuestions={questions.length}
          evaluatedCount={evaluatedCount}
          steps={finalizer.steps}
          error={finalizer.error}
          captureStopped={finalizer.captureStopped}
          canAcceptIncomplete={finalizer.canAcceptIncomplete}
          waitingLong={finalizer.waitingLong}
          onCancel={cancelStop}
          onConfirm={() => startStop()}
          onRetry={() => startStop()}
          onAcceptIncomplete={() => startStop({ acceptIncomplete: true })}
          onLeave={leaveProcessing}
        />
      )}
    </div>
  );
};
