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
  Loader2,
  MessageSquarePlus,
  AlertTriangle,
  RefreshCw,
  Clock,
  CheckCircle2,
} from 'lucide-react';

const SLOW_GENERATION_MS = 45000;

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

  const [generatingTooLong, setGeneratingTooLong] = useState(false);
  const isPollingRef = useRef(false);
  const reloadPendingRef = useRef(false);
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
          // A guide hint is allowed before the candidate answers, so only probe is blocked here.
          // Наводящий вопрос доступен до ответа кандидата, поэтому «не ответил» показываем только для probe.
          if (mode === 'guide') {
            setErrorMessage('Не удалось подготовить наводящий вопрос. Повторите попытку.');
          } else if (res.wait_reason === 'needs_role_assignment') {
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
      if (isPollingRef.current) {
        // Question or mode may have changed while a request is in flight: reload once it ends.
        reloadPendingRef.current = true;
        return;
      }
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
        if (reloadPendingRef.current && isMountedRef.current) {
          reloadPendingRef.current = false;
          void loadStateRef.current(true);
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [interviewId, questionId, activeMode, autoEnabled, disabled, isPaused, isGenerating, handleGenerate]
  );

  // A generation that never finishes (AI provider down, worker busy) must not spin forever.
  useEffect(() => {
    setGeneratingTooLong(false);
    if (!isGenerating) return;
    const timer = setTimeout(() => setGeneratingTooLong(true), SLOW_GENERATION_MS);
    return () => clearTimeout(timer);
  }, [isGenerating, questionId]);

  // Initial load and periodic polling. `loadState` changes identity after every response
  // (it depends on the fetched state), so the interval reads it through a ref; otherwise the
  // effect restarts on each response and polls the backend in a tight loop.
  const loadStateRef = useRef(loadState);
  loadStateRef.current = loadState;

  useEffect(() => {
    void loadStateRef.current(false);
    const interval = setInterval(() => {
      void loadStateRef.current(true);
    }, 2500);

    return () => {
      clearInterval(interval);
    };
  }, [interviewId, questionId, activeMode]);

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
    setActiveMode('probe');
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
        asked_text: customAskedText !== undefined ? customAskedText : (status === 'asked' ? (suggestion.question_text || suggestion.suggested_text) : null),
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
          <span className="chip chip-accent">
            <HelpCircle />
            <span>Уточнение</span>
          </span>
        );
      case 'deepen':
        return (
          <span className="chip chip-purple">
            <Sparkles />
            <span>Углубление</span>
          </span>
        );
      case 'guide':
        return (
          <span className="chip chip-warning">
            <Compass />
            <span>Наводящий</span>
          </span>
        );
    }
  };

  const renderQuote = (quoteText: string, segId: string | null | undefined, key: React.Key) => (
    <button
      key={key}
      type="button"
      className="quote"
      onClick={() => segId && onLocateSegment(segId)}
      disabled={!segId}
      title={segId ? 'Показать в стенограмме' : undefined}
    >
      <Quote />
      <span>«{quoteText}»</span>
    </button>
  );

  return (
    <section className="assist" aria-label="Что спросить дальше">
      <div className="live-pane-head">
        <span className="pane-title">Что спросить дальше</span>
        <label className="assist-toggle" title="Предлагать вопросы автоматически, когда кандидат закончил отвечать">
          <input
            type="checkbox"
            className="sr-only"
            checked={autoEnabled}
            onChange={(e) => setAutoEnabled(e.target.checked)}
            disabled={disabled}
          />
          <span className={`switch${autoEnabled ? ' switch-on' : ''}`} aria-hidden="true" />
          <span>Авто{cooldownRemaining > 0 && autoEnabled ? ` · ${cooldownRemaining}с` : ''}</span>
        </label>
      </div>
      {questionTitle && (
        <div className="assist-question" title={questionTitle}>
          По текущему вопросу: {questionTitle}
        </div>
      )}

      <div className="assist-section">
        <div className="assist-actions">
          <button
            type="button"
            onClick={() => handleGenerate('probe', 'manual')}
            disabled={disabled || isGenerating}
            className="btn btn-primary btn-sm"
            title="Уточняющие и углубляющие вопросы по ответу кандидата"
          >
            {isGenerating && activeMode === 'probe' ? <Loader2 className="spin" /> : <Sparkles />}
            <span>Предложить вопросы</span>
          </button>
          <button
            type="button"
            onClick={() => handleGenerate('guide', 'manual')}
            disabled={disabled || isGenerating}
            className="btn btn-secondary btn-sm"
            title="Один наводящий вопрос, если кандидат затрудняется. Доступен до ответа."
          >
            {isGenerating && activeMode === 'guide' ? <Loader2 className="spin" /> : <Compass />}
            <span>Мягко направить</span>
          </button>
        </div>

        {errorMessage && (
          <div className="notice notice-danger assist-gap">
            <AlertTriangle />
            <div className="notice-body">
              {errorMessage}
              <div className="notice-actions">
                <button type="button" onClick={handleRetry} className="btn btn-secondary btn-sm">
                  <RefreshCw />
                  <span>Повторить</span>
                </button>
                <button type="button" onClick={() => setErrorMessage(null)} className="btn btn-ghost btn-sm">
                  <X />
                  <span>Скрыть</span>
                </button>
              </div>
            </div>
          </div>
        )}

        {stateResponse?.has_new_answer && !isGenerating && visibleSuggestions.length > 0 && (
          <div className="notice notice-info assist-gap">
            <MessageSquarePlus />
            <div className="notice-body">
              Кандидат продолжил отвечать — подсказки могли устареть.
              <div className="notice-actions">
                <button
                  type="button"
                  onClick={() => handleGenerate(activeMode, 'manual')}
                  disabled={cooldownRemaining > 0}
                  className="btn btn-secondary btn-sm"
                >
                  <RefreshCw />
                  <span>Обновить</span>
                </button>
              </div>
            </div>
          </div>
        )}

        {isGenerating && (
          <div className="assist-loading" role="status">
            <Loader2 className="spin" />
            <span>{activeMode === 'guide' ? 'Готовим наводящий вопрос…' : 'Анализируем ответ кандидата…'}</span>
          </div>
        )}

        {isGenerating && generatingTooLong && (
          <div className="notice notice-warning assist-gap">
            <Clock />
            <div className="notice-body">
              AI отвечает дольше обычного. Проверьте подключение модели в настройках или повторите запрос.
              <div className="notice-actions">
                <button type="button" onClick={handleRetry} className="btn btn-secondary btn-sm">
                  <RefreshCw />
                  <span>Повторить</span>
                </button>
              </div>
            </div>
          </div>
        )}

        {!isGenerating && visibleSuggestions.length === 0 && !errorMessage && (
          stateResponse?.latest_request?.outcome === 'no_suggestions' ? (
            <div className="assist-empty">
              <Check />
              <span>Ответ достаточно полный</span>
              <small>Дополнительные вопросы по критериям не нужны</small>
            </div>
          ) : candidateSegments.length === 0 ? (
            <div className="assist-empty">
              <Clock />
              <span>Ждём ответ кандидата</span>
              <small>
                {unassignedSharedCount > 0
                  ? 'Отметьте в стенограмме, какие реплики принадлежат кандидату'
                  : 'Если кандидат затрудняется, можно «мягко направить» уже сейчас'}
              </small>
            </div>
          ) : (
            <div className="assist-empty">
              <Sparkles />
              <span>{autoEnabled ? 'Подсказки появятся, когда кандидат закончит мысль' : 'Нажмите «Предложить вопросы»'}</span>
            </div>
          )
        )}

        {visibleSuggestions.length > 0 && !isGenerating && (
          <div className="suggestions">
            {visibleSuggestions.map((s) => {
              const isEditing = editingId === s.id;
              const isGuide = s.kind === 'guide';
              const isAsked = s.status === 'asked';
              const isBusy = isSubmittingPatch === s.id;

              return (
                <article
                  key={s.id}
                  className={`suggestion${isGuide ? ' suggestion-guide' : ''}${isAsked ? ' suggestion-asked' : ''}`}
                >
                  <div className="suggestion-head">
                    <span className="inline-group">
                      {getKindBadge(s.kind)}
                      {Boolean(s.is_stale) && !isAsked && <span className="chip chip-warning">Ответ изменился</span>}
                    </span>
                    {isAsked && (
                      <span className="chip chip-success">
                        <CheckCircle2 />
                        <span>Задан</span>
                      </span>
                    )}
                  </div>

                  {isEditing ? (
                    <>
                      <textarea
                        value={editingText}
                        onChange={(e) => setEditingText(e.target.value)}
                        rows={3}
                        autoFocus
                        className="suggestion-edit"
                        placeholder="Как вы сформулировали вопрос"
                      />
                      <div className="suggestion-foot">
                        <button
                          type="button"
                          onClick={() => {
                            setEditingId(null);
                            setEditingText('');
                          }}
                          className="btn btn-ghost btn-sm"
                        >
                          Отмена
                        </button>
                        <button
                          type="button"
                          onClick={() => handleDecision(s, 'asked', editingText)}
                          disabled={isBusy || !editingText.trim()}
                          className="btn btn-primary btn-sm"
                        >
                          <Check />
                          <span>Сохранить как заданный</span>
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <p className="suggestion-text">{s.asked_text || s.question_text || s.suggested_text}</p>
                      {s.asked_text && s.asked_text !== (s.question_text || s.suggested_text) && (
                        <p className="suggestion-purpose">Исходная формулировка: «{s.question_text || s.suggested_text}»</p>
                      )}
                    </>
                  )}

                  {!isAsked && (s.purpose || s.rationale) && <p className="suggestion-purpose">{s.purpose || s.rationale}</p>}

                  {!isAsked &&
                    (s.source_refs && s.source_refs.length > 0
                      ? s.source_refs.map((ref, idx) => {
                          const segId = ref.segment_id || (ref as any).turn_id;
                          const quoteText = ref.exact_quote || (ref as any).quote;
                          return quoteText ? renderQuote(quoteText, segId, idx) : null;
                        })
                      : s.evidence_quote
                      ? renderQuote(s.evidence_quote, s.evidence_segment_id, 'evidence')
                      : null)}

                  {!isAsked && !isEditing && (
                    <div className="suggestion-foot">
                      <button
                        type="button"
                        onClick={() => {
                          setEditingId(s.id);
                          setEditingText(s.question_text || s.suggested_text || '');
                        }}
                        className="btn btn-ghost btn-sm"
                        title="Задали вопрос другими словами — сохраните вашу формулировку"
                      >
                        <Edit2 />
                        <span>Задал иначе</span>
                      </button>
                      <span className="inline-group">
                        <button
                          type="button"
                          onClick={() => handleDecision(s, 'dismissed')}
                          disabled={isBusy}
                          className="btn btn-ghost btn-sm"
                          title="Скрыть подсказку"
                        >
                          <X />
                          <span>Скрыть</span>
                        </button>
                        <button
                          type="button"
                          onClick={() => handleDecision(s, 'asked')}
                          disabled={isBusy}
                          className="btn btn-secondary btn-sm"
                          title="Отметить, что вы задали этот вопрос"
                        >
                          {isBusy ? <Loader2 className="spin" /> : <Check />}
                          <span>Задал</span>
                        </button>
                      </span>
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
};
