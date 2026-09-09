import React, { useState, useEffect } from 'react';
import { InterviewPlan, TranscriptSegment, AssessmentProposal } from '../types';
import { stopAudioCapture, updateInterviewStatus, getInterview } from '../services/api';
import { Square, Pause, Play, CheckCircle, MessageSquare, Quote, Sparkles, AlertTriangle } from 'lucide-react';

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
    }, 2500);

    return () => clearInterval(poll);
  }, [interviewId]);

  const formatTime = (totalSeconds: number) => {
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;
    return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  const handleStop = async () => {
    await stopAudioCapture();
    await updateInterviewStatus(interviewId, isPaused ? 'paused' : 'recording', 'review');
    onFinishSession(interviewId);
  };

  const currentQ = plan.questions[activeQuestionIdx] || plan.questions[0];
  const currentProp = proposals.find((p) => p.question_id === currentQ?.id);

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
            className="flex items-center space-x-1.5 px-4 py-1.5 bg-rose-600 hover:bg-rose-500 text-white text-xs font-semibold rounded-lg shadow-sm shadow-rose-600/30 transition"
          >
            <Square className="w-3.5 h-3.5 fill-white" />
            <span>Завершить запись</span>
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
              return (
                <div
                  key={s.id}
                  className={`flex flex-col ${isCandidate ? 'items-end' : 'items-start'}`}
                >
                  <div className="flex items-center space-x-2 mb-1">
                    <span className={`text-[11px] font-semibold ${isCandidate ? 'text-emerald-400' : 'text-indigo-400'}`}>
                      {isCandidate ? 'Кандидат' : 'Интервьюер'}
                    </span>
                    <span className="text-[10px] text-slate-500 font-mono">
                      {Math.round(s.start_time_ms / 1000)}s - {Math.round(s.end_time_ms / 1000)}s
                    </span>
                  </div>
                  <div
                    className={`max-w-[85%] p-3 rounded-xl text-xs leading-relaxed ${
                      isCandidate
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
        <div className="col-span-4 p-4 space-y-4 overflow-y-auto bg-slate-950/80">
          <div className="flex items-center space-x-2">
            <Sparkles className="w-4 h-4 text-indigo-400" />
            <span className="text-xs font-bold uppercase tracking-wider text-slate-200">Оперативная оценка</span>
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

                  {/* Verbatim Evidence Quotes */}
                  {score.evidence && score.evidence.length > 0 && (
                    <div className="space-y-1.5 pt-1">
                      <div className="flex items-center space-x-1.5 text-[11px] font-semibold text-slate-400">
                        <Quote className="w-3 h-3 text-indigo-400" />
                        <span>Дословные подтверждения (Evidence):</span>
                      </div>
                      {score.evidence.map((ev, eIdx) => (
                        <div
                          key={eIdx}
                          className="p-2 bg-slate-900/90 border-l-2 border-indigo-500 rounded text-[11px] text-slate-200 italic"
                        >
                          «{ev.exact_quote}»
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
              Ожидание ответа кандидата для запуска оценки Gemini 3.8 Flash...
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
