import React, { useState, useEffect, useCallback } from 'react';
import {
  InterviewPlan,
  AssessmentProposal,
  TranscriptSegment,
  HumanAssessment,
  SpeakerRole,
  ReportRevisionSummary,
} from '../types';
import {
  reviewAssessment,
  getInterview,
  exportInterview,
  enqueueJob,
  getSummary,
  confirmSummary,
  finalizeInterviewReport,
  startBatchRetranscribe,
  getTranscriptDiff,
  getTranscriptRevisions,
  setSegmentSpeakerRole,
  splitSegment,
  reopenInterviewRevision,
  getReportRevisions,
} from '../services/api';
import {
  Check,
  ShieldCheck,
  Download,
  Award,
  RotateCcw,
  ExternalLink,
  FileText,
  Sparkles,
  Loader2,
  CheckCircle2,
  Lock,
  AlertCircle,
  AlertTriangle,
  XCircle,
  FileCheck2,
  RefreshCw,
  GitCompare,
  X,
  Scissors,
  History,
  LayoutDashboard,
  CheckSquare,
  ArrowLeft,
  FileSpreadsheet,
  ThumbsUp,
} from 'lucide-react';

interface DecisionOption {
  id: string;
  labelEn: string;
  labelRu: string;
  tagline: string;
  icon: React.ComponentType<{ className?: string }>;
  colorActive: string;
  colorHover: string;
  accentColor: string;
}

const DECISION_OPTIONS: DecisionOption[] = [
  {
    id: 'STRONG_HIRE',
    labelEn: 'Strong Hire',
    labelRu: 'Уверенный найм',
    tagline: 'Исключительный кандидат, превосходит планку роли',
    icon: Sparkles,
    colorActive: 'bg-emerald-950/80 border-emerald-400 text-emerald-100 ring-2 ring-emerald-500/80 shadow-lg shadow-emerald-950/70',
    colorHover: 'hover:border-emerald-500/60 hover:bg-emerald-950/30 text-slate-300',
    accentColor: 'text-emerald-400',
  },
  {
    id: 'HIRE',
    labelEn: 'Hire',
    labelRu: 'Найм',
    tagline: 'Полностью соответствует профилю требований',
    icon: CheckCircle2,
    colorActive: 'bg-teal-950/80 border-teal-400 text-teal-100 ring-2 ring-teal-500/80 shadow-lg shadow-teal-950/70',
    colorHover: 'hover:border-teal-500/60 hover:bg-teal-950/30 text-slate-300',
    accentColor: 'text-teal-400',
  },
  {
    id: 'LEAN_HIRE',
    labelEn: 'Lean Hire',
    labelRu: 'Скорее найм',
    tagline: 'Проходит по планке, с небольшими оговорками',
    icon: ThumbsUp,
    colorActive: 'bg-cyan-950/80 border-cyan-400 text-cyan-100 ring-2 ring-cyan-500/80 shadow-lg shadow-cyan-950/70',
    colorHover: 'hover:border-cyan-500/60 hover:bg-cyan-950/30 text-slate-300',
    accentColor: 'text-cyan-400',
  },
  {
    id: 'LEAN_NO_HIRE',
    labelEn: 'Lean No Hire',
    labelRu: 'Скорее отказ',
    tagline: 'Существенные риски или пробелы в компетенциях',
    icon: AlertTriangle,
    colorActive: 'bg-amber-950/80 border-amber-400 text-amber-100 ring-2 ring-amber-500/80 shadow-lg shadow-amber-950/70',
    colorHover: 'hover:border-amber-500/60 hover:bg-amber-950/30 text-slate-300',
    accentColor: 'text-amber-400',
  },
  {
    id: 'NO_HIRE',
    labelEn: 'No Hire',
    labelRu: 'Отказ',
    tagline: 'Не соответствует требованиям позиции',
    icon: XCircle,
    colorActive: 'bg-rose-950/80 border-rose-400 text-rose-100 ring-2 ring-rose-500/80 shadow-lg shadow-rose-950/70',
    colorHover: 'hover:border-rose-500/60 hover:bg-rose-950/30 text-slate-300',
    accentColor: 'text-rose-400',
  },
];

type ReviewTab = 'overview' | 'questions' | 'transcript' | 'result' | 'history';

interface ReviewScreenProps {
  interviewId: string;
  plan: InterviewPlan;
  onNewInterview: () => void;
  onBackToHome?: () => void;
}

