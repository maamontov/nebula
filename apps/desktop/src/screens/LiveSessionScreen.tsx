import React, { useState, useEffect } from 'react';
import { InterviewPlan, TranscriptSegment, AssessmentProposal } from '../types';
import { stopAudioCapture, updateInterviewStatus, getInterview, enqueueJob, addTranscriptSegment } from '../services/api';
import { Square, Pause, Play, CheckCircle, MessageSquare, Quote, Sparkles, AlertTriangle, ExternalLink, Loader2 } from 'lucide-react';

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
  const [activeQuestionIdx, setActiveQuestionIdx] = useState(0);
  const [selectedSegmentId, setSelectedSegmentId] = useState<string | null>(null);
  const [isEvaluating, setIsEvaluating] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const [evalSuccessNotice, setEvalSuccessNotice] = useState<string | null>(null);

  const [segments, setSegments] = useState<TranscriptSegment[]>([
    {
      id: 'seg-1',
      track_id: 'interviewer',
      start_time_ms: 0,
      end_time_ms: 4500,
      text: plan.questions[0]?.text || 'Добрый день! Начнем с первого вопроса.',
      is_final: true,
    },
    {
      id: 'seg-2',
      track_id: 'candidate',
      start_time_ms: 5000,
      end_time_ms: 12500,
      text: 'Для распределённых транзакций мы применили паттерн Saga с оркестрацией. В случае сбоя шага оркестратор запускает компенсирующие транзакции в обратном порядке.',
      is_final: true,
    },
  ]);

  const [proposals, setProposals] = useState<AssessmentProposal[]>([
    {
      id: 'prop-1',
      interview_id: interviewId,
      question_id: plan.questions[0]?.id || 'q-1',
      model_profile_id: 'google/gemini-3.8-flash',
      scores: [
        {
          criterion_id: 'crit-arch-saga',
          score: 5.0,
          explanation: 'Кандидат корректно описывает применение паттерна Saga на основе оркестрации, а также механизм отката через запуск компенсирующих транзакций в обратном порядке.',
          evidence: [
            {
              segment_id: 'seg-2',
              exact_quote: 'Для распределённых транзакций мы применили паттерн Saga с оркестрацией. В случае сбоя шага оркестратор запускает компенсирующие транзакции в обратном порядке.',
            },
          ],
        },
      ],
      critical_errors: [],
      is_approved: false,
      created_at: new Date().toISOString(),
    },
  ]);

  // Monotonic timer
  useEffect(() => {
    if (isPaused) return;
    const timer = setInterval(() => setElapsedSec((s) => s + 1), 1000);
    return () => clearInterval(timer);
  }, [isPaused]);

  // Polling for backend updates
  useEffect(() => {
    const poll = setInterval(async () => {
      try {
        const data = await getInterview(interviewId);
        if (data.transcript_segments && data.transcript_segments.length > 0) {
          setSegments(data.transcript_segments);
        }
        if (data.assessment_proposals && data.assessment_proposals.length > 0) {
          setProposals(data.assessment_proposals);
        }
      } catch (err) {
        // Ignored in offline/local spike
      }
    }, 2000);

    return () => clearInterval(poll);
  }, [interviewId]);

  useEffect(() => {
    async function seedInitialSegments() {
      try {
        const data = await getInterview(interviewId);
        if (!data.transcript_segments || data.transcript_segments.length === 0) {
          for (const seg of segments) {
            await addTranscriptSegment(interviewId, seg);
          }
        }
      } catch (e) {
        console.warn('Seed initial segments error:', e);
      }
    }
    seedInitialSegments();
  }, [interviewId]);

  const formatTime = (totalSeconds: number) => {
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  const handleStop = async () => {
    if (isStopping) return;
    setIsStopping(true);
    try {
      await stopAudioCapture();
    } catch (e) {
      console.warn('stopAudioCapture warning/error:', e);
    }
    try {
      await updateInterviewStatus(interviewId, isPaused ? 'paused' : 'recording', 'review');
    } catch (e) {
      console.warn('updateInterviewStatus warning/error:', e);
    }
    onFinishSession(interviewId);
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
      // Find candidate speech segments
      const candidateSegments = segments.filter((s) => s.track_id === 'candidate');
      const candidateText = candidateSegments.map((s) => s.text).join(' ');
      const lastSegId = candidateSegments[candidateSegments.length - 1]?.id || 'seg-cand-last';

      await enqueueJob(interviewId, 'EVALUATE_QUESTION', {
        question_id: currentQ.id,
        candidate_text: candidateText || 'Кандидат ответил на вопрос по теме.',
        segment_id: lastSegId,
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
            onClick={() => setIsPaused(!isPaused)}
            className="flex items-center space-x-1.5 px-3.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-lg transition"
          >
            {isPaused ? <Play className="w-3.5 h-3.5 fill-slate-200" /> : <Pause className="w-3.5 h-3.5" />}
            <span>{isPaused ? 'Продолжить' : 'Пауза'}</span>
          </button>

          <button
            onClick={handleStop}
            disabled={isStopping}
            className="flex items-center space-x-1.5 px-4 py-1.5 bg-rose-600 hover:bg-rose-500 disabled:opacity-50 text-white text-xs font-semibold rounded-lg shadow-sm shadow-rose-600/30 transition cursor-pointer disabled:cursor-not-allowed"
          >
            <Square className="w-3.5 h-3.5 fill-white" />
            <span>{isStopping ? 'Завершение...' : 'Завершить запись'}</span>
          </button>
        </div>
      </div>

      {/* Main Grid: Left Questions, Center Live Transcripts, Right Live AI Copilot */}
      <div className="flex-1 grid grid-cols-12 gap-0 overflow-hidden">
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
        <div className="col-span-5 border-r border-slate-800 flex flex-col bg-slate-950/40">
          <div className="p-3 border-b border-slate-800/80 bg-slate-900/50 flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <MessageSquare className="w-4 h-4 text-slate-400" />
              <span className="text-xs font-bold uppercase tracking-wider text-slate-300">Живая стенограмма</span>
            </div>
            <span className="text-[11px] text-slate-400">Whisper Large v3 Turbo (STT)</span>
          </div>

          <div className="flex-1 p-4 space-y-4 overflow-y-auto">
            {segments.map((s) => {
              const isCandidate = s.track_id === 'candidate';
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
                    <span className={`text-[11px] font-semibold ${isCandidate ? 'text-emerald-400' : 'text-indigo-400'}`}>
                      {isCandidate ? 'Кандидат' : 'Интервьюер'}
                    </span>
                    <span className="text-[10px] text-slate-500 font-mono">
                      {Math.round(s.start_time_ms / 1000)}s - {Math.round(s.end_time_ms / 1000)}s
                    </span>
                    {isHighlighted && (
                      <span className="text-[10px] text-amber-400 font-semibold bg-amber-950/60 px-1.5 py-0.5 rounded border border-amber-800">
                        Подтверждающий фрагмент
                      </span>
                    )}
                  </div>
                  <div
                    className={`max-w-[85%] p-3 rounded-xl text-xs leading-relaxed transition-all ${
                      isHighlighted
                        ? 'ring-2 ring-amber-400 bg-amber-950/40 border border-amber-500 text-amber-100 shadow-lg shadow-amber-950/50'
                        : isCandidate
                        ? 'bg-emerald-950/30 border border-emerald-800/50 text-emerald-100 rounded-tr-none'
                        : 'bg-indigo-950/30 border border-indigo-800/50 text-indigo-100 rounded-tl-none'
                    }`}
                  >
                    {s.text}
                  </div>
                </div>
              );
            })}
          </div>
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
              </div>
            ) : (
              <div className="p-6 text-center text-xs text-slate-500 border border-dashed border-slate-800 rounded-xl">
                Ожидание ответа кандидата или нажмите кнопку «Запросить оценку» выше.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

