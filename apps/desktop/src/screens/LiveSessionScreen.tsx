import React, { useState, useEffect, useRef } from 'react';
import { InterviewPlan, TranscriptSegment, AssessmentProposal, TrackManifest, SpeakerRole } from '../types';
import {
  stopAudioCapture,
  pauseAudioCapture,
  resumeAudioCapture,
  pauseInterview,
  resumeInterview,
  stopInterview,
  getInterviewReadiness,
  getUploadProgress,
  updateInterviewStatus,
  getInterview,
  enqueueJob,
  setSegmentSpeakerRole,
} from '../services/api';
import { Square, Pause, Play, CheckCircle, MessageSquare, Quote, Sparkles, AlertTriangle, ExternalLink, Loader2, ArrowDown } from 'lucide-react';

interface LiveSessionScreenProps {
  interviewId: string;
  plan: InterviewPlan;
  onFinishSession: (interviewId: string) => void;
}

export const LiveSessionScreen: React.FC<LiveSessionScreenProps> = ({
  interviewId,
  plan,
  onFinishSession,
}) => {
  const [elapsedSec, setElapsedSec] = useState(0);
  const [isPaused, setIsPaused] = useState(false);
  const [isTogglingPause, setIsTogglingPause] = useState(false);
  const [activeQuestionIdx, setActiveQuestionIdx] = useState(0);
  const [selectedSegmentId, setSelectedSegmentId] = useState<string | null>(null);
  const [isEvaluating, setIsEvaluating] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const [stoppingStage, setStoppingStage] = useState<string | null>(null);
  const [evalSuccessNotice, setEvalSuccessNotice] = useState<string | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);

  // Real initial state: strictly empty, no synthetic speech or mock scores
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [proposals, setProposals] = useState<AssessmentProposal[]>([]);
  const [isUpdatingRole, setIsUpdatingRole] = useState<string | null>(null);
  const transcriptContainerRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef(true);
  const [showScrollBottomBtn, setShowScrollBottomBtn] = useState(false);

  const handleSetRole = async (segId: string, role: SpeakerRole) => {
    try {
      setIsUpdatingRole(segId);
      // Optimistic update for instant visual feedback
      setSegments((prev) =>
        prev.map((s) => (s.id === segId ? { ...s, speaker_role: role } : s))
      );
      await setSegmentSpeakerRole(interviewId, segId, role);
    } catch (err: any) {
      console.error('Failed to set speaker role in live transcript:', err);
    } finally {
      setIsUpdatingRole(null);
    }
  };

  const handleTranscriptScroll = () => {
    if (!transcriptContainerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = transcriptContainerRef.current;
    const isBottom = scrollHeight - scrollTop - clientHeight < 80;
    isNearBottomRef.current = isBottom;
    setShowScrollBottomBtn(!isBottom && segments.length > 0);
  };

  const scrollToBottom = (smooth = true) => {
    if (!transcriptContainerRef.current) return;
    transcriptContainerRef.current.scrollTo({
      top: transcriptContainerRef.current.scrollHeight,
      behavior: smooth ? 'smooth' : 'auto',
    });
    isNearBottomRef.current = true;
    setShowScrollBottomBtn(false);
  };

  // Monotonic timer
  useEffect(() => {
    if (isPaused) return;
    const timer = setInterval(() => setElapsedSec((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [isPaused]);

  // Polling for real backend updates with clean unmount
  useEffect(() => {
    let isSubscribed = true;
    const poll = setInterval(async () => {
      try {
        const data = await getInterview(interviewId);
        if (!isSubscribed) return;
        setSegments(data.transcript_segments || []);
        setProposals(data.assessment_proposals || []);
        setBackendError(null);
      } catch (err: any) {
        if (!isSubscribed) return;
        setBackendError('Связь с сервером бэкенда потеряна');
      }
    }, 800);

    return () => {
      isSubscribed = false;
      clearInterval(poll);
    };
  }, [interviewId]);

  // Auto-scroll when new segments arrive if user is near bottom
  useEffect(() => {
    if (isNearBottomRef.current && segments.length > 0) {
      scrollToBottom(true);
    }
  }, [segments]);

  const formatTime = (totalSeconds: number) => {
    const mins = Math.floor(totalSeconds / 60);
    const secs = totalSeconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  const handleTogglePause = async () => {
    if (isTogglingPause || isStopping) return;
    setIsTogglingPause(true);
    setBackendError(null);
    try {
      if (!isPaused) {
        // Pause audio capture engine
        await pauseAudioCapture();
        // Record pause state in backend
        await pauseInterview(interviewId);
        setIsPaused(true);
      } else {
        // Resume capture engine (advances epoch)
        await resumeAudioCapture();
        // Record resume in backend
        await resumeInterview(interviewId);
        setIsPaused(false);
      }
    } catch (err: any) {
      console.error('Failed to toggle pause:', err);
      setBackendError(`Ошибка переключения паузы: ${err.message || err}`);
    } finally {
      setIsTogglingPause(false);
    }
  };

  const handleStop = async () => {
    if (isStopping) return;
    setIsStopping(true);
    setBackendError(null);

    try {
      setStoppingStage('Остановка захвата звука...');
      const stopResult = await stopAudioCapture();

      setStoppingStage('Фиксация состояния и манифестов сессии...');
      let manifestsList: Array<TrackManifest | Record<string, unknown>> = [];
      if (Array.isArray(stopResult.manifests)) {
        manifestsList = stopResult.manifests;
      } else if (stopResult.manifests && typeof stopResult.manifests === 'object') {
        manifestsList = Object.values(stopResult.manifests);
      }
      await stopInterview(interviewId, manifestsList);

      setStoppingStage('Ожидание передачи аудио и завершения транскрибации...');
      let isReady = false;
      let attempts = 0;
      const maxAttempts = 120; // up to 60s
      while (!isReady && attempts < maxAttempts) {
        attempts++;
        try {
          const uploadProgress = await getUploadProgress(interviewId).catch(() => null);
          const readiness = await getInterviewReadiness(interviewId);
          if (readiness.is_ready) {
            isReady = true;
            break;
          } else {
            const incompleteCount = readiness.stt_jobs?.pending ?? 0;
            const missingChunksCount = readiness.missing_chunks
              ? Object.values(readiness.missing_chunks).reduce((acc, curr) => acc + curr.length, 0)
              : 0;
            const uploadInfo = uploadProgress
              ? ` (выгружено ${uploadProgress.uploaded_chunks}/${uploadProgress.total_chunks})`
              : missingChunksCount > 0
              ? ` (ожидание ${missingChunksCount} фрагментов)`
              : '';
            setStoppingStage(`Обработка аудио${uploadInfo}: ${readiness.details} (осталось задач: ${incompleteCount})...`);
          }
        } catch {
          // brief retry
        }
        await new Promise((r) => setTimeout(r, 500));
      }

      if (!isReady) {
        const finalReadiness = await getInterviewReadiness(interviewId).catch(() => null);
        const reasons = finalReadiness?.details || 'Превышено время ожидания готовности данных';
        throw new Error(`Данные интервью не полностью готовы к оценке: ${reasons}`);
      }

      setStoppingStage('Переход к ревью сессии...');
      await updateInterviewStatus(interviewId, 'processing', 'review');

      onFinishSession(interviewId);
    } catch (e: any) {
      console.error('Stop session failed:', e);
      setBackendError(`Не удалось завершить сессию: ${e.message || e}`);
      setIsStopping(false);
      setStoppingStage(null);
    }
  };

  const scrollToSegment = (segmentId: string) => {
    setSelectedSegmentId(segmentId);
    const elem = document.getElementById(`segment-${segmentId}`);
    if (elem) {
      elem.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  };

  const currentQ = plan.questions[activeQuestionIdx] || plan.questions[0];
  const currentProp = proposals.find((p) => p.question_id === currentQ?.id);

  const handleEvaluateCurrent = async () => {
    if (!currentQ) return;
    setIsEvaluating(true);
    setEvalSuccessNotice(null);

    try {
      // Find candidate speech segments (both dual-track and manually assigned candidate role)
      const candidateSegments = segments.filter(
        (s) => s.track_id === 'candidate' || s.speaker_role === 'candidate'
      );
      if (candidateSegments.length === 0) {
        setEvalSuccessNotice('Нет распознанных ответов кандидата для оценки.');
        setTimeout(() => setEvalSuccessNotice(null), 4000);
        return;
      }

      await enqueueJob(interviewId, 'EVALUATE_QUESTION', {
        question_id: currentQ.id,
        rubric_description: currentQ.criteria.map((c) => c.title).join(', '),
      });

      setEvalSuccessNotice('Задание на оценку отправлено в очередь!');
      setTimeout(() => setEvalSuccessNotice(null), 4000);
    } catch (e) {
      console.error('Failed to trigger evaluation:', e);
    } finally {
      setIsEvaluating(false);
    }
  };

  return (
    <div className="h-[calc(100vh-4rem)] flex flex-col bg-slate-950 overflow-hidden">
      {/* Backend connection banner */}
      {backendError && (
        <div className="px-6 py-2 bg-rose-950/80 border-b border-rose-800 text-rose-300 text-xs flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <AlertTriangle className="w-4 h-4 text-rose-400" />
            <span>{backendError}</span>
          </div>
          <span className="text-[11px] text-rose-400">Проверьте запуск бэкенда на localhost:8000</span>
        </div>
      )}

      {/* Top Session Control Bar */}
      <div className="px-6 py-3 bg-slate-900 border-b border-slate-800 flex items-center justify-between">
        <div className="flex items-center space-x-4">
          <div className="font-mono text-xl font-bold tracking-wider text-slate-100 flex items-center space-x-2">
            <span className="w-2.5 h-2.5 rounded-full bg-rose-500 recording-pulse" />
            <span>{formatTime(elapsedSec)}</span>
          </div>
          <span className="text-xs text-slate-400">Монотонный таймер сессии</span>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={handleTogglePause}
            disabled={isTogglingPause || isStopping}
            className="flex items-center space-x-1.5 px-3.5 py-1.5 bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-200 text-xs font-medium rounded-lg transition"
          >
            {isTogglingPause ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : isPaused ? (
              <Play className="w-3.5 h-3.5 fill-slate-200" />
            ) : (
              <Pause className="w-3.5 h-3.5" />
            )}
            <span>{isPaused ? 'Продолжить' : 'Пауза'}</span>
          </button>

          <button
            onClick={handleStop}
            disabled={isStopping || isTogglingPause}
            className="flex items-center space-x-1.5 px-4 py-1.5 bg-rose-600 hover:bg-rose-500 disabled:opacity-50 text-white text-xs font-semibold rounded-lg shadow-sm shadow-rose-600/30 transition cursor-pointer disabled:cursor-not-allowed"
          >
            {isStopping ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin text-white" />
            ) : (
              <Square className="w-3.5 h-3.5 fill-white" />
            )}
            <span>{stoppingStage || (isStopping ? 'Завершение...' : 'Завершить запись')}</span>
          </button>
        </div>
      </div>

      {/* Main Grid: Left Questions, Center Live Transcripts, Right Live AI Copilot */}
      <div className="flex-1 grid grid-cols-12 gap-0 min-h-0 overflow-hidden">
        {/* Column 1: Questions Plan Navigator (3 cols) */}
        <div className="col-span-3 border-r border-slate-800 bg-slate-950/70 p-4 space-y-3 overflow-y-auto">
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2">План вопросов</h3>
          {plan.questions.map((q, idx) => {
            const hasProposal = proposals.some((p) => p.question_id === q.id);
            const isActive = idx === activeQuestionIdx;
            return (
              <div
                key={q.id}
                onClick={() => setActiveQuestionIdx(idx)}
                className={`p-3 rounded-lg border cursor-pointer transition ${
                  isActive
                    ? 'bg-indigo-950/40 border-indigo-500/80 shadow-sm'
                    : 'bg-slate-900/40 border-slate-800/80 hover:bg-slate-900'
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className={`text-xs font-bold ${isActive ? 'text-indigo-400' : 'text-slate-400'}`}>
                    Вопрос #{idx + 1}
                  </span>
                  {hasProposal && (
                    <span className="flex items-center space-x-1 text-[11px] text-emerald-400 bg-emerald-950/50 px-1.5 py-0.5 rounded">
                      <CheckCircle className="w-3 h-3" />
                      <span>Оценён</span>
                    </span>
                  )}
                </div>
                <p className="text-xs text-slate-200 line-clamp-2">{q.text}</p>
              </div>
            );
          })}
        </div>

        {/* Column 2: Live Transcripts (5 cols) */}
        <div className="col-span-5 border-r border-slate-800 flex flex-col bg-slate-950/40 min-h-0 relative">
          <div className="p-3 border-b border-slate-800/80 bg-slate-900/50 flex items-center justify-between shrink-0">
            <div className="flex items-center space-x-2">
              <MessageSquare className="w-4 h-4 text-slate-400" />
              <span className="text-xs font-bold uppercase tracking-wider text-slate-300">Живая стенограмма</span>
            </div>
            <span className="text-[11px] text-slate-400">Whisper Large v3 Turbo (STT)</span>
          </div>

          <div
            ref={transcriptContainerRef}
            onScroll={handleTranscriptScroll}
            className="flex-1 min-h-0 p-4 space-y-4 overflow-y-auto"
          >
            {segments.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center text-slate-500 space-y-2 py-16">
                <MessageSquare className="w-8 h-8 text-slate-600 animate-pulse" />
                <p className="text-xs">Стенограмма пуста. Ожидание речи и распознавания...</p>
              </div>
            ) : (
              segments.map((s) => {
              const isCandidate = s.speaker_role === 'candidate' || (s.speaker_role !== 'interviewer' && s.track_id === 'candidate');
              const isInterviewer = s.speaker_role === 'interviewer' || (s.speaker_role !== 'candidate' && s.track_id === 'interviewer');
              const speakerLabel = isCandidate ? 'Кандидат' : isInterviewer ? 'Интервьюер' : 'Общий источник';
              const isHighlighted = selectedSegmentId === s.id;
              return (
                <div
                  key={s.id}
                  id={`segment-${s.id}`}
                  className={`flex flex-col transition-all duration-300 ${isCandidate ? 'items-end' : 'items-start'} ${
                    isHighlighted ? 'scale-[1.01]' : ''
                  }`}
                >
                  <div className="flex items-center space-x-2 mb-1">
                    <span className={`text-[11px] font-semibold ${isCandidate ? 'text-emerald-400' : isInterviewer ? 'text-indigo-400' : 'text-cyan-400'}`}>
                      {speakerLabel}
                    </span>
                    <span className="text-[10px] text-slate-500 font-mono">
                      {Math.round(s.start_time_ms / 1000)}s - {Math.round(s.end_time_ms / 1000)}s
                    </span>
                    {isHighlighted && (
                      <span className="text-[10px] text-amber-400 font-semibold bg-amber-950/60 px-1.5 py-0.5 rounded border border-amber-800">
                        Подтверждающий фрагмент
                      </span>
                    )}

                    {/* Quick Role Assignment Buttons */}
                    <div className="flex items-center space-x-1 ml-1.5">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleSetRole(s.id, isCandidate ? 'unknown' : 'candidate');
                        }}
                        disabled={isUpdatingRole === s.id}
                        className={`px-1.5 py-0.5 rounded text-[10px] font-medium transition cursor-pointer ${
                          isCandidate
                            ? 'bg-emerald-600/40 text-emerald-300 border border-emerald-500/60 shadow-sm'
                            : 'bg-slate-800/90 hover:bg-emerald-950/60 text-slate-400 hover:text-emerald-300 border border-slate-700/60'
                        }`}
                        title={isCandidate ? 'Снять роль кандидата' : 'Назначить репликой кандидата'}
                      >
                        Кандидат
                      </button>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleSetRole(s.id, isInterviewer ? 'unknown' : 'interviewer');
                        }}
                        disabled={isUpdatingRole === s.id}
                        className={`px-1.5 py-0.5 rounded text-[10px] font-medium transition cursor-pointer ${
                          isInterviewer
                            ? 'bg-indigo-600/40 text-indigo-300 border border-indigo-500/60 shadow-sm'
                            : 'bg-slate-800/90 hover:bg-indigo-950/60 text-slate-400 hover:text-indigo-300 border border-slate-700/60'
                        }`}
                        title={isInterviewer ? 'Снять роль интервьюера' : 'Назначить репликой интервьюера'}
                      >
                        Интервьюер
                      </button>
                    </div>
                  </div>
                  <div
                    className={`max-w-[85%] p-3 rounded-xl text-xs leading-relaxed transition-all ${
                      isHighlighted
                        ? 'ring-2 ring-amber-400 bg-amber-950/40 border border-amber-500 text-amber-100 shadow-lg shadow-amber-950/50'
                        : isCandidate
                        ? 'bg-emerald-950/30 border border-emerald-800/50 text-emerald-100 rounded-tr-none'
                        : isInterviewer
                        ? 'bg-indigo-950/30 border border-indigo-800/50 text-indigo-100 rounded-tl-none'
                        : 'bg-cyan-950/30 border border-cyan-800/50 text-cyan-100 rounded-tl-none'
                    }`}
                  >
                    {s.text}
                  </div>
                </div>
              );
            }))}
          </div>

          {/* Floating Scroll to Bottom Button */}
          {showScrollBottomBtn && (
            <button
              onClick={() => scrollToBottom(true)}
              className="absolute bottom-4 right-4 flex items-center space-x-1.5 px-3 py-1.5 bg-indigo-600/90 hover:bg-indigo-500 text-white text-xs font-semibold rounded-full shadow-lg shadow-indigo-950/80 border border-indigo-400/40 backdrop-blur transition-all duration-200 animate-in fade-in cursor-pointer"
            >
              <ArrowDown className="w-3.5 h-3.5" />
              <span>Вниз к новым</span>
            </button>
          )}
        </div>

        {/* Column 3: Live AI Copilot & Evidence (4 cols) */}
        <div className="col-span-4 p-4 space-y-4 overflow-y-auto bg-slate-950/80 flex flex-col justify-between">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <Sparkles className="w-4 h-4 text-indigo-400" />
                <span className="text-xs font-bold uppercase tracking-wider text-slate-200">Оперативная оценка</span>
              </div>
              <span className="text-[10px] text-slate-400 bg-slate-800/80 px-2 py-0.5 rounded">Gemini 3.8 Flash</span>
            </div>

            {/* Quick Action to evaluate current question */}
            <div className="p-3 bg-slate-900/80 border border-slate-800 rounded-xl space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-300">Оценить текущий ответ</span>
                {evalSuccessNotice && (
                  <span className="text-[10px] text-emerald-400">{evalSuccessNotice}</span>
                )}
              </div>
              <button
                onClick={handleEvaluateCurrent}
                disabled={isEvaluating}
                className="w-full flex items-center justify-center space-x-2 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:bg-indigo-950 disabled:text-slate-500 text-white text-xs font-semibold rounded-lg shadow-sm transition"
              >
                {isEvaluating ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    <span>Обработка оценки...</span>
                  </>
                ) : (
                  <>
                    <Sparkles className="w-3.5 h-3.5" />
                    <span>Запросить оценку вопроса #{activeQuestionIdx + 1}</span>
                  </>
                )}
              </button>
            </div>

            {currentProp ? (
              <div className="space-y-4">
                {currentProp.scores.map((score, sIdx) => (
                  <div key={sIdx} className="glass-panel p-4 rounded-xl space-y-3">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-bold text-slate-300">Оценка критерия</span>
                      <span className="px-2.5 py-0.5 text-xs font-bold rounded-full bg-indigo-900/60 text-indigo-300 border border-indigo-700/60">
                        {score.score} / 5.0
                      </span>
                    </div>

                    <p className="text-xs text-slate-300 leading-relaxed bg-slate-900/60 p-2.5 rounded-lg border border-slate-800">
                      {score.explanation}
                    </p>

                    {/* Verbatim Evidence Quotes with interactive Jump-to-Transcript */}
                    {score.evidence && score.evidence.length > 0 && (
                      <div className="space-y-2 pt-1">
                        <div className="flex items-center space-x-1.5 text-[11px] font-semibold text-slate-400">
                          <Quote className="w-3 h-3 text-indigo-400" />
                          <span>Подтверждающий текст (кликните для перехода):</span>
                        </div>
                        {score.evidence.map((ev, eIdx) => (
                          <div
                            key={eIdx}
                            onClick={() => scrollToSegment(ev.segment_id)}
                            className="group p-2.5 bg-slate-900/90 hover:bg-indigo-950/40 border-l-2 border-indigo-500 hover:border-amber-400 rounded cursor-pointer transition text-[11px] text-slate-200 flex flex-col space-y-1.5"
                          >
                            <div className="italic leading-relaxed">
                              «{ev.exact_quote}»
                            </div>
                            <div className="flex items-center space-x-1 text-[10px] text-indigo-400 group-hover:text-amber-300 font-semibold self-end">
                              <span>Показать в стенограмме</span>
                              <ExternalLink className="w-2.5 h-2.5" />
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}

                {currentProp.critical_errors.length > 0 && (
                  <div className="p-3 bg-rose-950/40 border border-rose-800/80 rounded-xl space-y-1">
                    <div className="flex items-center space-x-1.5 text-xs font-bold text-rose-300">
                      <AlertTriangle className="w-3.5 h-3.5" />
                      <span>Критическая ошибка</span>
                    </div>
                    {currentProp.critical_errors.map((err, idx) => (
                      <p key={idx} className="text-xs text-rose-200">
                        {err}
                      </p>
                    ))}
                  </div>
                )}

                {currentProp.is_rejected && (
                  <div className="p-3 bg-rose-950/60 border border-rose-800 rounded-xl space-y-1">
                    <div className="flex items-center space-x-1.5 text-xs font-bold text-rose-300">
                      <AlertTriangle className="w-3.5 h-3.5" />
                      <span>Предложение отклонено валидатором</span>
                    </div>
                    {currentProp.validation_errors && currentProp.validation_errors.length > 0 ? (
                      <ul className="list-disc list-inside text-[11px] text-rose-200/90 pl-1 space-y-0.5">
                        {currentProp.validation_errors.map((err, errIdx) => (
                          <li key={errIdx}>{err}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-[11px] text-rose-200">
                        Цитаты модели не найдены в стенограмме кандидата.
                      </p>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="p-6 text-center text-xs text-slate-500 border border-dashed border-slate-800 rounded-xl">
                Автооценка недоступна. Ожидание ответов кандидата или ручного запуска оценки.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

