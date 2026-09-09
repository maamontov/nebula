import React, { useState, useEffect } from 'react';
import { InterviewPlan, AssessmentProposal, TranscriptSegment } from '../types';
import { approveAssessment, getInterview, exportInterview, enqueueJob } from '../services/api';
import {
  Check,
  ShieldCheck,
  Download,
  Award,
  RotateCcw,
  ExternalLink,
  FileText,
  ChevronDown,
  ChevronUp,
  Sparkles,
  Loader2,
  CheckCircle2,
} from 'lucide-react';

interface ReviewScreenProps {
  interviewId: string;
  plan: InterviewPlan;
  onNewInterview: () => void;
}

export const ReviewScreen: React.FC<ReviewScreenProps> = ({
  interviewId,
  plan,
  onNewInterview,
}) => {
  const [proposals, setProposals] = useState<AssessmentProposal[]>([]);
  const [questionScores, setQuestionScores] = useState<Record<string, number>>({});
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [selectedQuote, setSelectedQuote] = useState<{ quote: string; segmentId: string } | null>(null);
  const [showFullTranscript, setShowFullTranscript] = useState(false);
  const [reviewerNotes, setReviewerNotes] = useState<Record<string, string>>({});
  const [savedSuccess, setSavedSuccess] = useState<Record<string, boolean>>({});
  const [isExporting, setIsExporting] = useState(false);
  const [isEvaluatingAll, setIsEvaluatingAll] = useState(false);

  useEffect(() => {
    async function loadData() {
      try {
        const data = await getInterview(interviewId);
        const existingProps: AssessmentProposal[] = data.assessment_proposals || [];
        const initialScores: Record<string, number> = {};
        const initialNotes: Record<string, string> = {};

        existingProps.forEach((p) => {
          const score = p.reviewed_scores?.[0]?.score ?? p.scores?.[0]?.score;
          if (score !== undefined) {
            initialScores[p.question_id] = score;
          }
          if (p.reviewer_notes) {
            initialNotes[p.question_id] = p.reviewer_notes;
          }
        });

        // Ensure every question has a proposal object and an initial score
        const mergedProps: AssessmentProposal[] = plan.questions.map((q, idx) => {
          const existing = existingProps.find((p) => p.question_id === q.id);
          if (existing) {
            if (!initialScores[q.id]) {
              initialScores[q.id] = existing.scores?.[0]?.score ?? 4;
            }
            return existing;
          }

          const defaultScore = initialScores[q.id] || 4;
          initialScores[q.id] = defaultScore;
          return {
            id: `prop-${q.id}`,
            interview_id: interviewId,
            question_id: q.id,
            model_profile_id: 'manual/reviewer',
            scores: [
              {
                criterion_id: q.criteria[0]?.id || `crit-${idx}`,
                score: defaultScore,
                explanation: 'Оценка эксперта-интервьюера на этапе ревью.',
                evidence: [],
              },
            ],
            critical_errors: [],
            is_approved: false,
            created_at: new Date().toISOString(),
          };
        });

        setProposals(mergedProps);
        setQuestionScores(initialScores);
        setReviewerNotes(initialNotes);

        if (data.transcript_segments && data.transcript_segments.length > 0) {
          setSegments(data.transcript_segments);
        } else {
          // Default demo segments if no segments captured yet
          setSegments([
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
        }
      } catch (err) {
        console.warn('Failed to load review data from backend:', err);
      }
    }
    loadData();
  }, [interviewId, plan]);

  // Deterministic 100-point score computation
  const calculateFinalScore = () => {
    let totalWeight = 0;
    let earnedWeight = 0;

    for (const q of plan.questions) {
      const score = questionScores[q.id] ?? (proposals.find((p) => p.question_id === q.id)?.scores[0]?.score);
      if (score !== undefined && score !== null) {
        const normalized = ((score - 1.0) / (5.0 - 1.0)) * 100.0;
        earnedWeight += normalized * q.weight;
      }
      totalWeight += q.weight;
    }

    return totalWeight > 0 ? Math.round(earnedWeight / totalWeight) : 0;
  };

  const handleScoreChange = async (questionId: string, newScore: number) => {
    setQuestionScores((prev) => ({ ...prev, [questionId]: newScore }));

    setProposals((prev) => {
      const existing = prev.find((p) => p.question_id === questionId);
      if (existing) {
        return prev.map((p) => {
          if (p.question_id === questionId) {
            const updatedScores = p.scores && p.scores.length > 0
              ? p.scores.map((s) => ({ ...s, score: newScore }))
              : [
                  {
                    criterion_id: 'crit-default',
                    score: newScore,
                    explanation: 'Ручная оценка интервьюера.',
                    evidence: [],
                  },
                ];
            return { ...p, scores: updatedScores, is_approved: true };
          }
          return p;
        });
      } else {
        const q = plan.questions.find((x) => x.id === questionId);
        const newProp: AssessmentProposal = {
          id: `prop-${questionId}`,
          interview_id: interviewId,
          question_id: questionId,
          model_profile_id: 'manual/reviewer',
          scores: [
            {
              criterion_id: q?.criteria[0]?.id || 'crit-default',
              score: newScore,
              explanation: 'Ручная оценка интервьюера.',
              evidence: [],
            },
          ],
          critical_errors: [],
          is_approved: true,
          created_at: new Date().toISOString(),
        };
        return [...prev, newProp];
      }
    });

    // Auto-save to backend
    const prop = proposals.find((p) => p.question_id === questionId);
    const propId = prop?.id || `prop-${questionId}`;
    try {
      await approveAssessment(
        interviewId,
        propId,
        [
          {
            criterion_id: prop?.scores?.[0]?.criterion_id || 'crit-default',
            score: newScore,
            explanation: prop?.scores?.[0]?.explanation || 'Ручная оценка интервьюера.',
            evidence: prop?.scores?.[0]?.evidence || [],
          },
        ],
        reviewerNotes[questionId] || 'Оценка подтверждена экспертом',
        questionId
      );
      setSavedSuccess((prev) => ({ ...prev, [questionId]: true }));
      setTimeout(() => {
        setSavedSuccess((prev) => ({ ...prev, [questionId]: false }));
      }, 2500);
    } catch (e) {
      console.warn('Auto-save score warning:', e);
    }
  };

  const handleSaveApproval = async (questionId: string) => {
    const prop = proposals.find((p) => p.question_id === questionId);
    const propId = prop?.id || `prop-${questionId}`;
    const score = questionScores[questionId] || prop?.scores?.[0]?.score || 4;

    try {
      await approveAssessment(
        interviewId,
        propId,
        [
          {
            criterion_id: prop?.scores?.[0]?.criterion_id || 'crit-default',
            score,
            explanation: prop?.scores?.[0]?.explanation || 'Оценка подтверждена интервьюером.',
            evidence: prop?.scores?.[0]?.evidence || [],
          },
        ],
        reviewerNotes[questionId] || 'Оценка подтверждена экспертом',
        questionId
      );
      setSavedSuccess((prev) => ({ ...prev, [questionId]: true }));
      setTimeout(() => {
        setSavedSuccess((prev) => ({ ...prev, [questionId]: false }));
      }, 3000);
    } catch (e) {
      console.warn('Approve error:', e);
    }
  };

  const handleAutoEvaluateAll = async () => {
    setIsEvaluatingAll(true);
    try {
      const candidateSegments = segments.filter((s) => s.track_id === 'candidate');
      const candidateText =
        candidateSegments.map((s) => s.text).join(' ') ||
        'Для проектирования распределённых транзакций используется паттерн Saga с оркестратором и компенсирующими транзакциями. При выборе БД PostgreSQL обеспечивает строгий ACID и надёжное партиционирование.';
      const lastSegId = candidateSegments[candidateSegments.length - 1]?.id || 'seg-2';

      for (const q of plan.questions) {
        await enqueueJob(interviewId, 'EVALUATE_QUESTION', {
          question_id: q.id,
          candidate_text: candidateText,
          segment_id: lastSegId,
          rubric_description: q.criteria.map((c) => c.title).join(', '),
        });
      }

      // Poll for proposals updates
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        try {
          const data = await getInterview(interviewId);
          if (
            (data.assessment_proposals && data.assessment_proposals.length >= plan.questions.length) ||
            attempts >= 10
          ) {
            clearInterval(poll);
            if (data.assessment_proposals && data.assessment_proposals.length > 0) {
              setProposals(data.assessment_proposals);
              const scores: Record<string, number> = {};
              data.assessment_proposals.forEach((p) => {
                const sc = p.reviewed_scores?.[0]?.score ?? p.scores?.[0]?.score;
                if (sc !== undefined) scores[p.question_id] = sc;
              });
              setQuestionScores((prev) => ({ ...prev, ...scores }));
            }
            setIsEvaluatingAll(false);
          }
        } catch {
          if (attempts >= 10) {
            clearInterval(poll);
            setIsEvaluatingAll(false);
          }
        }
      }, 1000);
    } catch (e) {
      console.error('Failed to auto evaluate:', e);
      setIsEvaluatingAll(false);
    }
  };

  const exportJsonReport = async () => {
    setIsExporting(true);
    try {
      let exportData: Record<string, unknown>;
      try {
        exportData = await exportInterview(interviewId);
      } catch {
        // Fallback local payload
        exportData = {
          interview_id: interviewId,
          exported_at: new Date().toISOString(),
          candidate_role: plan.role,
          final_score_100: calculateFinalScore(),
          questions_count: plan.questions.length,
          proposals,
          transcript_segments: segments,
          reviewer_notes: reviewerNotes,
        };
      }

      const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `nebula-verified-report-${interviewId}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setIsExporting(false);
    }
  };

  const finalScore = calculateFinalScore();

  return (
    <div className="max-w-5xl mx-auto p-8 space-y-8 overflow-y-auto h-[calc(100vh-4rem)]">
      {/* Top Banner with Score */}
      <div className="glass-panel p-6 rounded-2xl flex items-center justify-between shadow-xl">
        <div className="flex items-center space-x-4">
          <div className="w-14 h-14 rounded-2xl bg-gradient-to-tr from-emerald-600 to-teal-500 flex items-center justify-center shadow-lg shadow-emerald-500/20">
            <Award className="w-8 h-8 text-white" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <h2 className="text-xl font-bold text-slate-100">Итоговый отчёт интервью</h2>
              <span className="flex items-center space-x-1 px-2.5 py-0.5 text-xs font-semibold text-emerald-400 bg-emerald-950/60 border border-emerald-800/60 rounded-full">
                <ShieldCheck className="w-3.5 h-3.5" />
                <span>Человеческая верификация</span>
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Сессия: <span className="font-mono text-slate-300">{interviewId}</span> • Роль: {plan.role}
            </p>
          </div>
        </div>

        <div className="text-right">
          <div className="text-3xl font-extrabold text-emerald-400 font-mono tracking-tight">
            {finalScore} <span className="text-base text-slate-400 font-normal">/ 100</span>
          </div>
          <span className="text-[11px] text-slate-400">Взвешенный балл Nebula</span>
        </div>
      </div>

      {/* Interactive Transcript Drawer / Accordion */}
      <div className="border border-slate-800 rounded-xl bg-slate-900/40 overflow-hidden">
        <button
          onClick={() => setShowFullTranscript(!showFullTranscript)}
          className="w-full px-5 py-3 flex items-center justify-between text-left hover:bg-slate-900/80 transition cursor-pointer"
        >
          <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-300">
            <FileText className="w-4 h-4 text-indigo-400" />
            <span>Полная стенограмма собеседования ({segments.length} сегментов)</span>
          </div>
          <div className="flex items-center space-x-2 text-xs text-slate-400">
            <span>{showFullTranscript ? 'Свернуть' : 'Развернуть'}</span>
            {showFullTranscript ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </div>
        </button>

        {showFullTranscript && (
          <div className="p-4 border-t border-slate-800 space-y-3 max-h-80 overflow-y-auto bg-slate-950/70">
            {segments.map((s) => {
              const isSelected = selectedQuote?.segmentId === s.id;
              const isCandidate = s.track_id === 'candidate';
              return (
                <div
                  key={s.id}
                  id={`review-seg-${s.id}`}
                  className={`p-3 rounded-lg text-xs leading-relaxed transition-all ${
                    isSelected
                      ? 'ring-2 ring-amber-400 bg-amber-950/40 text-amber-100 border border-amber-500'
                      : isCandidate
                      ? 'bg-emerald-950/20 text-emerald-100 border border-emerald-900/40'
                      : 'bg-indigo-950/20 text-indigo-100 border border-indigo-900/40'
                  }`}
                >
                  <div className="flex items-center justify-between mb-1 text-[10px] text-slate-400">
                    <span className="font-semibold text-slate-300">
                      {isCandidate ? 'Кандидат' : 'Интервьюер'}
                    </span>
                    <span className="font-mono">
                      {Math.round(s.start_time_ms / 1000)}s - {Math.round(s.end_time_ms / 1000)}s
                    </span>
                  </div>
                  <p>{s.text}</p>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Question Proposals & Human Approval */}
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400">
            Оценка по вопросам ({plan.questions.length})
          </h3>

          <button
            onClick={handleAutoEvaluateAll}
            disabled={isEvaluatingAll}
            className="flex items-center space-x-2 px-3.5 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-semibold rounded-lg shadow-sm transition cursor-pointer disabled:cursor-not-allowed"
          >
            {isEvaluatingAll ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Sparkles className="w-3.5 h-3.5 text-indigo-200" />
            )}
            <span>{isEvaluatingAll ? 'Анализируем ответы...' : 'Автооценка всех ответов (AI)'}</span>
          </button>
        </div>

        {plan.questions.map((q, idx) => {
          const prop = proposals.find((p) => p.question_id === q.id);
          const currentScore = questionScores[q.id] ?? prop?.scores?.[0]?.score ?? 4;
          const isSaved = savedSuccess[q.id];

          return (
            <div key={q.id} className="glass-panel p-6 rounded-xl space-y-4">
              <div className="flex items-start justify-between">
                <div>
                  <span className="text-xs font-bold text-indigo-400">Вопрос #{idx + 1}</span>
                  <h4 className="text-sm font-semibold text-slate-100 mt-1">{q.text}</h4>
                </div>

                <div className="flex items-center space-x-2 bg-slate-900 px-3 py-1.5 rounded-lg border border-slate-800">
                  <span className="text-xs text-slate-400 font-medium">Балл (1–5):</span>
                  {[1, 2, 3, 4, 5].map((val) => {
                    const isSelected = currentScore === val;
                    return (
                      <button
                        key={val}
                        type="button"
                        onClick={() => handleScoreChange(q.id, val)}
                        className={`w-7 h-7 rounded text-xs font-bold transition cursor-pointer flex items-center justify-center ${
                          isSelected
                            ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/40 ring-2 ring-indigo-400 scale-105'
                            : 'bg-slate-800 text-slate-400 hover:bg-slate-700 hover:text-slate-200'
                        }`}
                      >
                        {val}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="space-y-3 bg-slate-900/50 p-4 rounded-lg border border-slate-800/80">
                <p className="text-xs text-slate-300 leading-relaxed">
                  {prop?.scores?.[0]?.explanation ||
                    'Оценка по критериям: ' + q.criteria.map((c) => c.title).join(', ')}
                </p>

                {/* Evidence Quotes with Click-to-Inspect */}
                {prop?.scores?.[0]?.evidence && prop.scores[0].evidence.length > 0 && (
                  <div className="space-y-2">
                    <span className="text-[11px] font-semibold text-slate-400">
                      Подтверждающий текст кандидата (кликните для проверки):
                    </span>
                    {prop.scores[0].evidence.map((ev, eIdx) => (
                      <div
                        key={eIdx}
                        onClick={() => {
                          setSelectedQuote({ quote: ev.exact_quote, segmentId: ev.segment_id });
                          setShowFullTranscript(true);
                          setTimeout(() => {
                            document
                              .getElementById(`review-seg-${ev.segment_id}`)
                              ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                          }, 100);
                        }}
                        className="group p-2.5 bg-slate-950 hover:bg-indigo-950/30 border-l-2 border-emerald-500 hover:border-amber-400 rounded text-xs italic text-slate-200 cursor-pointer transition flex items-center justify-between"
                      >
                        <span>«{ev.exact_quote}»</span>
                        <span className="text-[10px] text-indigo-400 group-hover:text-amber-300 font-semibold flex items-center space-x-1 pl-2">
                          <span>Открыть</span>
                          <ExternalLink className="w-3 h-3" />
                        </span>
                      </div>
                    ))}
                  </div>
                )}

                {/* Reviewer Note Input */}
                <div className="pt-2">
                  <label className="block text-[11px] font-medium text-slate-400 mb-1">
                    Комментарий проверяющего (Human Note):
                  </label>
                  <div className="flex items-center space-x-2">
                    <input
                      type="text"
                      value={reviewerNotes[q.id] || ''}
                      onChange={(e) =>
                        setReviewerNotes({ ...reviewerNotes, [q.id]: e.target.value })
                      }
                      placeholder="Обоснование решения / замечания..."
                      className="flex-1 bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"
                    />
                    <button
                      type="button"
                      onClick={() => handleSaveApproval(q.id)}
                      className="flex items-center space-x-1 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-lg transition cursor-pointer"
                    >
                      {isSaved ? (
                        <>
                          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                          <span className="text-emerald-400">Сохранено</span>
                        </>
                      ) : (
                        <>
                          <Check className="w-3.5 h-3.5" />
                          <span>Сохранить</span>
                        </>
                      )}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Action Footer */}
      <div className="flex items-center justify-between pt-4 pb-12">
        <button
          onClick={onNewInterview}
          className="flex items-center space-x-2 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded-lg transition cursor-pointer"
        >
          <RotateCcw className="w-3.5 h-3.5" />
          <span>Новое собеседование</span>
        </button>

        <button
          onClick={exportJsonReport}
          disabled={isExporting}
          className="flex items-center space-x-2 px-5 py-2.5 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold rounded-lg shadow-lg shadow-emerald-600/30 transition cursor-pointer disabled:cursor-not-allowed"
        >
          <Download className="w-4 h-4" />
          <span>{isExporting ? 'Экспорт...' : 'Экспортировать отчёт (JSON)'}</span>
        </button>
      </div>
    </div>
  );
};