export const ReviewScreen: React.FC<ReviewScreenProps> = ({
  interviewId,
  plan,
  onNewInterview,
  onBackToHome,
}) => {
  const [activeTab, setActiveTab] = useState<ReviewTab>('overview');

  // Baseline data state
  const [proposals, setProposals] = useState<AssessmentProposal[]>([]);
  const [humanAssessments, setHumanAssessments] = useState<HumanAssessment[]>([]);
  const [questionScores, setQuestionScores] = useState<Record<string, number>>({});
  const [criterionScores, setCriterionScores] = useState<Record<string, Record<string, number>>>({});
  const [segments, setSegments] = useState<TranscriptSegment[]>([]);
  const [activeRevisionId, setActiveRevisionId] = useState<string>('trans-rev-1');
  const [availableRevisions, setAvailableRevisions] = useState<Array<{
    revision_id: string;
    segment_count: number;
    is_active: boolean;
  }>>([]);
  const [isRetranscribing, setIsRetranscribing] = useState(false);
  const [retranscribeSuccess, setRetranscribeSuccess] = useState<string | null>(null);
  const [retranscribeError, setRetranscribeError] = useState<string | null>(null);
  const [diffData, setDiffData] = useState<any | null>(null);
  const [showDiffModal, setShowDiffModal] = useState(false);
  const [isLoadingDiff, setIsLoadingDiff] = useState(false);
  const [serverScoring, setServerScoring] = useState<{
    final_score_100: number | null;
    coverage_percentage: number;
  } | null>(null);
  const [interviewMetadata, setInterviewMetadata] = useState<any | null>(null);

  // Executive summary & hiring decision state
  const [summaryMarkdown, setSummaryMarkdown] = useState<string>('');
  const [hiringRecommendation, setHiringRecommendation] = useState<string>('');
  const [isSummaryConfirmed, setIsSummaryConfirmed] = useState(false);
  const [isSavingSummary, setIsSavingSummary] = useState(false);
  const [summarySavedSuccess, setSummarySavedSuccess] = useState(false);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [aiSummaryData, setAiSummaryData] = useState<any>(null);

  // Lifecycle & Finalization state
  const [interviewStatus, setInterviewStatus] = useState<string>('review');
  const [finalizedChecksum, setFinalizedChecksum] = useState<string | null>(null);
  const [isFinalizing, setIsFinalizing] = useState(false);
  const [finalizeError, setFinalizeError] = useState<string | null>(null);
  const [finalizeSuccess, setFinalizeSuccess] = useState(false);

  // Reopen Interview Revision Modal state
  const [showReopenModal, setShowReopenModal] = useState(false);
  const [reopenReviewer, setReopenReviewer] = useState('lead-interviewer');
  const [reopenReason, setReopenReason] = useState('');
  const [isReopening, setIsReopening] = useState(false);
  const [reopenError, setReopenError] = useState<string | null>(null);
  const [reopenSuccessMsg, setReopenSuccessMsg] = useState<string | null>(null);

  // Report revisions history
  const [reportRevisions, setReportRevisions] = useState<ReportRevisionSummary[]>([]);
  const [isLoadingRevisions, setIsLoadingRevisions] = useState(false);
  const [exportingRevNumber, setExportingRevNumber] = useState<number | null>(null);

  // Exclusions map: question_id -> { isExcluded: boolean; reason: string }
  const [excludedQuestions, setExcludedQuestions] = useState<Record<string, { isExcluded: boolean; reason: string }>>({});
  const [excludingQuestionModal, setExcludingQuestionModal] = useState<{ questionId: string; questionText: string } | null>(null);
  const [exclusionReasonInput, setExclusionReasonInput] = useState('');
  const [isSubmittingExclusion, setIsSubmittingExclusion] = useState(false);

  const [selectedQuote, setSelectedQuote] = useState<{ quote: string; segmentId: string } | null>(null);
  const [reviewerNotes, setReviewerNotes] = useState<Record<string, string>>({});
  const [savedSuccess, setSavedSuccess] = useState<Record<string, boolean>>({});
  const [isSavingQuestion, setIsSavingQuestion] = useState<Record<string, boolean>>({});
  const [isConfirmingAll, setIsConfirmingAll] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [isEvaluatingAll, setIsEvaluatingAll] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [evalWarning, setEvalWarning] = useState<string | null>(null);

  // Split Segment modal
  const [splittingSegment, setSplittingSegment] = useState<TranscriptSegment | null>(null);
  const [splitTimeSec, setSplitTimeSec] = useState<number>(0);
  const [splitText1, setSplitText1] = useState<string>('');
  const [splitText2, setSplitText2] = useState<string>('');
  const [splitRole1, setSplitRole1] = useState<SpeakerRole>('interviewer');
  const [splitRole2, setSplitRole2] = useState<SpeakerRole>('candidate');
  const [isUpdatingRole, setIsUpdatingRole] = useState<string | null>(null);

  const isFinalized = interviewStatus === 'finalized';

  const loadReportRevisions = useCallback(async () => {
    setIsLoadingRevisions(true);
    try {
      const revs = await getReportRevisions(interviewId);
      setReportRevisions(revs || []);
    } catch (e) {
      console.warn('Failed to load report revisions:', e);
    } finally {
      setIsLoadingRevisions(false);
    }
  }, [interviewId]);

  const loadData = useCallback(async () => {
    try {
      const data = await getInterview(interviewId);
      const existingProps: AssessmentProposal[] = data.assessment_proposals || [];
      const existingHuman: HumanAssessment[] = (data.human_assessments as HumanAssessment[]) || [];
      const initialScores: Record<string, number> = {};
      const initialCritScores: Record<string, Record<string, number>> = {};
      const initialNotes: Record<string, string> = {};
      const exclusionsMap: Record<string, { isExcluded: boolean; reason: string }> = {};

      if (data.interview) {
        setInterviewMetadata(data.interview);
        if (data.interview.status) {
          setInterviewStatus(data.interview.status);
        }
        if (data.interview.finalized_checksum) {
          setFinalizedChecksum(data.interview.finalized_checksum);
        }
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
          setAiSummaryData(sumData.summary || null);
          if (sumData.confirmed_markdown) {
            setSummaryMarkdown(sumData.confirmed_markdown);
          } else if (sumData.summary?.summary_markdown) {
            setSummaryMarkdown(sumData.summary.summary_markdown);
          } else if (sumData.summary?.overview) {
            setSummaryMarkdown(sumData.summary.overview);
          }
          if (sumData.confirmed_recommendation) {
            setHiringRecommendation(sumData.confirmed_recommendation);
          } else if (sumData.summary?.hiring_recommendation) {
            setHiringRecommendation(sumData.summary.hiring_recommendation);
          }
        }
      } catch (sumErr) {
        console.warn('Failed to load executive summary:', sumErr);
      }

      // Load Transcript Revisions
      try {
        const revsData = await getTranscriptRevisions(interviewId);
        if (revsData.revisions) {
          setAvailableRevisions(revsData.revisions);
        }
        if (revsData.active_revision_id) {
          setActiveRevisionId(revsData.active_revision_id);
        }
      } catch (revErr) {
        console.warn('Failed to load transcript revisions:', revErr);
      }

      // Load report revisions
      loadReportRevisions();
    } catch (err) {
      console.warn('Failed to load review data from backend:', err);
    }
  }, [interviewId, loadReportRevisions]);

  useEffect(() => {
    loadData();
  }, [loadData]);

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

  const finalScore =
    serverScoring?.final_score_100 !== undefined && serverScoring?.final_score_100 !== null
      ? Math.round(serverScoring.final_score_100)
      : calculateFinalScore();

  const handleSetRole = async (segId: string, role: SpeakerRole) => {
    try {
      setIsUpdatingRole(segId);
      await setSegmentSpeakerRole(interviewId, segId, role);
      setSegments((prev) =>
        prev.map((s) => (s.id === segId ? { ...s, speaker_role: role } : s))
      );
    } catch (e: any) {
      console.error('Failed to set speaker role:', e);
      alert(`Ошибка изменения роли: ${e.message || e}`);
    } finally {
      setIsUpdatingRole(null);
    }
  };

  const openSplitModal = (seg: TranscriptSegment) => {
    const midTime = Math.round((seg.start_time_ms + seg.end_time_ms) / 2000);
    const words = seg.text.split(' ');
    const half = Math.max(1, Math.ceil(words.length / 2));
    setSplittingSegment(seg);
    setSplitTimeSec(midTime);
    setSplitText1(words.slice(0, half).join(' '));
    setSplitText2(words.slice(half).join(' '));
    setSplitRole1('interviewer');
    setSplitRole2('candidate');
  };

  const handleConfirmSplit = async () => {
    if (!splittingSegment) return;
    try {
      const splitTimeMs = Math.round(splitTimeSec * 1000);
      const res = await splitSegment(interviewId, splittingSegment.id, {
        split_time_ms: splitTimeMs,
        text_part1: splitText1,
        text_part2: splitText2,
        role_part1: splitRole1,
        role_part2: splitRole2,
      });
      setSegments((prev) => {
        const idx = prev.findIndex((s) => s.id === splittingSegment.id);
        if (idx === -1) return prev;
        const copy = [...prev];
        copy.splice(idx, 1, res.segment_part1, res.segment_part2);
        return copy;
      });
      setSplittingSegment(null);
    } catch (e: any) {
      console.error('Failed to split segment:', e);
      alert(`Ошибка разделения сегмента: ${e.message || e}`);
    }
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

  const handleBatchRetranscribe = async () => {
    if (isFinalized || isRetranscribing) return;
    setIsRetranscribing(true);
    setRetranscribeError(null);
    setRetranscribeSuccess(null);
    try {
      let nextRevNum = 2;
      if (availableRevisions && availableRevisions.length > 0) {
        const nums = availableRevisions
          .map((r) => {
            const m = (r.revision_id || '').match(/^trans-rev-(\d+)$/);
            return m ? parseInt(m[1], 10) : 0;
          })
          .filter((n) => !isNaN(n));
        if (nums.length > 0) {
          nextRevNum = Math.max(...nums) + 1;
        }
      }
      const nextRev = `trans-rev-${nextRevNum}`;
      const res = await startBatchRetranscribe(interviewId, nextRev, activeRevisionId);
      setRetranscribeSuccess(`Пакетная перестенограмма успешно завершена! Активная ревизия переключена на: ${res.new_revision_id}`);
      await loadData();
    } catch (err: any) {
      setRetranscribeError(err?.message || 'Ошибка запуска пакетной перестенограммы');
    } finally {
      setIsRetranscribing(false);
    }
  };

  const handleLoadDiff = async () => {
    setIsLoadingDiff(true);
    try {
      const diff = await getTranscriptDiff(interviewId, 'trans-rev-1', activeRevisionId);
      setDiffData(diff);
      setShowDiffModal(true);
    } catch (err: any) {
      alert(`Ошибка загрузки различий ревизий: ${err?.message || err}`);
    } finally {
      setIsLoadingDiff(false);
    }
  };

  const handleSaveApproval = async (questionId: string, overrideScore?: number) => {
    if (isFinalized) return;
    const q = plan.questions.find((x) => x.id === questionId);
    if (!q) return;

    const prop = proposals.find((p) => p.question_id === questionId);
    const chosenScore =
      overrideScore !== undefined
        ? overrideScore
        : questionScores[questionId] !== undefined
        ? questionScores[questionId]
        : prop?.scores?.[0]?.score;

    if (chosenScore === undefined) {
      alert('Пожалуйста, сначала выберите балл для этого вопроса.');
      return;
    }

    const qCrits = criterionScores[questionId] || {};
    const scoresToSubmit = q.criteria.map((c) => {
      const val = qCrits[c.id] ?? chosenScore;
      return {
        criterion_id: c.id,
        score: val !== undefined ? val : 3.0,
      };
    });

    setIsSavingQuestion((prev) => ({ ...prev, [questionId]: true }));
    try {
      await reviewAssessment(interviewId, questionId, {
        expected_transcript_revision: activeRevisionId,
        scores: scoresToSubmit,
        reviewer_notes: reviewerNotes[questionId] || 'Оценка подтверждена экспертом',
        is_manually_adjusted: true,
      });

      setSavedSuccess((prev) => ({ ...prev, [questionId]: true }));
      setFinalizeError(null);
      setTimeout(() => {
        setSavedSuccess((prev) => ({ ...prev, [questionId]: false }));
      }, 3000);

      await loadData();
    } catch (e: any) {
      console.error('Save review error:', e);
      if (e?.message?.includes('409')) {
        alert('Конфликт версий (409): стенограмма собеседования была изменена в новой ревизии. Данные страницы будут обновлены.');
        await loadData();
      } else {
        alert(`Ошибка при сохранении оценки: ${e?.message || e}`);
      }
    } finally {
      setIsSavingQuestion((prev) => ({ ...prev, [questionId]: false }));
    }
  };

  const handleConfirmAllAssessments = async () => {
    if (isFinalized || isConfirmingAll) return;
    setIsConfirmingAll(true);
    try {
      for (const q of plan.questions) {
        if (excludedQuestions[q.id]?.isExcluded) continue;
        const human = humanAssessments.find((ha) => ha.question_id === q.id);
        if (human && !human.is_stale && human.transcript_revision_id === activeRevisionId) {
          continue;
        }
        const prop = proposals.find((p) => p.question_id === q.id);
        const chosenScore = questionScores[q.id] ?? prop?.scores?.[0]?.score ?? 3.0;
        const qCrits = criterionScores[q.id] || {};
        const scoresToSubmit = q.criteria.map((c) => ({
          criterion_id: c.id,
          score: qCrits[c.id] ?? chosenScore,
        }));

        await reviewAssessment(interviewId, q.id, {
          expected_transcript_revision: activeRevisionId,
          scores: scoresToSubmit,
          reviewer_notes: reviewerNotes[q.id] || 'Оценка подтверждена экспертом',
          is_manually_adjusted: true,
        });
      }
      setFinalizeError(null);
      await loadData();
    } catch (err: any) {
      alert(`Ошибка при подтверждении всех оценок: ${err?.message || err}`);
    } finally {
      setIsConfirmingAll(false);
    }
  };

  const handleOpenExcludeModal = (questionId: string, questionText: string) => {
    if (isFinalized) return;
    setExcludingQuestionModal({ questionId, questionText });
    setExclusionReasonInput('');
  };

  const handleConfirmExclude = async () => {
    if (!excludingQuestionModal) return;
    const reason = exclusionReasonInput.trim();
    if (!reason) {
      alert('Исключение вопроса требует явного указания причины.');
      return;
    }

    const qId = excludingQuestionModal.questionId;
    setIsSubmittingExclusion(true);
    try {
      await reviewAssessment(interviewId, qId, {
        expected_transcript_revision: activeRevisionId,
        scores: [],
        reviewer_notes: `Вопрос исключён экспертом: ${reason}`,
        is_manually_adjusted: true,
        is_excluded: true,
        exclusion_reason: reason,
      });
      setExcludedQuestions((prev) => ({
        ...prev,
        [qId]: { isExcluded: true, reason },
      }));
      setExcludingQuestionModal(null);
      setExclusionReasonInput('');
      await loadData();
    } catch (err: any) {
      alert(`Ошибка при исключении вопроса: ${err?.message || err}`);
    } finally {
      setIsSubmittingExclusion(false);
    }
  };

  const handleUnexcludeQuestion = async (questionId: string) => {
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

    try {
      await reviewAssessment(interviewId, questionId, {
        expected_transcript_revision: activeRevisionId,
        scores: scoresToSubmit,
        reviewer_notes: 'Вопрос возвращён в оценку экспертом',
        is_manually_adjusted: true,
        is_excluded: false,
        exclusion_reason: undefined,
      });
      setExcludedQuestions((prev) => {
        const copy = { ...prev };
        delete copy[questionId];
        return copy;
      });
      await loadData();
    } catch (err: any) {
      alert(`Ошибка при возврате вопроса в оценку: ${err?.message || err}`);
    }
  };

  const handleSaveSummary = async () => {
    if (isFinalized) return;
    setSummaryError(null);

    const trimmedSummary = summaryMarkdown.trim();
    if (!trimmedSummary) {
      setSummaryError('Пожалуйста, введите текст резюме (Executive Summary).');
      return;
    }
    if (!hiringRecommendation) {
      setSummaryError('Пожалуйста, выберите рекомендацию по найму (Hiring Decision).');
      return;
    }

    setIsSavingSummary(true);
    try {
      await confirmSummary(interviewId, {
        reviewer_id: 'lead-interviewer',
        confirmed_markdown: trimmedSummary,
        confirmed_recommendation: hiringRecommendation,
        expected_transcript_revision: activeRevisionId,
      });
      setIsSummaryConfirmed(true);
      setSummarySavedSuccess(true);
      setSummaryError(null);
      setTimeout(() => setSummarySavedSuccess(false), 3000);
      await loadData();
    } catch (err: any) {
      if (err?.message?.includes('409') || err?.message?.includes('Конфликт')) {
        setSummaryError('Конфликт версий (409): стенограмма собеседования была обновлена. Страница перезагрузит актуальную ревизию.');
        await loadData();
      } else {
        setSummaryError(`Ошибка при сохранении резюме: ${err?.message || err}`);
      }
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
      const human = humanAssessments.find((ha) => ha.question_id === q.id);
      if (!human || !human.scores || human.scores.length === 0) {
        blockers.push(`Вопрос «${q.title || q.text}» не оценен человеком (требуется подтверждение оценки или явное исключение).`);
      } else if (human.is_stale || human.transcript_revision_id !== activeRevisionId) {
        blockers.push(`Вопрос «${q.title || q.text}»: оценка устарела после обновления стенограммы (ревизия ${human.transcript_revision_id || 'старая'} != актуальная ${activeRevisionId}). Сохраните оценку повторно.`);
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
        expected_transcript_revision: activeRevisionId,
      });
      setInterviewStatus('finalized');
      setFinalizedChecksum(res.sha256_checksum);
      setFinalizeSuccess(true);
      await loadData();
      await loadReportRevisions();
    } catch (err: any) {
      if (err?.message?.includes('409') || err?.message?.includes('Конфликт')) {
        setFinalizeError('Конфликт финализации (409): обнаружены устаревшие оценки или несовпадение ревизии стенограммы. Данные обновлены, проверьте решения.');
        await loadData();
      } else {
        setFinalizeError(err?.message || 'Ошибка финализации отчёта');
      }
    } finally {
      setIsFinalizing(false);
    }
  };

  const handleConfirmReopen = async () => {
    if (!reopenReason.trim()) {
      setReopenError('Укажите причину переоткрытия интервью для редактирования.');
      return;
    }

    setIsReopening(true);
    setReopenError(null);
    try {
      await reopenInterviewRevision(interviewId, reopenReviewer.trim() || 'lead-interviewer', reopenReason.trim());
      setShowReopenModal(false);
      setReopenReason('');
      setReopenSuccessMsg('Интервью успешно переоткрыто для редактирования результатов. Предыдущая версия отчёта зафиксирована в истории.');
      setTimeout(() => setReopenSuccessMsg(null), 6000);
      setInterviewStatus('review');
      await loadData();
      await loadReportRevisions();
    } catch (err: any) {
      setReopenError(err?.message || 'Ошибка переоткрытия интервью');
    } finally {
      setIsReopening(false);
    }
  };

  const handleAutoEvaluateAll = async () => {
    if (isFinalized) return;
    setEvalWarning(null);
    const candidateSegments = segments.filter(
      (s) => s.speaker_role === 'candidate' || (s.speaker_role !== 'interviewer' && s.track_id === 'candidate')
    );
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

      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        try {
          const data = await getInterview(interviewId);
          if (
            (data.assessment_proposals && data.assessment_proposals.length >= plan.questions.length) ||
            attempts >= 30
          ) {
            clearInterval(poll);
            if (data.assessment_proposals && data.assessment_proposals.length > 0) {
              setProposals(data.assessment_proposals);
              const scores: Record<string, number> = {};
              const critScores: Record<string, Record<string, number>> = {};
              data.assessment_proposals.forEach((p) => {
                const sc = p.reviewed_scores?.[0]?.score ?? p.scores?.[0]?.score;
                if (sc !== undefined) scores[p.question_id] = sc;
                if (p.scores && p.scores.length > 0) {
                  critScores[p.question_id] = {};
                  p.scores.forEach((c) => {
                    if (c.score !== undefined && c.score !== null) {
                      critScores[p.question_id][c.criterion_id] = c.score;
                    }
                  });
                }
              });
              setQuestionScores((prev) => ({ ...prev, ...scores }));
              setCriterionScores((prev) => ({ ...prev, ...critScores }));
            }
            setIsEvaluatingAll(false);
          }
        } catch {
          if (attempts >= 30) {
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

  const exportJsonReport = async (revNumber?: number) => {
    setIsExporting(true);
    setExportError(null);
    try {
      const exportData = await exportInterview(interviewId, revNumber);
      const isFin = isFinalized || (exportData as any)?.status === 'FINALIZED';
      const filePrefix = revNumber
        ? `nebula-verified-report-rev${revNumber}`
        : isFin
        ? 'nebula-verified-report'
        : 'nebula-draft-report';

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

  const handleExportHistoricalRevision = async (revNum: number) => {
    setExportingRevNumber(revNum);
    try {
      await exportJsonReport(revNum);
    } finally {
      setExportingRevNumber(null);
    }
  };

  // Metrics for overview tab
  const assessedQuestionsCount = plan.questions.filter(
    (q) => !excludedQuestions[q.id]?.isExcluded && (questionScores[q.id] !== undefined || humanAssessments.some((ha) => ha.question_id === q.id && !ha.is_excluded && ha.scores?.length))
  ).length;
  const excludedQuestionsCount = Object.values(excludedQuestions).filter((x) => x.isExcluded).length;

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6 overflow-y-auto h-[calc(100vh-4rem)]">
      {/* Top Header & Context */}
      <div className="glass-panel p-5 rounded-2xl flex flex-wrap items-center justify-between gap-4 shadow-xl">
        <div className="flex items-center space-x-4">
          <div className="w-12 h-12 rounded-xl bg-gradient-to-tr from-emerald-600 to-teal-500 flex items-center justify-center shadow-lg shadow-emerald-500/20 shrink-0">
            <Award className="w-7 h-7 text-white" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <h2 className="text-lg font-bold text-slate-100">{plan.title || 'Собеседование'}</h2>
              {isFinalized ? (
                <span className="flex items-center space-x-1 px-2.5 py-0.5 text-xs font-semibold text-emerald-400 bg-emerald-950/80 border border-emerald-700 rounded-full">
                  <Lock className="w-3.5 h-3.5" />
                  <span>Финализировано (Sealed)</span>
                </span>
              ) : (
                <span className="flex items-center space-x-1 px-2.5 py-0.5 text-xs font-semibold text-amber-400 bg-amber-950/60 border border-amber-800/60 rounded-full">
                  <ShieldCheck className="w-3.5 h-3.5" />
                  <span>На проверке</span>
                </span>
              )}
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Роль: <span className="text-slate-200 font-medium">{plan.role}</span> • Сессия: <span className="font-mono text-slate-300">{interviewId}</span>
              {finalizedChecksum && (
                <span className="ml-2 font-mono text-[11px] text-emerald-400">
                  • SHA: {finalizedChecksum.substring(0, 10)}...
                </span>
              )}
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-4">
          <div className="text-right">
            {finalScore !== null ? (
              <>
                <div className="text-3xl font-extrabold text-emerald-400 font-mono tracking-tight">
                  {finalScore} <span className="text-base text-slate-400 font-normal">/ 100</span>
                </div>
                <span className="text-[11px] text-slate-400">
                  {serverScoring
                    ? `Взвешенный балл • Покрытие: ${serverScoring.coverage_percentage}%`
                    : 'Взвешенный балл'}
                </span>
              </>
            ) : (
              <>
                <div className="text-2xl font-bold text-slate-500 font-mono">
                  — <span className="text-sm text-slate-600 font-normal">/ 100</span>
                </div>
                <span className="text-[11px] text-slate-500">Оценки не выставлены</span>
              </>
            )}
          </div>

          <div className="flex items-center space-x-2 border-l border-slate-800 pl-4">
            {isFinalized && (
              <button
                type="button"
                onClick={() => setShowReopenModal(true)}
                className="flex items-center space-x-1.5 px-3 py-2 bg-amber-600/20 hover:bg-amber-600/30 text-amber-300 border border-amber-600/50 rounded-lg text-xs font-semibold transition cursor-pointer"
                title="Переоткрыть интервью для редактирования оценок и создания новой редакции отчёта"
              >
                <RotateCcw className="w-3.5 h-3.5" />
                <span>Редактировать результат</span>
              </button>
            )}

            {onBackToHome && (
              <button
                type="button"
                onClick={onBackToHome}
                className="p-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs transition cursor-pointer"
                title="Вернуться к списку собеседований"
              >
                <ArrowLeft className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>
      </div>

      {reopenSuccessMsg && (
        <div className="p-3 bg-emerald-950/60 border border-emerald-800 text-emerald-300 text-xs rounded-xl flex items-center justify-between shadow-md">
          <div className="flex items-center space-x-2">
            <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
            <span>{reopenSuccessMsg}</span>
          </div>
          <button
            type="button"
            onClick={() => setReopenSuccessMsg(null)}
            className="text-emerald-400 hover:text-emerald-200 cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Tabs Navigation Bar */}
      <div className="flex items-center space-x-1 border-b border-slate-800 pb-2">
        <button
          type="button"
          onClick={() => setActiveTab('overview')}
          className={`flex items-center space-x-2 px-4 py-2 rounded-lg text-xs font-semibold transition cursor-pointer ${
            activeTab === 'overview'
              ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/60'
          }`}
        >
          <LayoutDashboard className="w-3.5 h-3.5" />
          <span>Общее</span>
        </button>

        <button
          type="button"
          onClick={() => setActiveTab('questions')}
          className={`flex items-center space-x-2 px-4 py-2 rounded-lg text-xs font-semibold transition cursor-pointer ${
            activeTab === 'questions'
              ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/60'
          }`}
        >
          <CheckSquare className="w-3.5 h-3.5" />
          <span>Вопросы и ответы</span>
          <span className="px-2 py-0.5 rounded-full text-[10px] leading-tight font-mono font-medium bg-slate-800 text-slate-300 shrink-0">
            {plan.questions.length}
          </span>
        </button>

        <button
          type="button"
          onClick={() => setActiveTab('transcript')}
          className={`flex items-center space-x-2 px-4 py-2 rounded-lg text-xs font-semibold transition cursor-pointer ${
            activeTab === 'transcript'
              ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/60'
          }`}
        >
          <FileText className="w-3.5 h-3.5" />
          <span>Транскрипт</span>
          <span className="px-2 py-0.5 rounded-full text-[10px] leading-tight font-mono font-medium bg-slate-800 text-slate-300 shrink-0">
            {segments.length}
          </span>
        </button>

        <button
          type="button"
          onClick={() => setActiveTab('result')}
          className={`flex items-center space-x-2 px-4 py-2 rounded-lg text-xs font-semibold transition cursor-pointer ${
            activeTab === 'result'
              ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/60'
          }`}
        >
          <Award className="w-3.5 h-3.5" />
          <span>Результат</span>
          {isFinalized && (
            <span className="px-2 py-0.5 rounded-full text-[10px] leading-tight font-medium bg-emerald-950 text-emerald-400 border border-emerald-800 shrink-0">
              Sealed
            </span>
          )}
        </button>

        <button
          type="button"
          onClick={() => setActiveTab('history')}
          className={`flex items-center space-x-2 px-4 py-2 rounded-lg text-xs font-semibold transition cursor-pointer ${
            activeTab === 'history'
              ? 'bg-indigo-600 text-white shadow-md shadow-indigo-600/30'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/60'
          }`}
        >
          <History className="w-3.5 h-3.5" />
          <span>История отчётов</span>
          {reportRevisions.length > 0 && (
            <span className="px-2 py-0.5 rounded-full text-[10px] leading-tight font-mono font-medium bg-slate-800 text-slate-300 shrink-0">
              {reportRevisions.length}
            </span>
          )}
        </button>
      </div>

      {/* ========================================================================= */}
      {/* TAB 1: ОБЩЕЕ (OVERVIEW)                                                   */}
      {/* ========================================================================= */}
      {activeTab === 'overview' && (
        <div className="space-y-6">
          {/* Status banner */}
          {isFinalized ? (
            <div className="p-4 bg-emerald-950/40 border border-emerald-800/80 rounded-xl flex items-start justify-between gap-4">
              <div className="flex items-start space-x-3">
                <Lock className="w-5 h-5 text-emerald-400 shrink-0 mt-0.5" />
                <div>
                  <h4 className="text-sm font-bold text-emerald-300">Отчёт финализирован и зафиксирован</h4>
                  <p className="text-xs text-slate-300 mt-1 leading-relaxed">
                    Данный отчёт опечатан (sealed) криптографической контрольной суммой SHA-256. Редактирование оценок заблокировано.
                    Если необходимо скорректировать оценки или заключение, нажмите «Редактировать результат» — это переведёт собеседование в режим доработки и создаст новую редакцию при повторном утверждении.
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setShowReopenModal(true)}
                className="px-4 py-2 bg-amber-600 hover:bg-amber-500 text-white text-xs font-semibold rounded-lg shadow-md transition cursor-pointer shrink-0"
              >
                Редактировать результат
              </button>
            </div>
          ) : (
            <div className="p-4 bg-indigo-950/40 border border-indigo-800/80 rounded-xl flex items-center justify-between">
              <div className="flex items-center space-x-3">
                <ShieldCheck className="w-5 h-5 text-indigo-400 shrink-0" />
                <div>
                  <h4 className="text-sm font-bold text-indigo-300">Собеседование на этапе проверки</h4>
                  <p className="text-xs text-slate-300 mt-0.5">
                    Выставите оценки на вкладке «Вопросы и ответы», проверьте транскрипт и утвердите итоговый отчёт во вкладке «Результат».
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setActiveTab('questions')}
                className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold rounded-lg shadow-md transition cursor-pointer"
              >
                Перейти к оценкам
              </button>
            </div>
          )}

          {/* Quick Metrics Grid */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div className="glass-panel p-4 rounded-xl space-y-1">
              <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">Всего вопросов</span>
              <div className="text-2xl font-bold text-slate-100">{plan.questions.length}</div>
              <span className="text-[10px] text-slate-500">в утверждённом плане</span>
            </div>

            <div className="glass-panel p-4 rounded-xl space-y-1">
              <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">Оценено вопросов</span>
              <div className="text-2xl font-bold text-indigo-400">
                {assessedQuestionsCount} <span className="text-sm text-slate-500 font-normal">/ {plan.questions.length}</span>
              </div>
              <span className="text-[10px] text-slate-500">
                {excludedQuestionsCount > 0 ? `(${excludedQuestionsCount} искл.)` : 'готово к итогу'}
              </span>
            </div>

            <div className="glass-panel p-4 rounded-xl space-y-1">
              <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">Итоговый балл</span>
              <div className="text-2xl font-bold text-emerald-400 font-mono">
                {finalScore !== null ? `${finalScore} / 100` : '—'}
              </div>
              <span className="text-[10px] text-slate-500">
                Покрытие: {serverScoring?.coverage_percentage || 0}%
              </span>
            </div>

            <div className="glass-panel p-4 rounded-xl space-y-1">
              <span className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">Редакции отчёта</span>
              <div className="text-2xl font-bold text-amber-400">
                {reportRevisions.length > 0 ? `v${reportRevisions.length}` : 'Черновик'}
              </div>
              <span className="text-[10px] text-slate-500">
                {reportRevisions.length > 0 ? `${reportRevisions.length} зафиксировано` : 'ещё не финализирован'}
              </span>
            </div>
          </div>

          {/* Detailed Info Card */}
          <div className="glass-panel p-6 rounded-xl space-y-4">
            <h3 className="text-sm font-bold text-slate-200 uppercase tracking-wider flex items-center space-x-2">
              <FileSpreadsheet className="w-4 h-4 text-indigo-400" />
              <span>Карточка собеседования</span>
            </h3>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
              <div className="p-3 bg-slate-950/70 border border-slate-800/80 rounded-lg space-y-1">
                <span className="text-[10px] text-slate-500 uppercase font-semibold">Кандидат</span>
                <p className="font-semibold text-slate-100 text-sm">{plan.title}</p>
              </div>

              <div className="p-3 bg-slate-950/70 border border-slate-800/80 rounded-lg space-y-1">
                <span className="text-[10px] text-slate-500 uppercase font-semibold">Должность / Профиль</span>
                <p className="font-semibold text-slate-100 text-sm">{plan.role}</p>
              </div>

              <div className="p-3 bg-slate-950/70 border border-slate-800/80 rounded-lg space-y-1">
                <span className="text-[10px] text-slate-500 uppercase font-semibold">Идентификатор сессии</span>
                <p className="font-mono text-slate-300">{interviewId}</p>
              </div>

              <div className="p-3 bg-slate-950/70 border border-slate-800/80 rounded-lg space-y-1">
                <span className="text-[10px] text-slate-500 uppercase font-semibold">Активная стенограмма</span>
                <p className="font-mono text-indigo-300 font-bold">{activeRevisionId}</p>
              </div>

              <div className="p-3 bg-slate-950/70 border border-slate-800/80 rounded-lg space-y-1">
                <span className="text-[10px] text-slate-500 uppercase font-semibold">Дата создания</span>
                <p className="text-slate-300">
                  {interviewMetadata?.created_at
                    ? new Date(interviewMetadata.created_at).toLocaleString('ru-RU')
                    : '—'}
                </p>
              </div>

              <div className="p-3 bg-slate-950/70 border border-slate-800/80 rounded-lg space-y-1">
                <span className="text-[10px] text-slate-500 uppercase font-semibold">Хэш финализации (SHA-256)</span>
                <p className="font-mono text-emerald-400 break-all">
                  {finalizedChecksum || 'Отчёт ещё не финализирован'}
                </p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* TAB 2: ВОПРОСЫ И ОТВЕТЫ (QUESTIONS & ANSWERS)                             */}
      {/* ========================================================================= */}
      {activeTab === 'questions' && (
        <div className="space-y-6">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400">
              Экспертная верификация вопросов ({plan.questions.length})
            </h3>

            {!isFinalized && (
              <div className="flex items-center space-x-2">
                <button
                  type="button"
                  onClick={handleConfirmAllAssessments}
                  disabled={isConfirmingAll}
                  className="flex items-center space-x-2 px-3.5 py-1.5 bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 text-white text-xs font-semibold rounded-lg shadow-sm transition cursor-pointer"
                  title="Подтвердить все выставленные или предложенные AI оценки"
                >
                  {isConfirmingAll ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <ShieldCheck className="w-3.5 h-3.5 text-emerald-200" />
                  )}
                  <span>{isConfirmingAll ? 'Подтверждение...' : 'Подтвердить все оценки'}</span>
                </button>

                <button
                  type="button"
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
            )}
          </div>

          {evalWarning && (
            <div className="p-3 bg-amber-950/40 border border-amber-800 text-amber-200 text-xs rounded-lg">
              {evalWarning}
            </div>
          )}

          {segments.some((s) => (s.speaker_role || (s.track_id === 'shared' ? 'unknown' : s.track_id)) === 'unknown') && (
            <div className="p-3 bg-amber-950/30 border border-amber-800/70 text-amber-200 text-xs rounded-lg flex items-start space-x-2">
              <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0 mt-0.5" />
              <div>
                <strong>Внимание:</strong> В стенограмме обнаружены сегменты без назначенной роли говорящего.
                По правилам Nebula для оценки ответов кандидата используются <em>только</em> сегменты с ролью «Кандидат».
                Вы можете разметить роли во вкладке «Транскрипт».
              </div>
            </div>
          )}

          {plan.questions.map((q, idx) => {
            const prop = [...proposals].reverse().find((p) => p.question_id === q.id);
            const currentScore = questionScores[q.id] ?? prop?.scores?.[0]?.score;
            const isSaved = savedSuccess[q.id];
            const isExcluded = excludedQuestions[q.id]?.isExcluded;
            const exclusionReason = excludedQuestions[q.id]?.reason;
            const humanAssessment = humanAssessments.find((ha) => ha.question_id === q.id);
            const isHumanStale = Boolean(
              humanAssessment &&
                (humanAssessment.is_stale ||
                  (humanAssessment.transcript_revision_id && humanAssessment.transcript_revision_id !== activeRevisionId))
            );
            const hasHuman = Boolean(
              humanAssessment && !isHumanStale && !humanAssessment.is_excluded && humanAssessment.scores && humanAssessment.scores.length > 0
            );

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
                      ) : isHumanStale ? (
                        <span className="px-2 py-0.5 text-[10px] font-semibold text-amber-400 bg-amber-950/60 border border-amber-800/60 rounded flex items-center space-x-1">
                          <AlertTriangle className="w-3 h-3" />
                          <span>Оценка устарела (Stale)</span>
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
                      isExcluded ? (
                        <button
                          type="button"
                          onClick={() => handleUnexcludeQuestion(q.id)}
                          className="px-2.5 py-1 text-[11px] font-medium text-emerald-400 hover:text-emerald-300 bg-emerald-950/40 border border-emerald-800/60 rounded-lg hover:border-emerald-700 transition cursor-pointer flex items-center space-x-1"
                        >
                          <RotateCcw className="w-3 h-3" />
                          <span>Вернуть в оценку</span>
                        </button>
                      ) : (
                        <button
                          type="button"
                          onClick={() => handleOpenExcludeModal(q.id, q.text || (q as any).prompt || '')}
                          className="px-2.5 py-1 text-[11px] font-medium text-slate-400 hover:text-rose-300 bg-slate-900 border border-slate-800 rounded-lg hover:border-rose-800 transition cursor-pointer flex items-center space-x-1"
                        >
                          <XCircle className="w-3 h-3" />
                          <span>Исключить вопрос</span>
                        </button>
                      )
                    )}

                    {!isExcluded && (
                      <div className="flex items-center space-x-2">
                        <div className="flex items-center space-x-2 bg-slate-900 px-3 py-1.5 rounded-lg border border-slate-800">
                          <span className="text-xs text-slate-400 font-medium">Балл (1–5):</span>
                          {[1, 2, 3, 4, 5].map((val) => {
                            const isSelected = currentScore === val;
                            return (
                              <button
                                key={val}
                                type="button"
                                disabled={isFinalized}
                                onClick={() => {
                                  handleScoreChange(q.id, val);
                                  handleSaveApproval(q.id, val);
                                }}
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

                        {!isFinalized && (
                          <button
                            type="button"
                            disabled={isSavingQuestion[q.id]}
                            onClick={() => handleSaveApproval(q.id)}
                            className={`px-3 py-1.5 text-xs font-semibold rounded-lg flex items-center space-x-1.5 transition cursor-pointer shadow-sm ${
                              hasHuman && !isHumanStale
                                ? 'bg-emerald-950/70 text-emerald-300 border border-emerald-700/80 hover:bg-emerald-900/60'
                                : 'bg-indigo-600 text-white hover:bg-indigo-500 shadow-indigo-600/30'
                            }`}
                            title="Подтвердить оценку эксперта"
                          >
                            {isSavingQuestion[q.id] ? (
                              <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : hasHuman && !isHumanStale ? (
                              <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
                            ) : (
                              <Check className="w-3.5 h-3.5" />
                            )}
                            <span>{hasHuman && !isHumanStale ? 'Подтверждено' : 'Подтвердить'}</span>
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </div>

                {!isExcluded && (
                  <div className="space-y-3 bg-slate-900/50 p-4 rounded-lg border border-slate-800/80">
                    {isHumanStale && (
                      <div className="p-3 bg-amber-950/60 border border-amber-800 rounded-lg text-amber-200 text-xs space-y-1.5">
                        <div className="font-bold flex items-center space-x-1.5 text-amber-300">
                          <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
                          <span>Оценка устарела (Stale): стенограмма была обновлена</span>
                        </div>
                        <p className="text-[11px] text-amber-300/90 leading-relaxed">
                          {humanAssessment?.stale_reason ||
                            'Стенограмма собеседования была обновлена новой ревизией. Пожалуйста, проверьте актуальные цитаты кандидата и нажмите «Сохранить» для повторного подтверждения оценки.'}
                        </p>
                        <p className="text-[10px] text-amber-400/80 font-mono">
                          Ревизия оценки: {humanAssessment?.transcript_revision_id || 'предыдущая'} • Активная стенограмма: {activeRevisionId}
                        </p>
                      </div>
                    )}

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
                          Подтверждающий текст кандидата (кликните для перехода в стенограмму):
                        </span>
                        {prop.scores[0].evidence.map((ev, eIdx) => (
                          <div
                            key={eIdx}
                            onClick={() => {
                              setSelectedQuote({ quote: ev.exact_quote, segmentId: ev.segment_id });
                              setActiveTab('transcript');
                              setTimeout(() => {
                                document
                                  .getElementById(`review-seg-${ev.segment_id}`)
                                  ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                              }, 150);
                            }}
                            className="group p-2.5 bg-slate-950 hover:bg-indigo-950/30 border-l-2 border-emerald-500 hover:border-amber-400 rounded text-xs italic text-slate-200 cursor-pointer transition flex items-center justify-between"
                          >
                            <span>«{ev.exact_quote}»</span>
                            <span className="text-[10px] text-indigo-400 group-hover:text-amber-300 font-semibold flex items-center space-x-1 pl-2">
                              <span>В стенограмму</span>
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
      )}

      {/* ========================================================================= */}
      {/* TAB 3: ТРАНСКРИПТ (TRANSCRIPT)                                            */}
      {/* ========================================================================= */}
      {activeTab === 'transcript' && (
        <div className="space-y-4">
          {/* Revision Control & Batch Retranscribe Bar */}
          <div className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 flex flex-wrap items-center justify-between gap-3 shadow-md">
            <div className="flex items-center space-x-3">
              <span className="text-xs font-semibold text-slate-300">Активная стенограмма:</span>
              <span className="px-2.5 py-1 text-xs font-mono font-bold text-indigo-300 bg-indigo-950/80 border border-indigo-700/80 rounded-lg shadow-inner">
                {activeRevisionId}
              </span>
              {availableRevisions.length > 0 && (
                <span className="text-[11px] text-slate-400">
                  (версий в базе: {availableRevisions.length})
                </span>
              )}
            </div>

            <div className="flex items-center space-x-2">
              {!isFinalized && (
                <button
                  type="button"
                  onClick={handleBatchRetranscribe}
                  disabled={isRetranscribing}
                  className="flex items-center space-x-1.5 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 text-xs font-medium rounded-lg transition cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                  title="Запустить повторное STT-распознавание аудиосессии с фиксацией новой ревизии"
                >
                  {isRetranscribing ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin text-indigo-400" />
                  ) : (
                    <RefreshCw className="w-3.5 h-3.5 text-indigo-400" />
                  )}
                  <span>{isRetranscribing ? 'Пакетная STT...' : 'Пакетная перестенограмма'}</span>
                </button>
              )}

              <button
                type="button"
                onClick={handleLoadDiff}
                disabled={isLoadingDiff}
                className="flex items-center space-x-1.5 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 text-xs font-medium rounded-lg transition cursor-pointer disabled:opacity-50"
                title="Просмотреть анализ изменений между ревизиями стенограммы"
              >
                {isLoadingDiff ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin text-amber-400" />
                ) : (
                  <GitCompare className="w-3.5 h-3.5 text-amber-400" />
                )}
                <span>Анализ изменений (Diff)</span>
              </button>
            </div>
          </div>

          {retranscribeSuccess && (
            <div className="p-3 bg-emerald-950/60 border border-emerald-800 text-emerald-300 text-xs rounded-lg flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
                <span>{retranscribeSuccess}</span>
              </div>
              <button
                type="button"
                onClick={() => setRetranscribeSuccess(null)}
                className="text-emerald-400 hover:text-emerald-200 text-xs cursor-pointer"
              >
                Закрыть
              </button>
            </div>
          )}

          {retranscribeError && (
            <div className="p-3 bg-rose-950/60 border border-rose-800 text-rose-300 text-xs rounded-lg flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <AlertCircle className="w-4 h-4 text-rose-400 shrink-0" />
                <span>{retranscribeError}</span>
              </div>
              <button
                type="button"
                onClick={() => setRetranscribeError(null)}
                className="text-rose-400 hover:text-rose-200 text-xs cursor-pointer"
              >
                Закрыть
              </button>
            </div>
          )}

          {/* Transcript Segments List */}
          <div className="p-4 border border-slate-800 rounded-xl space-y-3 bg-slate-950/70 max-h-[60vh] overflow-y-auto">
            {segments.length === 0 ? (
              <p className="text-xs text-slate-500 italic py-6 text-center">
                Стенограмма пуста (аудиозапись не содержала речи или ещё не обработана).
              </p>
            ) : (
              segments.map((s) => {
                const isSelected = selectedQuote?.segmentId === s.id;
                const role = s.speaker_role || (s.track_id === 'candidate' ? 'candidate' : s.track_id === 'interviewer' ? 'interviewer' : 'unknown');
                const isCandidate = role === 'candidate';
                const isInterviewer = role === 'interviewer';
                const isUnknown = role === 'unknown';

                return (
                  <div
                    key={s.id}
                    id={`review-seg-${s.id}`}
                    className={`p-3 rounded-lg text-xs leading-relaxed transition-all ${
                      isSelected
                        ? 'ring-2 ring-amber-400 bg-amber-950/40 text-amber-100 border border-amber-500'
                        : isCandidate
                        ? 'bg-emerald-950/20 text-emerald-100 border border-emerald-900/40'
                        : isInterviewer
                        ? 'bg-indigo-950/20 text-indigo-100 border border-indigo-900/40'
                        : isUnknown
                        ? 'bg-amber-950/20 text-amber-100 border border-amber-900/50'
                        : 'bg-slate-900/40 text-slate-100 border border-slate-800'
                    }`}
                  >
                    <div className="flex items-center justify-between mb-1 text-[10px] text-slate-400">
                      <div className="flex items-center space-x-2">
                        <span
                          className={`font-semibold px-2 py-0.5 rounded text-[11px] ${
                            isCandidate
                              ? 'bg-emerald-900/60 text-emerald-300 border border-emerald-800/50'
                              : isInterviewer
                              ? 'bg-indigo-900/60 text-indigo-300 border border-indigo-800/50'
                              : 'bg-amber-900/70 text-amber-300 border border-amber-700/60 font-medium'
                          }`}
                        >
                          {isCandidate ? 'Кандидат' : isInterviewer ? 'Интервьюер' : 'Общая дорожка (роль не назначена)'}
                        </span>
                        {s.track_id === 'shared' && (
                          <span className="text-[9px] text-slate-500 font-mono">общий источник</span>
                        )}
                      </div>

                      <div className="flex items-center space-x-3">
                        <span className="font-mono text-slate-400">
                          {Math.round(s.start_time_ms / 1000)}с - {Math.round(s.end_time_ms / 1000)}с
                        </span>
                        {!isFinalized && (
                          <div className="flex items-center space-x-1">
                            <button
                              type="button"
                              onClick={() => handleSetRole(s.id, 'candidate')}
                              disabled={isCandidate || isUpdatingRole === s.id}
                              className={`px-2 py-0.5 rounded text-[10px] font-medium transition cursor-pointer ${
                                isCandidate
                                  ? 'bg-emerald-600/30 text-emerald-300 border border-emerald-500/40 cursor-default'
                                  : 'bg-slate-800 hover:bg-emerald-900/50 text-slate-400 hover:text-emerald-200'
                              }`}
                              title="Назначить репликой кандидата"
                            >
                              Кандидат
                            </button>
                            <button
                              type="button"
                              onClick={() => handleSetRole(s.id, 'interviewer')}
                              disabled={isInterviewer || isUpdatingRole === s.id}
                              className={`px-2 py-0.5 rounded text-[10px] font-medium transition cursor-pointer ${
                                isInterviewer
                                  ? 'bg-indigo-600/30 text-indigo-300 border border-indigo-500/40 cursor-default'
                                  : 'bg-slate-800 hover:bg-indigo-900/50 text-slate-400 hover:text-indigo-200'
                              }`}
                              title="Назначить репликой интервьюера"
                            >
                              Интервьюер
                            </button>
                            <button
                              type="button"
                              onClick={() => openSplitModal(s)}
                              className="px-2 py-0.5 rounded text-[10px] bg-slate-800 hover:bg-slate-700 text-slate-300 flex items-center space-x-1 transition cursor-pointer"
                              title="Разделить сегмент на две отдельные реплики"
                            >
                              <Scissors className="w-3 h-3 text-slate-400" />
                              <span>Разделить</span>
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                    <p className="mt-1 text-slate-200">{s.text}</p>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* TAB 4: РЕЗУЛЬТАТ (RESULT)                                                 */}
      {/* ========================================================================= */}
      {activeTab === 'result' && (
        <div className="space-y-6">
          {/* Executive Summary & Decision Card */}
          <div className="glass-panel p-6 rounded-xl space-y-5">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400">
                  Итоговое резюме и решение по найму (Executive Decision)
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

            {/* AI Insights (Strengths, Growth Areas, Rationale) if available */}
            {aiSummaryData && (
              <div className="space-y-3 pt-1 border-b border-slate-800/80 pb-4">
                {aiSummaryData.recommendation_rationale && (
                  <div className="p-3 bg-indigo-950/30 border border-indigo-800/40 rounded-lg text-xs">
                    <div className="flex items-center space-x-1.5 font-semibold text-indigo-300 mb-1">
                      <Sparkles className="w-3.5 h-3.5 text-indigo-400" />
                      <span>Предварительная аналитика AI (Обоснование):</span>
                    </div>
                    <p className="text-slate-300 leading-relaxed">{aiSummaryData.recommendation_rationale}</p>
                  </div>
                )}

                {((aiSummaryData.key_strengths && aiSummaryData.key_strengths.length > 0) ||
                  (aiSummaryData.growth_areas && aiSummaryData.growth_areas.length > 0)) && (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
                    {aiSummaryData.key_strengths && aiSummaryData.key_strengths.length > 0 && (
                      <div className="p-3 bg-emerald-950/20 border border-emerald-800/40 rounded-lg space-y-2">
                        <div className="font-semibold text-emerald-400 flex items-center space-x-1.5">
                          <CheckCircle2 className="w-3.5 h-3.5" />
                          <span>Сильные стороны:</span>
                        </div>
                        <div className="space-y-1.5">
                          {aiSummaryData.key_strengths.map((item: any, idx: number) => (
                            <div key={idx} className="bg-slate-900/60 p-2 rounded border border-slate-800/70">
                              <div className="font-medium text-slate-200">{item.title}</div>
                              {item.description && <div className="text-[11px] text-slate-400 mt-0.5">{item.description}</div>}
                              {item.evidence_quote && (
                                <div className="text-[10px] text-emerald-300/80 italic mt-1 border-l-2 border-emerald-500/50 pl-1.5">
                                  «{item.evidence_quote}»
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {aiSummaryData.growth_areas && aiSummaryData.growth_areas.length > 0 && (
                      <div className="p-3 bg-amber-950/20 border border-amber-800/40 rounded-lg space-y-2">
                        <div className="font-semibold text-amber-400 flex items-center space-x-1.5">
                          <AlertTriangle className="w-3.5 h-3.5" />
                          <span>Зоны роста и риски:</span>
                        </div>
                        <div className="space-y-1.5">
                          {aiSummaryData.growth_areas.map((item: any, idx: number) => (
                            <div key={idx} className="bg-slate-900/60 p-2 rounded border border-slate-800/70">
                              <div className="font-medium text-slate-200">{item.title}</div>
                              {item.description && <div className="text-[11px] text-slate-400 mt-0.5">{item.description}</div>}
                              {item.evidence_quote && (
                                <div className="text-[10px] text-amber-300/80 italic mt-1 border-l-2 border-amber-500/50 pl-1.5">
                                  «{item.evidence_quote}»
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            <div className="space-y-4">
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <label className="block text-xs font-semibold text-slate-300">
                    Резюме встречи (Executive Summary):
                  </label>
                  {aiSummaryData?.summary_markdown && summaryMarkdown !== aiSummaryData.summary_markdown && !isFinalized && (
                    <button
                      type="button"
                      onClick={() => {
                        setSummaryMarkdown(aiSummaryData.summary_markdown);
                        setIsSummaryConfirmed(false);
                        setSummaryError(null);
                      }}
                      className="text-[11px] text-indigo-400 hover:text-indigo-300 flex items-center space-x-1 cursor-pointer transition"
                    >
                      <RotateCcw className="w-3 h-3" />
                      <span>Восстановить черновик от AI</span>
                    </button>
                  )}
                </div>
                <textarea
                  rows={4}
                  disabled={isFinalized}
                  value={summaryMarkdown}
                  onChange={(e) => {
                    setSummaryMarkdown(e.target.value);
                    setIsSummaryConfirmed(false);
                    setSummaryError(null);
                  }}
                  placeholder="Введите профессиональное резюме результатов кандидата, ключевые сильные стороны и риски..."
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg p-3 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 leading-relaxed font-sans disabled:opacity-75 disabled:cursor-not-allowed"
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-300 mb-2">
                  Рекомендация по найму (Hiring Recommendation):
                </label>
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
                  {DECISION_OPTIONS.map((opt) => {
                    const isSelected = hiringRecommendation === opt.id;
                    const IconComp = opt.icon;
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        disabled={isFinalized}
                        onClick={() => {
                          setHiringRecommendation(opt.id);
                          setIsSummaryConfirmed(false);
                          setSummaryError(null);
                        }}
                        className={`relative p-3.5 rounded-xl border transition-all duration-200 text-left flex flex-col justify-between cursor-pointer group ${
                          isSelected
                            ? opt.colorActive
                            : `bg-slate-950/60 border-slate-800/90 ${opt.colorHover}`
                        } ${isFinalized ? 'cursor-not-allowed opacity-80' : ''}`}
                      >
                        <div className="flex items-center justify-between mb-2">
                          <div className={`p-1.5 rounded-lg ${isSelected ? 'bg-white/10' : 'bg-slate-900 group-hover:bg-slate-800'}`}>
                            <IconComp className={`w-4 h-4 ${isSelected ? 'text-white' : opt.accentColor}`} />
                          </div>
                          <div
                            className={`w-4 h-4 rounded-full border flex items-center justify-center transition ${
                              isSelected
                                ? 'border-white bg-white text-slate-950'
                                : 'border-slate-700 bg-slate-900/80'
                            }`}
                          >
                            {isSelected && <span className="w-2 h-2 rounded-full bg-current" />}
                          </div>
                        </div>

                        <div>
                          <div className="text-xs font-bold tracking-tight">
                            {opt.labelEn}
                          </div>
                          <div className={`text-[11px] font-medium mt-0.5 ${isSelected ? 'text-white/90' : 'text-slate-400'}`}>
                            {opt.labelRu}
                          </div>
                          <div className={`text-[10px] mt-1.5 leading-snug line-clamp-2 ${isSelected ? 'text-white/75' : 'text-slate-500'}`}>
                            {opt.tagline}
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
                {!hiringRecommendation && (
                  <p className="text-[11px] text-amber-400 mt-1.5">Решение не выбрано (выберите один из вариантов выше).</p>
                )}
              </div>

              {summaryError && (
                <div className="p-3 bg-rose-950/60 border border-rose-800 rounded-lg flex items-center space-x-2 text-xs text-rose-200">
                  <AlertCircle className="w-4 h-4 text-rose-400 shrink-0" />
                  <span>{summaryError}</span>
                </div>
              )}

              {summarySavedSuccess && (
                <div className="p-3 bg-emerald-950/60 border border-emerald-800 rounded-lg flex items-center space-x-2 text-xs text-emerald-200">
                  <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
                  <span>Резюме и решение по найму успешно подтверждены экспертом.</span>
                </div>
              )}

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

          {/* Finalization Blockers Warning */}
          {finalizeError && (
            <div className="p-4 bg-rose-950/60 border border-rose-800 rounded-xl space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-2 text-xs font-bold text-rose-300">
                  <AlertCircle className="w-4 h-4 text-rose-400 shrink-0" />
                  <span>Финализация заблокирована:</span>
                </div>
                {finalizeError.includes('не оценен человеком') && !isFinalized && (
                  <button
                    type="button"
                    onClick={handleConfirmAllAssessments}
                    disabled={isConfirmingAll}
                    className="flex items-center space-x-1.5 px-3 py-1 bg-emerald-700 hover:bg-emerald-600 text-white text-xs font-semibold rounded-lg shadow transition cursor-pointer"
                  >
                    {isConfirmingAll ? <Loader2 className="w-3 h-3 animate-spin" /> : <ShieldCheck className="w-3 h-3" />}
                    <span>Подтвердить все не оценённые вопросы</span>
                  </button>
                )}
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

          {/* Action Row */}
          <div className="glass-panel p-5 rounded-xl flex items-center justify-between">
            <div className="text-xs text-slate-400">
              {isFinalized ? (
                <span>Отчёт утверждён. Вы можете экспортировать JSON или переоткрыть для правок.</span>
              ) : (
                <span>Все критерии должны быть оценены или исключены перед утверждением отчёта.</span>
              )}
            </div>

            <div className="flex items-center space-x-3">
              {!isFinalized ? (
                <button
                  type="button"
                  onClick={handleFinalizeReport}
                  disabled={isFinalizing}
                  className="flex items-center space-x-2 px-5 py-2.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs font-semibold rounded-lg shadow-lg shadow-indigo-600/30 transition cursor-pointer disabled:cursor-not-allowed"
                >
                  {isFinalizing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Lock className="w-4 h-4" />}
                  <span>{isFinalizing ? 'Финализация...' : 'Утвердить и финализировать отчёт'}</span>
                </button>
              ) : (
                <div className="flex items-center space-x-1.5 px-4 py-2 bg-emerald-950/70 border border-emerald-800 text-emerald-300 text-xs font-semibold rounded-lg">
                  <Lock className="w-3.5 h-3.5" />
                  <span>Отчёт зафиксирован (Sealed)</span>
                </div>
              )}

              <button
                type="button"
                onClick={() => exportJsonReport()}
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
      )}

      {/* ========================================================================= */}
      {/* TAB 5: ИСТОРИЯ (HISTORY)                                                  */}
      {/* ========================================================================= */}
      {activeTab === 'history' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-bold uppercase tracking-wider text-slate-400">
                История редакций отчёта ({reportRevisions.length})
              </h3>
              <p className="text-xs text-slate-500 mt-0.5">
                Каждая финализация фиксирует неизменяемый снимок с хэшем SHA-256. Любую редакцию можно скачать в JSON.
              </p>
            </div>

            <button
              type="button"
              onClick={loadReportRevisions}
              disabled={isLoadingRevisions}
              className="flex items-center space-x-1 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs font-medium transition cursor-pointer"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${isLoadingRevisions ? 'animate-spin text-indigo-400' : ''}`} />
              <span>Обновить</span>
            </button>
          </div>

          {isLoadingRevisions ? (
            <div className="flex items-center justify-center p-12 text-slate-500 text-xs">
              <Loader2 className="w-5 h-5 animate-spin mr-2 text-indigo-400" />
              <span>Загрузка редакций отчёта...</span>
            </div>
          ) : reportRevisions.length === 0 ? (
            <div className="p-8 border border-dashed border-slate-800 rounded-2xl text-center space-y-2 bg-slate-950/30">
              <History className="w-8 h-8 text-slate-600 mx-auto" />
              <div className="text-sm font-semibold text-slate-400">Нет утверждённых редакций</div>
              <p className="text-xs text-slate-500 max-w-md mx-auto">
                Отчёт собеседования пока не был финализирован. Первая зафиксированная редакция появится после первого утверждения отчёта.
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              {reportRevisions.map((rev, rIdx) => {
                const isLatest = rIdx === reportRevisions.length - 1;
                return (
                  <div
                    key={rev.id || rev.revision_number}
                    className={`glass-panel p-5 rounded-xl border space-y-3 transition ${
                      isLatest ? 'border-emerald-700/60 bg-emerald-950/10' : 'border-slate-800 bg-slate-900/40'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <div className="flex items-center space-x-3">
                        <span className="text-sm font-bold text-slate-100 font-mono">
                          Редакция #{rev.revision_number}
                        </span>
                        {isLatest ? (
                          <span className="px-2 py-0.5 text-[10px] font-semibold text-emerald-300 bg-emerald-950/80 border border-emerald-700/80 rounded-full">
                            Актуальная редакция
                          </span>
                        ) : (
                          <span className="px-2 py-0.5 text-[10px] font-semibold text-slate-400 bg-slate-800 rounded-full">
                            Архивная редакция
                          </span>
                        )}
                        {rev.hiring_recommendation && (
                          <span className="px-2 py-0.5 text-[10px] font-semibold text-indigo-300 bg-indigo-950/60 border border-indigo-800/60 rounded">
                            {rev.hiring_recommendation}
                          </span>
                        )}
                      </div>

                      <button
                        type="button"
                        onClick={() => handleExportHistoricalRevision(rev.revision_number)}
                        disabled={exportingRevNumber === rev.revision_number}
                        className="flex items-center space-x-1.5 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-lg transition cursor-pointer"
                      >
                        {exportingRevNumber === rev.revision_number ? (
                          <Loader2 className="w-3.5 h-3.5 animate-spin text-indigo-400" />
                        ) : (
                          <Download className="w-3.5 h-3.5 text-indigo-400" />
                        )}
                        <span>Экспорт ревизии #{rev.revision_number}</span>
                      </button>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-4 gap-2 text-xs">
                      <div className="p-2.5 bg-slate-950/70 rounded-lg border border-slate-800/60">
                        <span className="text-[10px] text-slate-500 uppercase font-semibold block">Дата фиксации</span>
                        <span className="text-slate-300 font-medium">
                          {rev.created_at ? new Date(rev.created_at).toLocaleString('ru-RU') : '—'}
                        </span>
                      </div>

                      <div className="p-2.5 bg-slate-950/70 rounded-lg border border-slate-800/60">
                        <span className="text-[10px] text-slate-500 uppercase font-semibold block">Утвердил</span>
                        <span className="text-slate-300 font-medium">{rev.confirmed_by || 'lead-interviewer'}</span>
                      </div>

                      <div className="p-2.5 bg-slate-950/70 rounded-lg border border-slate-800/60">
                        <span className="text-[10px] text-slate-500 uppercase font-semibold block">Итоговый балл</span>
                        <span className="font-mono text-emerald-400 font-bold">
                          {rev.final_score_100 !== null && rev.final_score_100 !== undefined
                            ? `${rev.final_score_100} / 100`
                            : '—'}{' '}
                          <span className="text-[10px] text-slate-400 font-normal">
                            ({rev.coverage_percentage || 0}%)
                          </span>
                        </span>
                      </div>

                      <div className="p-2.5 bg-slate-950/70 rounded-lg border border-slate-800/60">
                        <span className="text-[10px] text-slate-500 uppercase font-semibold block">SHA-256</span>
                        <span className="font-mono text-[11px] text-slate-400 truncate block" title={rev.sha256_checksum}>
                          {rev.sha256_checksum ? rev.sha256_checksum.substring(0, 14) + '...' : '—'}
                        </span>
                      </div>
                    </div>

                    {rev.reopen_reason && (
                      <div className="p-2.5 bg-amber-950/30 border border-amber-800/50 rounded-lg text-xs text-amber-300">
                        <strong>Причина переоткрытия перед этой ревизией:</strong> {rev.reopen_reason}
                      </div>
                    )}

                    {rev.summary_markdown && (
                      <div className="p-3 bg-slate-950/50 rounded-lg border border-slate-800/50 text-xs text-slate-300 space-y-1">
                        <span className="text-[10px] text-slate-500 uppercase font-semibold block">Резюме редакции</span>
                        <p className="line-clamp-2 leading-relaxed text-slate-300/90">{rev.summary_markdown}</p>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {exportError && (
        <div className="p-3 bg-rose-950/70 border border-rose-800 rounded-lg text-xs text-rose-300 flex items-center justify-between">
          <span>{exportError}</span>
          <button
            type="button"
            onClick={() => setExportError(null)}
            className="text-rose-400 hover:text-rose-200 text-xs cursor-pointer ml-2"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Footer Navigation */}
      <div className="flex items-center justify-between pt-4 pb-8 border-t border-slate-800">
        <button
          type="button"
          onClick={onNewInterview}
          className="flex items-center space-x-2 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded-lg transition cursor-pointer"
        >
          <RotateCcw className="w-3.5 h-3.5" />
          <span>Новое собеседование</span>
        </button>

        <div className="flex items-center space-x-3">
          {onBackToHome && (
            <button
              type="button"
              onClick={onBackToHome}
              className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded-lg transition cursor-pointer"
            >
              К списку собеседований
            </button>
          )}

          <button
            type="button"
            onClick={() => exportJsonReport()}
            disabled={isExporting}
            className={`flex items-center space-x-2 px-4 py-2 text-white text-xs font-semibold rounded-lg shadow transition cursor-pointer disabled:opacity-50 ${
              isFinalized ? 'bg-emerald-600 hover:bg-emerald-500' : 'bg-slate-700 hover:bg-slate-600'
            }`}
          >
            <Download className="w-3.5 h-3.5" />
            <span>{isExporting ? 'Экспорт...' : 'Экспорт отчёта'}</span>
          </button>
        </div>
      </div>

      {/* ========================================================================= */}
      {/* MODAL: REOPEN INTERVIEW REVISION                                          */}
      {/* ========================================================================= */}
      {showReopenModal && (
        <div className="fixed inset-0 bg-black/75 backdrop-blur-xs z-50 flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl max-w-lg w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2 text-amber-400">
                <RotateCcw className="w-5 h-5" />
                <h3 className="text-base font-bold text-white">Редактировать результат</h3>
              </div>
              <button
                type="button"
                onClick={() => setShowReopenModal(false)}
                className="text-slate-400 hover:text-white cursor-pointer"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="p-3 bg-amber-950/40 border border-amber-800/80 rounded-lg text-xs text-amber-200 leading-relaxed">
              Собеседование перейдёт из статуса «Завершено» обратно в «На проверке». Текущая версия отчёта сохранится в истории.
              После внесения правок вы сможете утвердить новую редакцию отчёта.
            </div>

            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-slate-300 mb-1">
                  Проверяющий (Reviewer ID):
                </label>
                <input
                  type="text"
                  value={reopenReviewer}
                  onChange={(e) => setReopenReviewer(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-slate-100 focus:outline-none focus:border-amber-500"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-300 mb-1">
                  Причина переоткрытия (обязательно):
                </label>
                <textarea
                  rows={3}
                  value={reopenReason}
                  onChange={(e) => setReopenReason(e.target.value)}
                  placeholder="Опишите причину изменений (например: реклассификация ответа на вопрос #3 после апелляции)..."
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg p-3 text-xs text-slate-100 focus:outline-none focus:border-amber-500 leading-relaxed"
                />
              </div>

              {reopenError && (
                <div className="p-2.5 bg-rose-950/80 border border-rose-800 rounded-lg text-xs text-rose-300 flex items-center space-x-2">
                  <AlertCircle className="w-4 h-4 text-rose-400 shrink-0" />
                  <span>{reopenError}</span>
                </div>
              )}
            </div>

            <div className="flex items-center justify-end space-x-3 pt-2">
              <button
                type="button"
                onClick={() => setShowReopenModal(false)}
                className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium rounded-lg transition cursor-pointer"
              >
                Отмена
              </button>
              <button
                type="button"
                onClick={handleConfirmReopen}
                disabled={isReopening || !reopenReason.trim()}
                className="px-5 py-2 bg-amber-600 hover:bg-amber-500 disabled:opacity-50 text-white text-xs font-bold rounded-lg shadow-md transition cursor-pointer flex items-center space-x-1.5"
              >
                {isReopening ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    <span>Переоткрытие...</span>
                  </>
                ) : (
                  <span>Переоткрыть для правок</span>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* MODAL: DIFF INSPECTION                                                    */}
      {/* ========================================================================= */}
      {showDiffModal && diffData && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-xs p-4">
          <div className="glass-panel w-full max-w-2xl bg-slate-900 border border-slate-800 rounded-2xl shadow-2xl p-6 space-y-4 max-h-[85vh] flex flex-col">
            <div className="flex items-center justify-between border-b border-slate-800 pb-3">
              <div className="flex items-center space-x-2">
                <GitCompare className="w-5 h-5 text-amber-400" />
                <h3 className="text-base font-bold text-slate-100">
                  Сравнение ревизий стенограммы
                </h3>
              </div>
              <button
                type="button"
                onClick={() => setShowDiffModal(false)}
                className="p-1 text-slate-400 hover:text-slate-200 rounded-lg hover:bg-slate-800 transition cursor-pointer"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="text-xs text-slate-300 space-y-2">
              <div className="grid grid-cols-3 gap-2 bg-slate-950/80 p-3 rounded-lg border border-slate-800 text-center">
                <div>
                  <span className="text-[10px] text-slate-500 uppercase font-semibold block">Базовая ревизия</span>
                  <span className="font-mono text-indigo-300 font-bold">{diffData.from_revision}</span>
                </div>
                <div>
                  <span className="text-[10px] text-slate-500 uppercase font-semibold block">Сравниваемая</span>
                  <span className="font-mono text-emerald-300 font-bold">{diffData.to_revision}</span>
                </div>
                <div>
                  <span className="text-[10px] text-slate-500 uppercase font-semibold block">Изменено сегментов</span>
                  <span className="font-mono text-amber-300 font-bold">{diffData.total_segment_diffs}</span>
                </div>
              </div>
            </div>

            <div className="overflow-y-auto space-y-3 flex-1 pr-1">
              <span className="text-xs font-bold text-slate-400 uppercase tracking-wider block">
                Влияние на вопросы ({diffData.question_reports?.length || 0}):
              </span>
              {diffData.question_reports && diffData.question_reports.length > 0 ? (
                diffData.question_reports.map((qr: any) => {
                  const q = plan.questions.find((x) => x.id === qr.question_id);
                  return (
                    <div
                      key={qr.question_id}
                      className={`p-3 rounded-lg border text-xs space-y-1.5 ${
                        qr.is_modified
                          ? 'bg-amber-950/30 border-amber-800/60 text-amber-200'
                          : 'bg-slate-950/40 border-slate-800 text-slate-300'
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-semibold text-slate-200">
                          {q ? q.title || q.text : qr.question_id}
                        </span>
                        {qr.is_modified ? (
                          <span className="px-2 py-0.5 text-[10px] font-bold text-amber-400 bg-amber-950/80 border border-amber-800 rounded">
                            Изменено (Ratio: {Math.round((qr.diff_ratio || 0) * 100)}%)
                          </span>
                        ) : (
                          <span className="px-2 py-0.5 text-[10px] font-semibold text-slate-400 bg-slate-800 rounded">
                            Без изменений
                          </span>
                        )}
                      </div>
                      {qr.stale_reason && (
                        <p className="text-[11px] text-amber-300/90 italic">
                          Причина инвалидации: {qr.stale_reason}
                        </p>
                      )}
                      <div className="flex items-center space-x-3 text-[10px] text-slate-400 font-mono">
                        <span>Сломанных цитат: {qr.broken_evidence_count || 0}</span>
                        <span>•</span>
                        <span>Измененных сегментов: {qr.changed_segments_count || 0}</span>
                      </div>
                    </div>
                  );
                })
              ) : (
                <p className="text-xs text-slate-500 italic py-2">
                  Изменений, влияющих на вопросы, не зафиксировано.
                </p>
              )}
            </div>

            <div className="flex justify-end pt-2 border-t border-slate-800">
              <button
                type="button"
                onClick={() => setShowDiffModal(false)}
                className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold rounded-lg transition cursor-pointer"
              >
                Закрыть
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* MODAL: SPLIT SEGMENT                                                      */}
      {/* ========================================================================= */}
      {splittingSegment && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-800 rounded-xl max-w-xl w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2 text-indigo-400">
                <Scissors className="w-5 h-5" />
                <h3 className="text-base font-semibold text-white">Разделение сегмента речи</h3>
              </div>
              <button
                type="button"
                onClick={() => setSplittingSegment(null)}
                className="text-slate-400 hover:text-white cursor-pointer"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <p className="text-xs text-slate-400">
              Исходный интервал: {Math.round(splittingSegment.start_time_ms / 1000)}с - {Math.round(splittingSegment.end_time_ms / 1000)}с.
              Укажите время границы разделения и распределите текст и роли участников.
            </p>

            <div className="space-y-1">
              <label className="block text-xs font-medium text-slate-300">
                Время разделения (секунды):
              </label>
              <input
                type="number"
                step="0.5"
                min={Math.ceil(splittingSegment.start_time_ms / 1000)}
                max={Math.floor(splittingSegment.end_time_ms / 1000)}
                value={splitTimeSec}
                onChange={(e) => setSplitTimeSec(parseFloat(e.target.value) || 0)}
                className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-100"
              />
            </div>

            {/* Part 1 */}
            <div className="p-3 bg-slate-950/70 border border-slate-800 rounded-lg space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-300">
                  Часть 1 ({Math.round(splittingSegment.start_time_ms / 1000)}с - {splitTimeSec}с)
                </span>
                <select
                  value={splitRole1}
                  onChange={(e) => setSplitRole1(e.target.value as SpeakerRole)}
                  className="bg-slate-900 border border-slate-700 rounded px-2 py-0.5 text-xs text-slate-200"
                >
                  <option value="interviewer">Интервьюер</option>
                  <option value="candidate">Кандидат</option>
                  <option value="unknown">Не назначено</option>
                </select>
              </div>
              <textarea
                value={splitText1}
                onChange={(e) => setSplitText1(e.target.value)}
                rows={3}
                className="w-full bg-slate-900 border border-slate-700 rounded p-2 text-xs text-slate-100 focus:outline-none focus:border-indigo-500"
                placeholder="Текст первой реплики..."
              />
            </div>

            {/* Part 2 */}
            <div className="p-3 bg-slate-950/70 border border-slate-800 rounded-lg space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-300">
                  Часть 2 ({splitTimeSec}с - {Math.round(splittingSegment.end_time_ms / 1000)}с)
                </span>
                <select
                  value={splitRole2}
                  onChange={(e) => setSplitRole2(e.target.value as SpeakerRole)}
                  className="bg-slate-900 border border-slate-700 rounded px-2 py-0.5 text-xs text-slate-200"
                >
                  <option value="candidate">Кандидат</option>
                  <option value="interviewer">Интервьюер</option>
                  <option value="unknown">Не назначено</option>
                </select>
              </div>
              <textarea
                value={splitText2}
                onChange={(e) => setSplitText2(e.target.value)}
                rows={3}
                className="w-full bg-slate-900 border border-slate-700 rounded p-2 text-xs text-slate-100 focus:outline-none focus:border-indigo-500"
                placeholder="Текст второй реплики..."
              />
            </div>

            <div className="flex items-center justify-end space-x-3 pt-2">
              <button
                type="button"
                onClick={() => setSplittingSegment(null)}
                className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs rounded-lg font-medium transition cursor-pointer"
              >
                Отмена
              </button>
              <button
                type="button"
                onClick={handleConfirmSplit}
                disabled={!splitText1.trim() || !splitText2.trim()}
                className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-xs rounded-lg font-medium shadow-md shadow-indigo-600/30 transition cursor-pointer"
              >
                Применить разделение
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================================= */}
      {/* MODAL: EXCLUDE QUESTION                                                   */}
      {/* ========================================================================= */}
      {excludingQuestionModal && (
        <div className="fixed inset-0 bg-black/75 backdrop-blur-xs z-50 flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-800 rounded-xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2 text-rose-400">
                <XCircle className="w-5 h-5" />
                <h3 className="text-base font-semibold text-white">Исключить вопрос из оценки</h3>
              </div>
              <button
                type="button"
                onClick={() => setExcludingQuestionModal(null)}
                className="text-slate-400 hover:text-white cursor-pointer"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="p-3 bg-slate-950/80 border border-slate-800 rounded-lg space-y-1">
              <span className="text-[11px] font-semibold text-indigo-400">Вопрос:</span>
              <p className="text-xs text-slate-200 line-clamp-3">{excludingQuestionModal.questionText}</p>
            </div>

            <p className="text-xs text-slate-400 leading-relaxed">
              По регламенту Nebula исключение вопроса требует обязательного указания обоснованной причины эксперта. Вопрос не будет влиять на итоговый скоринг и покрытие.
            </p>

            <div className="space-y-1.5">
              <label className="block text-xs font-medium text-slate-300">
                Быстрый выбор типовой причины:
              </label>
              <div className="flex flex-wrap gap-1.5">
                {[
                  'Не успели обсудить (нехватка времени)',
                  'Тема не входит в специализацию кандидата',
                  'Технический сбой связи / аудио',
                  'Вопрос пропущен по согласованию',
                ].map((quickReason) => (
                  <button
                    key={quickReason}
                    type="button"
                    onClick={() => setExclusionReasonInput(quickReason)}
                    className={`px-2 py-1 text-[11px] rounded border transition cursor-pointer text-left ${
                      exclusionReasonInput === quickReason
                        ? 'bg-rose-950/80 border-rose-600 text-rose-200'
                        : 'bg-slate-950 border-slate-800 text-slate-400 hover:text-slate-200 hover:border-slate-700'
                    }`}
                  >
                    {quickReason}
                  </button>
                ))}
              </div>
            </div>

            <div className="space-y-1">
              <label className="block text-xs font-medium text-slate-300">
                Причина исключения (обязательно):
              </label>
              <textarea
                value={exclusionReasonInput}
                onChange={(e) => setExclusionReasonInput(e.target.value)}
                rows={3}
                placeholder="Опишите причину исключения вопроса из оценки..."
                className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-xs text-slate-100 focus:outline-none focus:border-rose-500"
              />
            </div>

            <div className="flex items-center justify-end space-x-3 pt-2">
              <button
                type="button"
                onClick={() => setExcludingQuestionModal(null)}
                className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs rounded-lg font-medium transition cursor-pointer"
              >
                Отмена
              </button>
              <button
                type="button"
                onClick={handleConfirmExclude}
                disabled={!exclusionReasonInput.trim() || isSubmittingExclusion}
                className="px-4 py-2 bg-rose-600 hover:bg-rose-500 disabled:opacity-50 text-white text-xs rounded-lg font-semibold shadow-md shadow-rose-600/30 transition cursor-pointer flex items-center space-x-1.5"
              >
                {isSubmittingExclusion ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    <span>Исключение...</span>
                  </>
                ) : (
                  <span>Исключить вопрос</span>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
