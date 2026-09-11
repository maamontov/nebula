import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  FollowUpSuggestion,
  FollowUpKind,
  FollowUpMode,
  FollowUpTrigger,
  FollowUpsStateResponse,
  TranscriptSegment,
} from '../types';
import {
  getFollowUpsState,
  generateFollowUps,
  patchFollowUpSuggestion,
  retryFollowUpRequest,
} from '../services/api';
import {
  HelpCircle,
  Compass,
  Sparkles,
  Check,
  X,
  Edit2,
  Quote,
  ExternalLink,
  Loader2,
  AlertTriangle,
  RefreshCw,
  Clock,
  CheckCircle2,
} from 'lucide-react';

export interface FollowUpSuggestionsProps {
  interviewId: string;
  questionId: string;
  questionTitle?: string;
  segments: TranscriptSegment[];
  onLocateSegment: (segmentId: string) => void;
  isPaused?: boolean;
  disabled?: boolean;
}

export const FollowUpSuggestions: React.FC<FollowUpSuggestionsProps> = ({
  interviewId,
  questionId,
  questionTitle,
  segments,
  onLocateSegment,
  isPaused = false,
  disabled = false,
}) => {
  const [autoEnabled, setAutoEnabled] = useState(true);
  const [activeMode, setActiveMode] = useState<FollowUpMode>('probe');
  const [stateResponse, setStateResponse] = useState<FollowUpsStateResponse | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [cooldownRemaining, setCooldownRemaining] = useState(0);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState('');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmittingPatch, setIsSubmittingPatch] = useState<string | null>(null);

  const isPollingRef = useRef(false);
  const isMountedRef = useRef(true);
  const activeQuestionIdRef = useRef(questionId);
  const activeInterviewIdRef = useRef(interviewId);
  const activeRubricRevRef = useRef<string | null>(null);
  const activeTransRevRef = useRef<string | null>(null);

  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cooldownTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const lastRequestedFingerprintRef = useRef<string | null>(null);
  const pendingDebounceFingerprintRef = useRef<string | null>(null);
  const cooldownDeferredFingerprintRef = useRef<string | null>(null);

  // Filter candidate segments for presentation
  const candidateSegments = segments.filter(
    (s) =>
      (s.track_id === 'candidate' && s.speaker_role !== 'interviewer') ||
      (s.track_id === 'shared' && s.speaker_role === 'candidate')
  );
  const unassignedSharedCount = segments.filter(
    (s) => s.track_id === 'shared' && (!s.speaker_role || s.speaker_role === 'unknown')
  ).length;

  // Handle generation request
  const handleGenerate = useCallback(
    async (mode: FollowUpMode, trigger: FollowUpTrigger) => {
      if (disabled || isGenerating) return;
      if (trigger === 'auto' && cooldownRemaining > 0) return;

      setErrorMessage(null);
      setIsGenerating(true);
      setActiveMode(mode);

      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
      pendingDebounceFingerprintRef.current = null;

      try {
        const currentContextHash = stateResponse?.context_hash || null;
        const currentFingerprint = stateResponse?.candidate_fingerprint || null;

        const res = await generateFollowUps(interviewId, {
          question_id: questionId,
          mode,
          trigger,
          client_context_hash: currentContextHash,
        });

        if (currentFingerprint) {
          lastRequestedFingerprintRef.current = currentFingerprint;
        }

        if (res.status === 'waiting') {
          setIsGenerating(false);
          if (res.wait_reason === 'needs_role_assignment') {
            setErrorMessage('Для генерации подсказок подтвердите роли кандидата в стенограмме');
          } else if (trigger === 'manual') {
            setErrorMessage('Кандидат ещё не ответил на текущий вопрос');
          }
          await loadState(true);
        } else if (res.status === 'cooldown_active') {
          setIsGenerating(false);
          const cd = res.cooldown_remaining_sec ? Math.ceil(res.cooldown_remaining_sec) : 30;
          setCooldownRemaining(cd);
        } else if (res.is_cached) {
          setIsGenerating(false);
          await loadState(true);
        } else {
          // Worker enqueued; poll quickly
          setTimeout(() => {
            if (isMountedRef.current) loadState(true);
          }, 1000);
        }
      } catch (err: any) {
        setIsGenerating(false);
        setErrorMessage(err.message || 'Не удалось запустить генерацию подсказок');
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [disabled, isGenerating, cooldownRemaining, interviewId, questionId, stateResponse]
  );

  // Polling server state with in-flight guard
  const loadState = useCallback(
    async (silent = true) => {
      if (!interviewId || !questionId) return;
      if (isPollingRef.current) return;
      isPollingRef.current = true;

      try {
        const res = await getFollowUpsState(interviewId, questionId, activeMode);
        if (!isMountedRef.current || activeQuestionIdRef.current !== questionId) {
          return;
        }

        // Check revision changes
        if (
          activeTransRevRef.current !== null &&
          (res.active_transcript_revision_id !== activeTransRevRef.current ||
            res.active_rubric_revision_id !== activeRubricRevRef.current)
        ) {
          lastRequestedFingerprintRef.current = null;
          pendingDebounceFingerprintRef.current = null;
          if (debounceTimerRef.current) {
            clearTimeout(debounceTimerRef.current);
            debounceTimerRef.current = null;
          }
        }
        activeTransRevRef.current = res.active_transcript_revision_id || null;
        activeRubricRevRef.current = res.active_rubric_revision_id || null;

        setStateResponse(res);

        const cooldown =
          res.cooldown_seconds_remaining ??
          (res.cooldown_remaining_sec ? Math.ceil(res.cooldown_remaining_sec) : 0);
        setCooldownRemaining(cooldown);

        const isErr =
          res.status === 'error' ||
          res.latest_request?.outcome === 'failed' ||
          res.latest_request?.outcome === 'error';
        const isProc =
          res.status === 'processing' ||
          res.wait_reason === 'generating' ||
          res.latest_request?.outcome === 'pending';

        if (isErr) {
          setIsGenerating(false);
          setErrorMessage(res.error_message || 'Не удалось сгенерировать подсказки');
        } else if (isProc) {
          setIsGenerating(true);
        } else {
          setIsGenerating(false);
        }

        // Auto debounce evaluation based on backend candidate_fingerprint
        const fp = res.candidate_fingerprint;
        const canAuto =
          autoEnabled &&
          !disabled &&
          !isPaused &&
          activeMode === 'probe' &&
          Boolean(fp) &&
          fp !== lastRequestedFingerprintRef.current;

        if (canAuto && fp) {
          if (cooldown > 0) {
            cooldownDeferredFingerprintRef.current = fp;
            if (debounceTimerRef.current) {
              clearTimeout(debounceTimerRef.current);
              debounceTimerRef.current = null;
              pendingDebounceFingerprintRef.current = null;
            }
          } else if (!isProc && !isGenerating) {
            // Only start timer if not already ticking for this exact fingerprint
            if (pendingDebounceFingerprintRef.current !== fp) {
              if (debounceTimerRef.current) {
                clearTimeout(debounceTimerRef.current);
              }
              pendingDebounceFingerprintRef.current = fp;
              debounceTimerRef.current = setTimeout(() => {
                if (isMountedRef.current && activeQuestionIdRef.current === questionId) {
                  handleGenerate('probe', 'auto');
                }
              }, 3000);
            }
          }
        } else if (!canAuto && debounceTimerRef.current && fp === lastRequestedFingerprintRef.current) {
          clearTimeout(debounceTimerRef.current);
          debounceTimerRef.current = null;
          pendingDebounceFingerprintRef.current = null;
        }
      } catch (err: any) {
        if (!silent && isMountedRef.current) {
          setErrorMessage(err.message || 'Ошибка загрузки состояния подсказок');
        }
      } finally {
        isPollingRef.current = false;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [interviewId, questionId, activeMode, autoEnabled, disabled, isPaused, isGenerating, handleGenerate]
  );

  // Initial load and periodic polling
  useEffect(() => {
    loadState(false);
    const interval = setInterval(() => {
      loadState(true);
    }, 2500);

    return () => {
      clearInterval(interval);
    };
  }, [loadState]);

  // Local countdown for cooldown
  useEffect(() => {
    if (cooldownRemaining <= 0) {
      if (cooldownTimerRef.current) clearInterval(cooldownTimerRef.current);
      // Cooldown just hit 0: if deferred speech arrived during cooldown, trigger debounce
      const deferred = cooldownDeferredFingerprintRef.current;
      if (
        deferred &&
        deferred !== lastRequestedFingerprintRef.current &&
        autoEnabled &&
        !disabled &&
        !isPaused &&
        activeMode === 'probe' &&
        !isGenerating
      ) {
        cooldownDeferredFingerprintRef.current = null;
        if (pendingDebounceFingerprintRef.current !== deferred) {
          if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
          pendingDebounceFingerprintRef.current = deferred;
          debounceTimerRef.current = setTimeout(() => {
            if (isMountedRef.current && activeQuestionIdRef.current === questionId) {
              handleGenerate('probe', 'auto');
            }
          }, 3000);
        }
      }
      return;
    }

    cooldownTimerRef.current = setInterval(() => {
      setCooldownRemaining((prev) => (prev > 1 ? prev - 1 : 0));
    }, 1000);

    return () => {
      if (cooldownTimerRef.current) clearInterval(cooldownTimerRef.current);
    };
  }, [cooldownRemaining, autoEnabled, disabled, isPaused, activeMode, isGenerating, questionId, handleGenerate]);

  // Reset state and timers on question change
  useEffect(() => {
    activeQuestionIdRef.current = questionId;
    lastRequestedFingerprintRef.current = null;
    pendingDebounceFingerprintRef.current = null;
    cooldownDeferredFingerprintRef.current = null;
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }
    setEditingId(null);
    setEditingText('');
    setErrorMessage(null);
    setStateResponse(null);
    setIsGenerating(false);
  }, [questionId]);

  // Lifecycle mount / unmount cleanup
  useEffect(() => {
    isMountedRef.current = true;
    activeInterviewIdRef.current = interviewId;
    return () => {
      isMountedRef.current = false;
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
      if (cooldownTimerRef.current) clearInterval(cooldownTimerRef.current);
    };
  }, [interviewId]);

  // Patch suggestion decision
  const handleDecision = async (
    suggestion: FollowUpSuggestion,
    status: 'asked' | 'dismissed',
    customAskedText?: string
  ) => {
    try {
      setIsSubmittingPatch(suggestion.id);
      setErrorMessage(null);
      await patchFollowUpSuggestion(interviewId, suggestion.id, {
        status,
        asked_text: customAskedText !== undefined ? customAskedText : (status === 'asked' ? suggestion.suggested_text : null),
        expected_decision_version: suggestion.decision_version,
      });
      setEditingId(null);
      setEditingText('');
      await loadState(true);
    } catch (err: any) {
      if (err.message && err.message.includes('409')) {
        setErrorMessage('Карточка уже была обновлена. Загружаем актуальные данные...');
        await loadState(true);
      } else {
        setErrorMessage(err.message || 'Ошибка обновления статуса карточки');
      }
    } finally {
      setIsSubmittingPatch(null);
    }
  };

  // Retry request
  const handleRetry = async () => {
    setErrorMessage(null);
    const reqId = stateResponse?.latest_request?.id || stateResponse?.active_request_id;
    if (!reqId) {
      handleGenerate(activeMode, 'manual');
      return;
    }
    try {
      setIsGenerating(true);
      await retryFollowUpRequest(interviewId, reqId);
      setTimeout(() => loadState(true), 1000);
    } catch (err: any) {
      setIsGenerating(false);
      setErrorMessage(err.message || 'Ошибка повторного запуска генерации');
    }
  };

  const visibleSuggestions = (stateResponse?.suggestions || []).filter(
    (s) => s.status !== 'dismissed'
  );

  const getKindBadge = (kind: FollowUpKind) => {
    switch (kind) {
      case 'clarify':
        return (
          <span className="px-2 py-0.5 text-[10px] font-bold rounded bg-indigo-950/80 text-indigo-300 border border-indigo-700/60 flex items-center space-x-1">
            <HelpCircle className="w-3 h-3" />
            <span>Уточнение</span>
          </span>
        );
      case 'deepen':
        return (
          <span className="px-2 py-0.5 text-[10px] font-bold rounded bg-purple-950/80 text-purple-300 border border-purple-700/60 flex items-center space-x-1">
            <Sparkles className="w-3 h-3" />
            <span>Углубление</span>
          </span>
        );
      case 'guide':
        return (
          <span className="px-2 py-0.5 text-[10px] font-bold rounded bg-amber-950/80 text-amber-300 border border-amber-600/70 flex items-center space-x-1 shadow-sm">
            <Compass className="w-3 h-3 text-amber-400" />
            <span>Наводящий вопрос (подсказка)</span>
          </span>
        );
    }
  };

  return (
    <div className="p-3 bg-slate-900/90 border border-slate-800 rounded-xl space-y-3">
      {/* Header with Title and Auto-toggle */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <HelpCircle className="w-4 h-4 text-cyan-400 shrink-0" />
          <div className="min-w-0">
            <span className="text-xs font-bold uppercase tracking-wider text-slate-200 block">
              Что спросить дальше
            </span>
            {questionTitle && (
              <span className="text-[10px] text-slate-400 truncate block max-w-[170px]" title={questionTitle}>
                {questionTitle}
              </span>
            )}
          </div>
        </div>

        <label className="flex items-center space-x-1.5 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={autoEnabled}
            onChange={(e) => setAutoEnabled(e.target.checked)}
            disabled={disabled}
            className="rounded border-slate-700 text-indigo-600 focus:ring-indigo-500 bg-slate-800 text-xs w-3.5 h-3.5 cursor-pointer"
          />
          <span className="text-[11px] text-slate-300 font-medium">Автоподсказки</span>
          {cooldownRemaining > 0 && autoEnabled && (
            <span className="text-[10px] text-slate-400 font-mono">({cooldownRemaining}с)</span>
          )}
        </label>
      </div>

      {/* Action Buttons: Probe & Guide */}
      <div className="grid grid-cols-2 gap-2">
        <button
          type="button"
          onClick={() => handleGenerate('probe', 'manual')}
          disabled={disabled || isGenerating}
          className="flex items-center justify-center space-x-1.5 px-2.5 py-1.5 bg-indigo-600/90 hover:bg-indigo-500 disabled:bg-slate-800/80 disabled:text-slate-500 text-white text-xs font-semibold rounded-lg shadow-sm transition cursor-pointer disabled:cursor-not-allowed"
          title="Сгенерировать уточняющие и углубляющие вопросы по ответу кандидата"
        >
          {isGenerating && activeMode === 'probe' ? (
            <>
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              <span>Генерация...</span>
            </>
          ) : (
            <>
              <Sparkles className="w-3.5 h-3.5 text-indigo-300" />
              <span>Предложить вопросы</span>
            </>
          )}
        </button>

        <button
          type="button"
          onClick={() => handleGenerate('guide', 'manual')}
          disabled={disabled || isGenerating}
          className="flex items-center justify-center space-x-1.5 px-2.5 py-1.5 bg-amber-600/90 hover:bg-amber-500 disabled:bg-slate-800/80 disabled:text-slate-500 text-white text-xs font-semibold rounded-lg shadow-sm transition cursor-pointer disabled:cursor-not-allowed"
          title="Предложить один наводящий вопрос, если кандидат испытывает затруднения"
        >
          {isGenerating && activeMode === 'guide' ? (
            <>
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              <span>Генерация...</span>
            </>
          ) : (
            <>
              <Compass className="w-3.5 h-3.5 text-amber-200" />
              <span>Мягко направить</span>
            </>
          )}
        </button>
      </div>

      {/* Error / Retry Bar */}
      {errorMessage && (
        <div className="p-2.5 bg-rose-950/40 border border-rose-800/80 rounded-lg flex items-center justify-between space-x-2 text-xs text-rose-300">
          <div className="flex items-center space-x-1.5 min-w-0">
            <AlertTriangle className="w-3.5 h-3.5 text-rose-400 shrink-0" />
            <span className="truncate" title={errorMessage}>{errorMessage}</span>
          </div>
          <div className="flex items-center space-x-1 shrink-0">
            <button
              type="button"
              onClick={handleRetry}
              className="px-2 py-0.5 bg-rose-800/60 hover:bg-rose-700/80 text-white rounded text-[10px] font-semibold flex items-center space-x-1 cursor-pointer transition"
            >
              <RefreshCw className="w-2.5 h-2.5" />
              <span>Повторить</span>
            </button>
            <button
              type="button"
              onClick={() => setErrorMessage(null)}
              className="p-1 hover:bg-rose-900/50 text-rose-400 hover:text-rose-200 rounded cursor-pointer transition"
              title="Закрыть"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        </div>
      )}

      {/* Warning: Stale answer banner */}
      {stateResponse?.has_new_answer && !isGenerating && (
        <div className="p-2 bg-indigo-950/40 border border-indigo-800/60 rounded-lg flex items-center justify-between text-xs text-indigo-300">
          <span className="text-[11px]">Поступили новые реплики кандидата</span>
          <button
            type="button"
            onClick={() => handleGenerate(activeMode, 'manual')}
            disabled={cooldownRemaining > 0}
            className="px-2 py-0.5 bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50 text-white text-[10px] font-semibold rounded"
          >
            Обновить
          </button>
        </div>
      )}

      {/* Context info: No candidate segments */}
      {candidateSegments.length === 0 && (
        <div className="p-3 text-center text-xs text-slate-400 border border-dashed border-slate-800 rounded-lg space-y-1">
          <Clock className="w-4 h-4 mx-auto text-slate-500" />
          <p>Ожидание ответа кандидата на текущий вопрос</p>
          {unassignedSharedCount > 0 && (
            <p className="text-[10px] text-amber-400 font-medium">
              Назначьте реплики кандидата в стенограмме для генерации подсказок
            </p>
          )}
        </div>
      )}

      {/* Generating Loader */}
      {isGenerating && (
        <div className="p-4 bg-slate-950/60 border border-indigo-900/60 rounded-lg flex items-center justify-center space-x-2 text-xs text-indigo-300 animate-pulse">
          <Loader2 className="w-4 h-4 animate-spin text-indigo-400" />
          <span>Анализируем ответ кандидата...</span>
        </div>
      )}

      {/* Informative message when no suggestions were needed */}
      {visibleSuggestions.length === 0 && !isGenerating && !errorMessage && stateResponse?.latest_request?.outcome === 'no_suggestions' && (
        <div className="p-3 text-center text-xs text-slate-400 border border-slate-800/80 rounded-lg space-y-1 bg-slate-950/40">
          <Check className="w-4 h-4 mx-auto text-emerald-400" />
          <p className="text-slate-300 font-medium">Ответ достаточно полон</p>
          <p className="text-[10px] text-slate-500">Дополнительные вопросы по текущим критериям не требуются</p>
        </div>
      )}

      {/* Suggestion Cards */}
      {visibleSuggestions.length > 0 && !isGenerating && (
        <div className="space-y-2.5">
          {visibleSuggestions.map((s) => {
            const isEditing = editingId === s.id;
            const isGuide = s.kind === 'guide';
            const isAsked = s.status === 'asked';

            return (
              <div
                key={s.id}
                className={`p-3 rounded-xl border transition-all space-y-2 ${
                  isGuide
                    ? 'bg-amber-950/20 border-amber-800/60 shadow-md shadow-amber-950/30'
                    : isAsked
                    ? 'bg-emerald-950/20 border-emerald-800/50'
                    : 'bg-slate-950/70 border-slate-800'
                }`}
              >
                {/* Kind & Status Badges */}
                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-1.5">
                    {getKindBadge(s.kind)}
                    {s.is_stale && !isAsked && (
                      <span className="px-1.5 py-0.5 text-[9px] font-semibold text-amber-400 bg-amber-950/80 border border-amber-800/70 rounded">
                        Ответ изменился
                      </span>
                    )}
                  </div>

                  {isAsked && (
                    <span className="px-2 py-0.5 text-[10px] font-bold text-emerald-300 bg-emerald-950/80 border border-emerald-700/60 rounded flex items-center space-x-1">
                      <CheckCircle2 className="w-3 h-3 text-emerald-400" />
                      <span>Задан</span>
                    </span>
                  )}
                </div>

                {/* Suggested Question Text */}
                {isEditing ? (
                  <div className="space-y-1.5">
                    <textarea
                      value={editingText}
                      onChange={(e) => setEditingText(e.target.value)}
                      rows={2}
                      className="w-full text-xs p-2 rounded bg-slate-900 border border-indigo-600 text-slate-100 focus:outline-none"
                      placeholder="Отредактируйте формулировку вопроса..."
                    />
                    <div className="flex justify-end space-x-1.5">
                      <button
                        type="button"
                        onClick={() => {
                          setEditingId(null);
                          setEditingText('');
                        }}
                        className="px-2 py-0.5 text-[10px] text-slate-400 hover:text-slate-200"
                      >
                        Отмена
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDecision(s, 'asked', editingText)}
                        disabled={isSubmittingPatch === s.id || !editingText.trim()}
                        className="px-2.5 py-0.5 text-[10px] font-semibold bg-emerald-600 hover:bg-emerald-500 text-white rounded flex items-center space-x-1"
                      >
                        <Check className="w-3 h-3" />
                        <span>Подтвердить</span>
                      </button>
                    </div>
                  </div>
                ) : (
                  <div>
                    <p className="text-xs font-medium text-slate-100 leading-relaxed">
                      {s.asked_text || s.suggested_text}
                    </p>
                    {s.asked_text && s.asked_text !== s.suggested_text && (
                      <p className="text-[10px] text-slate-400 italic mt-0.5">
                        Исходный: «{s.suggested_text}»
                      </p>
                    )}
                  </div>
                )}

                {/* Rationale */}
                {s.rationale && (
                  <p className="text-[11px] text-slate-400 leading-normal">
                    {s.rationale}
                  </p>
                )}

                {/* Evidence Quote with Clickable Transcript Jump */}
                {s.evidence_quote && (
                  <div
                    onClick={() => {
                      if (s.evidence_segment_id) {
                        onLocateSegment(s.evidence_segment_id);
                      }
                    }}
                    className={`group p-2 rounded border text-[11px] flex items-start space-x-1.5 transition cursor-pointer ${
                      s.evidence_segment_id
                        ? 'bg-slate-900/80 hover:bg-indigo-950/40 border-slate-800 hover:border-indigo-600 text-slate-300'
                        : 'bg-slate-900/50 border-slate-800 text-slate-400'
                    }`}
                    title={s.evidence_segment_id ? 'Перейти к цитате в стенограмме' : undefined}
                  >
                    <Quote className="w-3 h-3 text-indigo-400 shrink-0 mt-0.5" />
                    <div className="flex-1 italic leading-snug">«{s.evidence_quote}»</div>
                    {s.evidence_segment_id && (
                      <ExternalLink className="w-2.5 h-2.5 text-indigo-400 group-hover:text-amber-300 shrink-0 mt-0.5" />
                    )}
                  </div>
                )}

                {/* Actions when not asked yet */}
                {!isAsked && !isEditing && (
                  <div className="flex items-center justify-between pt-1 border-t border-slate-800/80">
                    <button
                      type="button"
                      onClick={() => {
                        setEditingId(s.id);
                        setEditingText(s.suggested_text);
                      }}
                      className="flex items-center space-x-1 text-[10px] text-slate-400 hover:text-indigo-300 transition"
                      title="Отредактировать формулировку перед подтверждением"
                    >
                      <Edit2 className="w-2.5 h-2.5" />
                      <span>С правкой</span>
                    </button>

                    <div className="flex items-center space-x-1.5">
                      <button
                        type="button"
                        onClick={() => handleDecision(s, 'dismissed')}
                        disabled={isSubmittingPatch === s.id}
                        className="px-2 py-1 text-[10px] font-semibold text-slate-400 hover:text-rose-300 hover:bg-rose-950/40 border border-slate-700/60 rounded transition flex items-center space-x-1"
                        title="Отклонить эту подсказку"
                      >
                        <X className="w-2.5 h-2.5" />
                        <span>Не подходит</span>
                      </button>

                      <button
                        type="button"
                        onClick={() => handleDecision(s, 'asked')}
                        disabled={isSubmittingPatch === s.id}
                        className="px-2.5 py-1 text-[10px] font-semibold bg-emerald-600/90 hover:bg-emerald-500 text-white rounded shadow-sm transition flex items-center space-x-1"
                        title="Отметить вопрос как заданный кандидату"
                      >
                        {isSubmittingPatch === s.id ? (
                          <Loader2 className="w-2.5 h-2.5 animate-spin" />
                        ) : (
                          <Check className="w-2.5 h-2.5" />
                        )}
                        <span>Задан</span>
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
