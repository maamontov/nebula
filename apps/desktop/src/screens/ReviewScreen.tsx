import React, { useState, useEffect } from 'react';
import { InterviewPlan, AssessmentProposal, TranscriptSegment, HumanAssessment } from '../types';
import {
  reviewAssessment,
  getInterview,
  exportInterview,
  enqueueJob,
  getSummary,
  confirmSummary,
  finalizeInterviewReport,
} from '../services/api';
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
  Lock,
  AlertCircle,
  AlertTriangle,
  XCircle,
  FileCheck2,
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
  const [humanAssessments, setHumanAssessments] = useState<HumanAssessment[]>([]);
  const [questionScores, setQuestionScores] = useState<Record<string, number>>({});
  const [criterionScores, setCriterionScores] = useState<Record<string, Record<string, number>>>({});
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [activeRevisionId, setActiveRevisionId] = useState<string>('trans-rev-1');
  const [serverScoring, setServerScoring] = useState<{
    final_score_100: number | null;
    coverage_percentage: number;
  } | null>(null);

  // Executive summary & hiring decision state (No fake defaults!)
  const [summaryMarkdown, setSummaryMarkdown] = useState<string>('');
  const [hiringRecommendation, setHiringRecommendation] = useState<string>('');
  const [isSummaryConfirmed, setIsSummaryConfirmed] = useState(false);
  const [isSavingSummary, setIsSavingSummary] = useState(false);
  const [summarySavedSuccess, setSummarySavedSuccess] = useState(false);

  // Lifecycle & Finalization state
  const [interviewStatus, setInterviewStatus] = useState<string>('review');
  const [finalizedChecksum, setFinalizedChecksum] = useState<string | null>(null);
  const [isFinalizing, setIsFinalizing] = useState(false);
  const [finalizeError, setFinalizeError] = useState<string | null>(null);
  const [finalizeSuccess, setFinalizeSuccess] = useState(false);

  // Exclusions map: question_id -> { isExcluded: boolean; reason: string }
  const [excludedQuestions, setExcludedQuestions] = useState<Record<string, { isExcluded: boolean; reason: string }>>({});

  const [selectedQuote, setSelectedQuote] = useState<{ quote: string; segmentId: string } | null>(null);
  const [showFullTranscript, setShowFullTranscript] = useState(false);
  const [reviewerNotes, setReviewerNotes] = useState<Record<string, string>>({});
  const [savedSuccess, setSavedSuccess] = useState<Record<string, boolean>>({});
  const [isExporting, setIsExporting] = useState(false);
  const [isEvaluatingAll, setIsEvaluatingAll] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [evalWarning, setEvalWarning] = useState<string | null>(null);

  useEffect(() => {
    async function loadData() {
      try {
        const data = await getInterview(interviewId);
        const existingProps: AssessmentProposal[] = data.assessment_proposals || [];
        const existingHuman: HumanAssessment[] = (data.human_assessments as HumanAssessment[]) || [];
        const initialScores: Record<string, number> = {};
        const initialCritScores: Record<string, Record<string, number>> = {};
        const initialNotes: Record<string, string> = {};
        const exclusionsMap: Record<string, { isExcluded: boolean; reason: string }> = {};

        if (data.interview?.status) {
          setInterviewStatus(data.interview.status);
        }

        // Baseline proposals from AI
        existingProps.forEach((p) => {
          if (p.scores && p.scores.length > 0) {
            initialCritScores[p.question_id] = {};
            p.scores.forEach((sc) => {
              initialCritScores[p.question_id][sc.criterion_id] = sc.score;
            });
            initialScores[p.question_id] = p.scores[0].score;
          }
        });

        // Authoritative human assessments take precedence
        existingHuman.forEach((ha) => {
          if (ha.is_excluded) {
            exclusionsMap[ha.question_id] = {
              isExcluded: true,
              reason: ha.exclusion_reason || '',
            };
          }
          if (ha.scores && ha.scores.length > 0) {
            if (!initialCritScores[ha.question_id]) initialCritScores[ha.question_id] = {};
            let sum = 0;
            ha.scores.forEach((sc) => {
              initialCritScores[ha.question_id][sc.criterion_id] = sc.score;
              sum += sc.score;
            });
            initialScores[ha.question_id] = Math.round(sum / ha.scores.length);
          }
          if (ha.reviewer_notes) {
            initialNotes[ha.question_id] = ha.reviewer_notes;
          }
        });

        setProposals(existingProps);
        setHumanAssessments(existingHuman);
        setQuestionScores(initialScores);
        setCriterionScores(initialCritScores);
        setReviewerNotes(initialNotes);
        setExcludedQuestions(exclusionsMap);
        setSegments(data.transcript_segments || []);

        if (data.interview?.active_transcript_revision_id) {
          setActiveRevisionId(data.interview.active_transcript_revision_id);
        }
        if (data.scoring) {
          setServerScoring(data.scoring);
        }

        // Load Executive Summary
        try {
          const sumData = await getSummary(interviewId);
          if (sumData.has_summary) {
            setIsSummaryConfirmed(Boolean(sumData.is_confirmed));
            if (sumData.confirmed_markdown) {
              setSummaryMarkdown(sumData.confirmed_markdown);
            } else if (sumData.summary?.overview) {
              setSummaryMarkdown(sumData.summary.overview);
            }
            if (sumData.confirmed_recommendation) {
              setHiringRecommendation(sumData.confirmed_recommendation);
            }
          }
        } catch (sumErr) {
          console.warn('Failed to load executive summary:', sumErr);
        }
      } catch (err) {
        console.warn('Failed to load review data from backend:', err);
      }
    }
    loadData();
  }, [interviewId, plan]);

  const isFinalized = interviewStatus === 'finalized';

  // Deterministic 100-point score computation over assessed questions
  const calculateFinalScore = (): number | null => {
    let totalAssessedWeight = 0;
    let earnedWeight = 0;
    let assessedCount = 0;

    for (const q of plan.questions) {
      if (excludedQuestions[q.id]?.isExcluded) continue;
      const score = questionScores[q.id];
      if (score !== undefined && score !== null) {
        const normalized = ((score - 1.0) / (5.0 - 1.0)) * 100.0;
        earnedWeight += normalized * q.weight;
        totalAssessedWeight += q.weight;
        assessedCount++;
      }
    }

    if (assessedCount === 0 || totalAssessedWeight === 0) {
      return null;
    }

    return Math.round(earnedWeight / totalAssessedWeight);
  };

  const handleCriterionScoreChange = (questionId: string, criterionId: string, newScore: number) => {
    if (isFinalized) return;
    setCriterionScores((prev) => {
      const qCrits = { ...(prev[questionId] || {}), [criterionId]: newScore };
      return { ...prev, [questionId]: qCrits };
    });

    const q = plan.questions.find((x) => x.id === questionId);
    if (q && q.criteria.length > 0) {
      const currentCrits = { ...(criterionScores[questionId] || {}), [criterionId]: newScore };
      let sum = 0;
      let count = 0;
      q.criteria.forEach((c) => {
        if (currentCrits[c.id] !== undefined) {
          sum += currentCrits[c.id];
          count++;
        }
      });
      if (count > 0) {
        setQuestionScores((prev) => ({ ...prev, [questionId]: Math.round(sum / count) }));
      }
    }
  };

  const handleScoreChange = (questionId: string, newScore: number) => {
    if (isFinalized) return;
    setQuestionScores((prev) => ({ ...prev, [questionId]: newScore }));
    const q = plan.questions.find((x) => x.id === questionId);
    if (q) {
      const qCrits: Record<string, number> = {};
      q.criteria.forEach((c) => {
        qCrits[c.id] = newScore;
      });
      setCriterionScores((prev) => ({ ...prev, [questionId]: qCrits }));
    }
  };

  const handleSaveApproval = async (questionId: string) => {
    if (isFinalized) return;
    const q = plan.questions.find((x) => x.id === questionId);
    if (!q) return;

    const qCrits = criterionScores[questionId] || {};
    const scoresToSubmit = q.criteria.map((c) => {
      const val = qCrits[c.id] ?? questionScores[questionId];
      return {
        criterion_id: c.id,
        score: val !== undefined ? val : 3.0,
      };
    });

    if (questionScores[questionId] === undefined && Object.keys(qCrits).length === 0) {
      alert('Пожалуйста, сначала выберите балл для этого вопроса.');
      return;
    }

    try {
      await reviewAssessment(interviewId, questionId, {
        expected_transcript_revision: activeRevisionId,
        scores: scoresToSubmit,
        reviewer_notes: reviewerNotes[questionId] || 'Оценка подтверждена экспертом',
        is_manually_adjusted: true,
      });

      setSavedSuccess((prev) => ({ ...prev, [questionId]: true }));
      setTimeout(() => {
        setSavedSuccess((prev) => ({ ...prev, [questionId]: false }));
      }, 3000);

      // Refresh data to update server scoring
      const updated = await getInterview(interviewId);
      if (updated.scoring) setServerScoring(updated.scoring);
      if (updated.human_assessments) setHumanAssessments(updated.human_assessments as HumanAssessment[]);
    } catch (e: any) {
      console.error('Save review error:', e);
      if (e?.message?.includes('409')) {
        alert('Конфликт версий: стенограмма собеседования была изменена или интервью финализировано.');
      } else {
        alert(`Ошибка при сохранении оценки: ${e?.message || e}`);
      }
    }
  };

  const handleToggleExcludeQuestion = async (questionId: string) => {
    if (isFinalized) return;
    const currentEx = excludedQuestions[questionId]?.isExcluded;
    if (currentEx) {
      // Un-exclude
      setExcludedQuestions((prev) => {
        const copy = { ...prev };
        delete copy[questionId];
        return copy;
      });
    } else {
      const reason = window.prompt('Укажите обязательную причину исключения этого вопроса:') || '';
      if (!reason.trim()) {
        alert('Исключение вопроса требует явного указания причины.');
        return;
      }
      setExcludedQuestions((prev) => ({
        ...prev,
        [questionId]: { isExcluded: true, reason: reason.trim() },
      }));
      try {
        await reviewAssessment(interviewId, questionId, {
          expected_transcript_revision: activeRevisionId,
          scores: [],
          reviewer_notes: `Вопрос исключён экспертом: ${reason.trim()}`,
          is_manually_adjusted: true,
          is_excluded: true,
          exclusion_reason: reason.trim(),
        });
        const updated = await getInterview(interviewId);
        if (updated.scoring) setServerScoring(updated.scoring);
        if (updated.human_assessments) setHumanAssessments(updated.human_assessments as HumanAssessment[]);
      } catch (err: any) {
        alert(`Ошибка при исключении вопроса: ${err?.message || err}`);
      }
    }
  };

  const handleSaveSummary = async () => {
    if (isFinalized) return;
    if (!summaryMarkdown.trim()) {
      alert('Пожалуйста, введите текст резюме (Executive Summary).');
      return;
    }
    if (!hiringRecommendation) {
      alert('Пожалуйста, выберите рекомендацию по найму (Hiring Decision).');
      return;
    }

    setIsSavingSummary(true);
    try {
      await confirmSummary(interviewId, {
        reviewer_id: 'lead-interviewer',
        confirmed_markdown: summaryMarkdown.trim(),
        confirmed_recommendation: hiringRecommendation,
      });
      setIsSummaryConfirmed(true);
      setSummarySavedSuccess(true);
      setTimeout(() => setSummarySavedSuccess(false), 3000);
    } catch (err: any) {
      alert(`Ошибка при сохранении резюме: ${err?.message || err}`);
    } finally {
      setIsSavingSummary(false);
    }
  };

  const getFinalizeBlockers = (): string[] => {
    if (isFinalized) return ['Отчёт уже финализирован и зафиксирован (Sealed).'];
    const blockers: string[] = [];

    for (const q of plan.questions) {
      const isEx = excludedQuestions[q.id]?.isExcluded;
      const exReason = excludedQuestions[q.id]?.reason;
      if (isEx) {
        if (!exReason || !exReason.trim()) {
          blockers.push(`Вопрос «${q.title || q.text}»: указан как исключённый, но отсутствует причина.`);
        }
        continue;
      }
      const hasHuman = humanAssessments.some(
        (ha) => ha.question_id === q.id && !ha.is_stale && !ha.is_excluded && ha.scores && ha.scores.length > 0
      );
      if (!hasHuman) {
        blockers.push(`Вопрос «${q.title || q.text}» не оценен человеком (требуется подтверждение оценки или явное исключение).`);
      }
    }

    if (!summaryMarkdown.trim()) {
      blockers.push('Резюме встречи (Executive Summary) не заполнено.');
    }
    if (!hiringRecommendation) {
      blockers.push('Рекомендация по найму (Hiring Decision) не выбрана.');
    }
    if (!isSummaryConfirmed) {
      blockers.push('Резюме и рекомендация должны быть сохранены перед финализацией.');
    }
    return blockers;
  };

  const handleFinalizeReport = async () => {
    setFinalizeError(null);
    const blockers = getFinalizeBlockers();
    if (blockers.length > 0) {
      setFinalizeError(blockers.join('\n• '));
      return;
    }

    setIsFinalizing(true);
    try {
      const res = await finalizeInterviewReport(interviewId, {
        summary_markdown: summaryMarkdown.trim(),
        hiring_recommendation: hiringRecommendation,
        confirmed_by: 'lead-interviewer',
      });
      setInterviewStatus('finalized');
      setFinalizedChecksum(res.sha256_checksum);
      setFinalizeSuccess(true);
      const updated = await getInterview(interviewId);
      if (updated.scoring) setServerScoring(updated.scoring);
    } catch (err: any) {
      setFinalizeError(err?.message || 'Ошибка финализации отчёта');
    } finally {
      setIsFinalizing(false);
    }
  };

  const handleAutoEvaluateAll = async () => {
    if (isFinalized) return;
    setEvalWarning(null);
    const candidateSegments = segments.filter((s) => s.track_id === 'candidate');
    if (candidateSegments.length === 0) {
      setEvalWarning('В стенограмме нет распознанной речи кандидата для запуска автооценки.');
      return;
    }

    setIsEvaluatingAll(true);
    try {
      for (const q of plan.questions) {
        await enqueueJob(interviewId, 'EVALUATE_QUESTION', {
          question_id: q.id,
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
    setExportError(null);
    try {
      const exportData = await exportInterview(interviewId);
      const isFin = isFinalized || (exportData as any)?.status === 'FINALIZED';
      const filePrefix = isFin ? 'nebula-verified-report' : 'nebula-draft-report';

      const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${filePrefix}-${interviewId}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      console.error('Export failed:', err);
      setExportError(err?.message || 'Не удалось сформировать экспорт отчёта с сервера');
    } finally {
      setIsExporting(false);
    }
  };

  const finalScore = serverScoring?.final_score_100 !== undefined && serverScoring?.final_score_100 !== null
    ? Math.round(serverScoring.final_score_100)
    : calculateFinalScore();

  return (
    <div className="max-w-5xl mx-auto p-8 space-y-8 overflow-y-auto h-[calc(100vh-4rem)]">
      {/* Top Banner with Score and Status */}
      <div className="glass-panel p-6 rounded-2xl flex items-center justify-between shadow-xl">
        <div className="flex items-center space-x-4">
          <div className="w-14 h-14 rounded-2xl bg-gradient-to-tr from-emerald-600 to-teal-500 flex items-center justify-center shadow-lg shadow-emerald-500/20">
            <Award className="w-8 h-8 text-white" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <h2 className="text-xl font-bold text-slate-100">Итоговый отчёт интервью</h2>
              {isFinalized ? (
                <span className="flex items-center space-x-1 px-2.5 py-0.5 text-xs font-semibold text-emerald-400 bg-emerald-950/80 border border-emerald-700 rounded-full">
                  <Lock className="w-3.5 h-3.5" />
                  <span>Финализировано (Sealed)</span>
                </span>
              ) : (
                <span className="flex items-center space-x-1 px-2.5 py-0.5 text-xs font-semibold text-amber-400 bg-amber-950/60 border border-amber-800/60 rounded-full">
                  <ShieldCheck className="w-3.5 h-3.5" />
                  <span>Черновик на проверке</span>
                </span>
              )}
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Сессия: <span className="font-mono text-slate-300">{interviewId}</span> • Роль: {plan.role}
              {finalizedChecksum && (
                <span className="ml-2 font-mono text-[11px] text-emerald-400">
                  • SHA-256: {finalizedChecksum.substring(0, 12)}...
                </span>
              )}
            </p>
          </div>
        </div>

        <div className="text-right">
          {finalScore !== null ? (
            <>
              <div className="text-3xl font-extrabold text-emerald-400 font-mono tracking-tight">
                {finalScore} <span className="text-base text-slate-400 font-normal">/ 100</span>
              </div>
              <span className="text-[11px] text-slate-400">
                {serverScoring
                  ? `Взвешенный балл • Покрытие: ${serverScoring.coverage_percentage}%`
                  : 'Взвешенный балл Nebula'}
              </span>
            </>
          ) : (
            <>
              <div className="text-3xl font-bold text-slate-500 font-mono tracking-tight">
                — <span className="text-base text-slate-600 font-normal">/ 100</span>
              </div>
              <span className="text-[11px] text-slate-500">Оценки ещё не выставлены</span>
            </>
          )}
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
            {segments.length === 0 ? (
              <p className="text-xs text-slate-500 italic py-2">Стенограмма пуста (аудиозапись не содержала речи или ещё не обработана).</p>
            ) : (
              segments.map((s) => {
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
              })
            )}
          </div>
        )}
      </div>

      {/* Question Proposals & Human Approval */}
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400">
            1. Экспертная верификация вопросов ({plan.questions.length})
          </h3>

          {!isFinalized && (
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
          )}
        </div>

        {evalWarning && (
          <div className="p-3 bg-amber-950/40 border border-amber-800 text-amber-200 text-xs rounded-lg">
            {evalWarning}
          </div>
        )}

        {plan.questions.map((q, idx) => {
          const prop = proposals.find((p) => p.question_id === q.id);
          const currentScore = questionScores[q.id] ?? prop?.scores?.[0]?.score;
          const isSaved = savedSuccess[q.id];
          const isExcluded = excludedQuestions[q.id]?.isExcluded;
          const exclusionReason = excludedQuestions[q.id]?.reason;
          const hasHuman = humanAssessments.some((ha) => ha.question_id === q.id && !ha.is_stale);

          return (
            <div key={q.id} className={`glass-panel p-6 rounded-xl space-y-4 ${isExcluded ? 'opacity-60 bg-slate-900/30' : ''}`}>
              <div className="flex items-start justify-between">
                <div>
                  <div className="flex items-center space-x-2">
                    <span className="text-xs font-bold text-indigo-400">Вопрос #{idx + 1}</span>
                    {isExcluded ? (
                      <span className="px-2 py-0.5 text-[10px] font-semibold text-rose-400 bg-rose-950/60 border border-rose-800/60 rounded flex items-center space-x-1">
                        <XCircle className="w-3 h-3" />
                        <span>Исключён из оценки</span>
                      </span>
                    ) : hasHuman ? (
                      <span className="px-2 py-0.5 text-[10px] font-semibold text-emerald-400 bg-emerald-950/60 border border-emerald-800/60 rounded flex items-center space-x-1">
                        <ShieldCheck className="w-3 h-3" />
                        <span>Подтверждено экспертом ({currentScore} / 5.0)</span>
                      </span>
                    ) : prop?.is_rejected ? (
                      <span className="px-2 py-0.5 text-[10px] font-semibold text-rose-400 bg-rose-950/60 border border-rose-800/60 rounded flex items-center space-x-1">
                        <AlertTriangle className="w-3 h-3" />
                        <span>AI-оценка отклонена</span>
                      </span>
                    ) : currentScore !== undefined ? (
                      <span className="px-2 py-0.5 text-[10px] font-semibold text-indigo-300 bg-indigo-950/60 border border-indigo-800/60 rounded">
                        Черновик: {currentScore} / 5.0
                      </span>
                    ) : (
                      <span className="px-2 py-0.5 text-[10px] font-semibold text-slate-400 bg-slate-800 rounded">
                        Не оценено
                      </span>
                    )}
                  </div>
                  <h4 className="text-sm font-semibold text-slate-100 mt-1">{q.text}</h4>
                  {isExcluded && exclusionReason && (
                    <p className="text-xs text-rose-300 italic mt-1">Причина исключения: {exclusionReason}</p>
                  )}
                </div>

                <div className="flex items-center space-x-2">
                  {!isFinalized && (
                    <button
                      type="button"
                      onClick={() => handleToggleExcludeQuestion(q.id)}
                      className="px-2.5 py-1 text-[11px] font-medium text-slate-400 hover:text-rose-300 bg-slate-900 border border-slate-800 rounded-lg hover:border-rose-800 transition cursor-pointer"
                    >
                      {isExcluded ? 'Вернуть в оценку' : 'Исключить вопрос'}
                    </button>
                  )}

                  {!isExcluded && (
                    <div className="flex items-center space-x-2 bg-slate-900 px-3 py-1.5 rounded-lg border border-slate-800">
                      <span className="text-xs text-slate-400 font-medium">Балл (1–5):</span>
                      {[1, 2, 3, 4, 5].map((val) => {
                        const isSelected = currentScore === val;
                        return (
                          <button
                            key={val}
                            type="button"
                            disabled={isFinalized}
                            onClick={() => handleScoreChange(q.id, val)}
                            className={`w-7 h-7 rounded text-xs font-bold transition cursor-pointer flex items-center justify-center ${
                              isSelected
                                ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/40 ring-2 ring-indigo-400 scale-105'
                                : 'bg-slate-800 text-slate-400 hover:bg-slate-700 hover:text-slate-200'
                            } ${isFinalized ? 'cursor-not-allowed opacity-80' : ''}`}
                          >
                            {val}
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>

              {!isExcluded && (
                <div className="space-y-3 bg-slate-900/50 p-4 rounded-lg border border-slate-800/80">
                  {prop?.is_rejected && (
                    <div className="p-3 bg-rose-950/60 border border-rose-800 rounded-lg text-rose-200 text-xs space-y-1.5">
                      <div className="font-bold flex items-center space-x-1.5 text-rose-300">
                        <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0" />
                        <span>AI-предложение отклонено: ошибки валидации доказательств</span>
                      </div>
                      {prop.validation_errors && prop.validation_errors.length > 0 ? (
                        <ul className="list-disc list-inside space-y-0.5 text-rose-300/90 pl-1 text-[11px]">
                          {prop.validation_errors.map((err, errIdx) => (
                            <li key={errIdx}>{err}</li>
                          ))}
                        </ul>
                      ) : (
                        <p className="text-[11px] text-rose-300/90">
                          Цитаты модели не найдены в стенограмме кандидата или выходят за рамки критериев.
                        </p>
                      )}
                      <p className="text-[11px] text-rose-400/80 italic">
                        Автоодобрение заблокировано (409 Conflict). Пожалуйста, выставьте баллы и сохраните экспертную оценку вручную.
                      </p>
                    </div>
                  )}

                  <p className="text-xs text-slate-300 leading-relaxed">
                    {prop?.scores?.[0]?.explanation ||
                      (currentScore !== undefined
                        ? 'Оценка выставлена экспертом-интервьюером вручную.'
                        : 'Оценка по этому вопросу отсутствует. Выберите балл выше или запустите автооценку.')}
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

                  {/* Per-Criterion Interactive Scoring */}
                  {q.criteria && q.criteria.length > 0 && (
                    <div className="space-y-2 pt-2 border-t border-slate-800">
                      <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">
                        Оценка критериев вопроса ({q.criteria.length}):
                      </span>
                      {q.criteria.map((crit) => {
                        const cScore = criterionScores[q.id]?.[crit.id];
                        return (
                          <div
                            key={crit.id}
                            className="flex items-center justify-between p-2.5 rounded-lg bg-slate-950/70 border border-slate-800/80"
                          >
                            <div className="pr-3">
                              <div className="flex items-center space-x-2">
                                <span className="text-xs font-semibold text-slate-200">{crit.title}</span>
                                <span className="text-[10px] text-slate-500 font-mono">
                                  вес: {crit.weight} • шкала: [{crit.min_score}–{crit.max_score}]
                                </span>
                              </div>
                              {crit.description && (
                                <p className="text-[11px] text-slate-400 mt-0.5">{crit.description}</p>
                              )}
                            </div>
                            <div className="flex items-center space-x-1 bg-slate-900 px-2 py-1 rounded-lg border border-slate-800">
                              {[1, 2, 3, 4, 5].map((val) => {
                                const isCritSelected = cScore === val;
                                return (
                                  <button
                                    key={val}
                                    type="button"
                                    disabled={isFinalized}
                                    onClick={() => handleCriterionScoreChange(q.id, crit.id, val)}
                                    className={`w-6 h-6 rounded text-[11px] font-bold transition cursor-pointer flex items-center justify-center ${
                                      isCritSelected
                                        ? 'bg-indigo-600 text-white shadow ring-1 ring-indigo-400'
                                        : 'bg-slate-800 text-slate-400 hover:bg-slate-700 hover:text-slate-200'
                                    } ${isFinalized ? 'cursor-not-allowed opacity-80' : ''}`}
                                  >
                                    {val}
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {/* Reviewer Note Input */}
                  {!isFinalized && (
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
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* 2. Executive Summary & Hiring Recommendation */}
      <div className="glass-panel p-6 rounded-xl space-y-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400">
              2. Итоговое резюме и решение по найму (Executive Decision)
            </h3>
            {isSummaryConfirmed ? (
              <span className="px-2 py-0.5 text-[10px] font-semibold text-emerald-400 bg-emerald-950/60 border border-emerald-800/60 rounded flex items-center space-x-1">
                <Check className="w-3 h-3" />
                <span>Подтверждено экспертом</span>
              </span>
            ) : (
              <span className="px-2 py-0.5 text-[10px] font-semibold text-amber-400 bg-amber-950/60 border border-amber-800/60 rounded">
                Требует подтверждения
              </span>
            )}
          </div>
        </div>

        <div className="space-y-4">
          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-1.5">
              Резюме встречи (Executive Summary):
            </label>
            <textarea
              rows={4}
              disabled={isFinalized}
              value={summaryMarkdown}
              onChange={(e) => {
                setSummaryMarkdown(e.target.value);
                setIsSummaryConfirmed(false);
              }}
              placeholder="Введите профессиональное резюме результатов кандидата, ключевые сильные стороны и риски..."
              className="w-full bg-slate-950 border border-slate-800 rounded-lg p-3 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 leading-relaxed font-sans disabled:opacity-75 disabled:cursor-not-allowed"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-1.5">
              Рекомендация по найму (Hiring Recommendation):
            </label>
            <div className="grid grid-cols-5 gap-2">
              {[
                { id: 'STRONG_HIRE', label: 'Strong Hire', color: 'border-emerald-500 text-emerald-300 hover:bg-emerald-950/40' },
                { id: 'HIRE', label: 'Hire', color: 'border-teal-500 text-teal-300 hover:bg-teal-950/40' },
                { id: 'LEAN_HIRE', label: 'Lean Hire', color: 'border-cyan-500 text-cyan-300 hover:bg-cyan-950/40' },
                { id: 'LEAN_NO_HIRE', label: 'Lean No Hire', color: 'border-amber-500 text-amber-300 hover:bg-amber-950/40' },
                { id: 'NO_HIRE', label: 'No Hire', color: 'border-rose-500 text-rose-300 hover:bg-rose-950/40' },
              ].map((opt) => {
                const isSelected = hiringRecommendation === opt.id;
                return (
                  <button
                    key={opt.id}
                    type="button"
                    disabled={isFinalized}
                    onClick={() => {
                      setHiringRecommendation(opt.id);
                      setIsSummaryConfirmed(false);
                    }}
                    className={`p-2.5 rounded-lg border text-xs font-bold transition flex flex-col items-center justify-center cursor-pointer ${opt.color} ${
                      isSelected
                        ? 'bg-slate-800 ring-2 ring-indigo-400 shadow-md'
                        : 'bg-slate-950/60 border-slate-800/80 opacity-60'
                    } ${isFinalized ? 'cursor-not-allowed opacity-80' : ''}`}
                  >
                    <span>{opt.label}</span>
                  </button>
                );
              })}
            </div>
            {!hiringRecommendation && (
              <p className="text-[11px] text-amber-400 mt-1">Решение не выбрано (выберите один из вариантов выше).</p>
            )}
          </div>

          {!isFinalized && (
            <div className="flex justify-end pt-2">
              <button
                type="button"
                onClick={handleSaveSummary}
                disabled={isSavingSummary}
                className="flex items-center space-x-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-semibold rounded-lg shadow transition cursor-pointer"
              >
                {isSavingSummary ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : summarySavedSuccess ? (
                  <CheckCircle2 className="w-3.5 h-3.5 text-emerald-200" />
                ) : (
                  <FileCheck2 className="w-3.5 h-3.5" />
                )}
                <span>{summarySavedSuccess ? 'Резюме сохранено' : 'Подтвердить резюме и решение'}</span>
              </button>
            </div>
          )}
        </div>
      </div>

      {/* 3. Finalization Pre-checks Warning / Blocker Box */}
      {finalizeError && (
        <div className="p-4 bg-rose-950/60 border border-rose-800 rounded-xl space-y-2">
          <div className="flex items-center space-x-2 text-xs font-bold text-rose-300">
            <AlertCircle className="w-4 h-4 text-rose-400" />
            <span>Финализация заблокирована по следующим причинам:</span>
          </div>
          <div className="text-xs text-rose-200 whitespace-pre-line leading-relaxed pl-6">
            • {finalizeError}
          </div>
        </div>
      )}

      {finalizeSuccess && (
        <div className="p-4 bg-emerald-950/60 border border-emerald-800 rounded-xl flex items-center space-x-3 text-xs text-emerald-200">
          <CheckCircle2 className="w-5 h-5 text-emerald-400 shrink-0" />
          <div>
            <div className="font-bold text-slate-100">Отчёт успешно финализирован и зафиксирован!</div>
            <div className="text-[11px] text-slate-400 font-mono mt-0.5">
              SHA-256 Checksum: {finalizedChecksum}
            </div>
          </div>
        </div>
      )}

      {exportError && (
        <div className="p-3 bg-rose-950/70 border border-rose-800 rounded-lg text-xs text-rose-300">
          {exportError}
        </div>
      )}

      {/* Action Footer */}
      <div className="flex items-center justify-between pt-4 pb-12">
        <button
          onClick={onNewInterview}
          className="flex items-center space-x-2 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded-lg transition cursor-pointer"
        >
          <RotateCcw className="w-3.5 h-3.5" />
          <span>Новое собеседование</span>
        </button>

        <div className="flex items-center space-x-3">
          {!isFinalized ? (
            <button
              onClick={handleFinalizeReport}
              disabled={isFinalizing}
              className="flex items-center space-x-2 px-5 py-2.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-semibold rounded-lg shadow-lg shadow-indigo-600/30 transition cursor-pointer disabled:cursor-not-allowed"
            >
              {isFinalizing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Lock className="w-4 h-4" />}
              <span>{isFinalizing ? 'Финализация...' : 'Финализировать отчёт'}</span>
            </button>
          ) : (
            <div className="flex items-center space-x-1.5 px-4 py-2 bg-emerald-950/70 border border-emerald-800 text-emerald-300 text-xs font-semibold rounded-lg">
              <Lock className="w-3.5 h-3.5" />
              <span>Отчёт финализирован</span>
            </div>
          )}

          <button
            onClick={exportJsonReport}
            disabled={isExporting}
            className={`flex items-center space-x-2 px-5 py-2.5 text-white text-xs font-semibold rounded-lg shadow-lg transition cursor-pointer disabled:cursor-not-allowed ${
              isFinalized
                ? 'bg-emerald-600 hover:bg-emerald-500 shadow-emerald-600/30'
                : 'bg-slate-700 hover:bg-slate-600 shadow-slate-700/20'
            }`}
          >
            <Download className="w-4 h-4" />
            <span>
              {isExporting
                ? 'Экспорт...'
                : isFinalized
                ? 'Экспортировать итоговый отчёт (JSON)'
                : 'Экспортировать черновик (JSON)'}
            </span>
          </button>
        </div>
      </div>
    </div>
  );
};
