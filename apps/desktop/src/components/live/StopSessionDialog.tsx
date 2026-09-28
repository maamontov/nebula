import React, { useEffect, useRef } from 'react';
import { AlertTriangle, Check, Loader2, Square, X } from 'lucide-react';
import { pluralRu } from '../../utils/plural';
import { formatClock } from './liveUtils';

export type StopStepKey = 'capture' | 'finalize' | 'upload' | 'review';
export type StopStepState = 'pending' | 'active' | 'done' | 'error';

export interface StopStep {
  key: StopStepKey;
  label: string;
  state: StopStepState;
  detail?: string | null;
}

interface StopSessionDialogProps {
  phase: 'confirm' | 'progress';
  elapsedMs: number;
  askedCount: number;
  totalQuestions: number;
  evaluatedCount: number;
  steps: StopStep[];
  error: string | null;
  /** Recording is already stopped: going back to the live screen is no longer possible. */
  captureStopped: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  onRetry: () => void;
  onSkipWaiting: () => void;
}

export const StopSessionDialog: React.FC<StopSessionDialogProps> = ({
  phase,
  elapsedMs,
  askedCount,
  totalQuestions,
  evaluatedCount,
  steps,
  error,
  captureStopped,
  onCancel,
  onConfirm,
  onRetry,
  onSkipWaiting,
}) => {
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (phase === 'confirm') confirmRef.current?.focus();
  }, [phase]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && phase === 'confirm') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [phase, onCancel]);

  const notAsked = totalQuestions - askedCount;
  const failedStep = steps.find((s) => s.state === 'error');

  return (
    <div className="dialog-backdrop" role="presentation">
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="stop-dialog-title">
        {phase === 'confirm' ? (
          <>
            <h2 id="stop-dialog-title">Завершить интервью?</h2>
            <p>Запись остановится, аудио будет обработано, и откроется экран проверки. Продолжить запись после этого нельзя.</p>
            <div className="dialog-stats">
              <div className="dialog-stat">
                <b>{formatClock(elapsedMs)}</b>
                <span>длительность</span>
              </div>
              <div className="dialog-stat">
                <b>
                  {askedCount}/{totalQuestions}
                </b>
                <span>вопросов задано</span>
              </div>
              <div className="dialog-stat">
                <b>{evaluatedCount}</b>
                <span>предв. оценок</span>
              </div>
            </div>
            {notAsked > 0 && (
              <div className="notice notice-warning">
                <AlertTriangle />
                <div className="notice-body">
                  {notAsked}{' '}
                  {pluralRu(notAsked, ['вопрос плана не был задан', 'вопроса плана не были заданы', 'вопросов плана не были заданы'])}.
                  На экране проверки их можно исключить из оценки.
                </div>
              </div>
            )}
            <div className="dialog-actions">
              <button type="button" className="btn btn-secondary" onClick={onCancel}>
                Продолжить запись
              </button>
              <button ref={confirmRef} type="button" className="btn btn-danger" onClick={onConfirm}>
                <Square />
                <span>Завершить</span>
              </button>
            </div>
          </>
        ) : (
          <>
            <h2 id="stop-dialog-title">{error ? 'Завершение прервано' : 'Завершаем интервью'}</h2>
            <p>
              {error
                ? 'Запись и данные сохранены. Можно повторить шаг или продолжить позже.'
                : 'Это займёт до минуты: дожидаемся выгрузки аудио и распознавания последних реплик.'}
            </p>
            <ol className="steps">
              {steps.map((step) => (
                <li key={step.key} className={`step step-${step.state}`}>
                  <span className="step-icon">
                    {step.state === 'done' ? (
                      <Check />
                    ) : step.state === 'active' ? (
                      <Loader2 className="spin" />
                    ) : step.state === 'error' ? (
                      <X />
                    ) : null}
                  </span>
                  <span>
                    {step.label}
                    {step.detail && <span className="step-detail">{step.detail}</span>}
                  </span>
                </li>
              ))}
            </ol>
            {error && (
              <div className="notice notice-danger">
                <AlertTriangle />
                <div className="notice-body">{error}</div>
              </div>
            )}
            {error && (
              <div className="dialog-actions">
                {!captureStopped && (
                  <button type="button" className="btn btn-secondary" onClick={onCancel}>
                    Вернуться к записи
                  </button>
                )}
                {failedStep?.key === 'upload' && (
                  <button type="button" className="btn btn-secondary" onClick={onSkipWaiting}>
                    Открыть проверку без ожидания
                  </button>
                )}
                <button type="button" className="btn btn-primary" onClick={onRetry}>
                  Повторить
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
};
