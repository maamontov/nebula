import React, { useState } from 'react';
import {
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  CornerDownLeft,
  Loader2,
  Mic,
  Quote,
  RefreshCw,
  Sparkles,
} from 'lucide-react';
import { AssessmentProposal, PlannedQuestion } from '../../types';
import { EvalStatus } from './QuestionList';
import { formatClock, formatScore, proposalAverage, questionPrompt, questionTitle } from './liveUtils';

interface QuestionFocusProps {
  question: PlannedQuestion;
  index: number;
  isCurrent: boolean;
  firstMarkMs?: number;
  proposal?: AssessmentProposal;
  evalStatus: EvalStatus;
  evalError?: string | null;
  isMarking: boolean;
  disabled: boolean;
  onMarkAsCurrent: () => void;
  onBackToCurrent: () => void;
  onEvaluate: () => void;
  onLocateSegment: (segmentId: string) => void;
}

export const QuestionFocus: React.FC<QuestionFocusProps> = ({
  question,
  index,
  isCurrent,
  firstMarkMs,
  proposal,
  evalStatus,
  evalError,
  isMarking,
  disabled,
  onMarkAsCurrent,
  onBackToCurrent,
  onEvaluate,
  onLocateSegment,
}) => {
  const [promptExpanded, setPromptExpanded] = useState(false);
  const [evalExpanded, setEvalExpanded] = useState(false);
  const title = questionTitle(question);
  const prompt = questionPrompt(question);
  const showPrompt = prompt && prompt !== title;
  const asked = firstMarkMs !== undefined;
  const avg = proposalAverage(proposal, question);
  const isBusy = evalStatus === 'running' || evalStatus === 'scheduled';

  const criterionTitle = (id: string) => question.criteria.find((c) => c.id === id)?.title || 'Критерий';
  const criterionMax = (id: string) => question.criteria.find((c) => c.id === id)?.max_score || 5;

  const renderEvaluation = () => {
    if (isBusy) {
      return (
        <div className="focus-eval-row">
          <span className="focus-eval-label">
            <Loader2 className="spin" />
            {evalStatus === 'scheduled' ? 'Ждём окончания распознавания ответа…' : 'Оцениваем ответ…'}
          </span>
        </div>
      );
    }

    if (evalStatus === 'failed') {
      return (
        <div className="notice notice-danger">
          <AlertTriangle />
          <div className="notice-body">
            Не удалось оценить ответ{evalError ? `: ${evalError}` : '.'}
            <div className="notice-actions">
              <button type="button" className="btn btn-secondary btn-sm" onClick={onEvaluate} disabled={disabled}>
                <RefreshCw />
                <span>Повторить</span>
              </button>
            </div>
          </div>
        </div>
      );
    }

    if (!proposal) {
      return (
        <div className="focus-eval-row">
          <span className="focus-eval-label">
            <Sparkles />
            {isCurrent
              ? 'Оценка — после перехода к следующему вопросу'
              : asked
              ? 'Ответ ещё не оценён'
              : 'Вопрос ещё не задан'}
          </span>
          {asked && (
            <button type="button" className="btn btn-ghost btn-sm" onClick={onEvaluate} disabled={disabled}>
              <Sparkles />
              <span>Оценить сейчас</span>
            </button>
          )}
        </div>
      );
    }

    return (
      <>
        <div className="focus-eval-row">
          <span className="focus-eval-label">
            <Sparkles />
            Предварительная оценка AI
            {avg && (
              <span className="score-pill">
                {formatScore(avg.value)}
                <small>/{avg.max}</small>
              </span>
            )}
            {Boolean(proposal.is_rejected) && <span className="chip chip-warning">Требует проверки</span>}
          </span>
          <span className="focus-eval-tools">
            <button type="button" className="btn btn-ghost btn-sm" onClick={onEvaluate} disabled={disabled} title="Оценить заново с учётом новых реплик">
              <RefreshCw />
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setEvalExpanded((v) => !v)}
              aria-expanded={evalExpanded}
            >
              {evalExpanded ? <ChevronUp /> : <ChevronDown />}
              <span>{evalExpanded ? 'Свернуть' : 'Подробнее'}</span>
            </button>
          </span>
        </div>

        {evalExpanded && (
          <div className="criterion-list">
            {Boolean(proposal.is_rejected) && (
              <div className="notice notice-warning">
                <AlertTriangle />
                <div className="notice-body">
                  Цитаты модели не подтвердились в стенограмме. Оценку нужно проверить вручную на экране проверки.
                  {proposal.validation_errors && proposal.validation_errors.length > 0 && (
                    <ul className="notice-list">
                      {proposal.validation_errors.map((err, i) => (
                        <li key={i}>{err}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            )}
            {proposal.scores.map((score, i) => (
              <div key={`${score.criterion_id}-${i}`} className="criterion">
                <div className="criterion-head">
                  <span>{criterionTitle(score.criterion_id)}</span>
                  {typeof score.score === 'number' ? (
                    <span className="score-pill">
                      {formatScore(score.score)}
                      <small>/{criterionMax(score.criterion_id)}</small>
                    </span>
                  ) : (
                    <span className="chip">Нет ответа</span>
                  )}
                </div>
                {score.explanation && <p className="criterion-text">{score.explanation}</p>}
                {score.evidence?.map((ev, j) => (
                  <button
                    key={j}
                    type="button"
                    className="quote"
                    onClick={() => onLocateSegment(ev.segment_id)}
                    title="Показать в стенограмме"
                  >
                    <Quote />
                    <span>«{ev.exact_quote}»</span>
                  </button>
                ))}
              </div>
            ))}
            {proposal.critical_errors.length > 0 && (
              <div className="notice notice-danger">
                <AlertTriangle />
                <div className="notice-body">
                  <strong>Критические ошибки</strong>
                  {proposal.critical_errors.map((err, i) => (
                    <div key={i}>{err}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </>
    );
  };

  return (
    <section className={`focus${isCurrent ? '' : ' focus-preview'}`} aria-label="Выбранный вопрос">
      <div className="focus-head">
        <span className={`focus-kicker${isCurrent ? ' focus-kicker-current' : ''}`}>
          {isCurrent ? <Mic className="w-3.5 h-3.5" /> : null}
          {isCurrent ? 'Сейчас' : 'Просмотр'} · Вопрос {index + 1}
          {asked && <span className="tdivider-time">{formatClock(firstMarkMs!)}</span>}
        </span>
        {question.weight !== undefined && question.weight !== 1 && (
          <span className="chip">вес {question.weight}</span>
        )}
      </div>

      <h2 className="focus-title">{title}</h2>
      {showPrompt && (
        <p
          className={`focus-prompt${promptExpanded ? '' : ' focus-prompt-clamped'}`}
          onClick={() => setPromptExpanded((v) => !v)}
          title={promptExpanded ? undefined : 'Показать полностью'}
        >
          {prompt}
        </p>
      )}

      {question.criteria.length > 0 && (
        <div className="focus-criteria" aria-label="Критерии оценки">
          {question.criteria.map((c) => (
            <span key={c.id} className="chip" title={c.description || undefined}>
              {c.title}
            </span>
          ))}
        </div>
      )}

      {!isCurrent && (
        <div className="focus-actions">
          <button type="button" className="btn btn-primary btn-sm" onClick={onMarkAsCurrent} disabled={disabled || isMarking}>
            {isMarking ? <Loader2 className="spin" /> : <Mic />}
            <span>{asked ? 'Вернуться к этому вопросу' : 'Задаю этот вопрос'}</span>
          </button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onBackToCurrent}>
            <CornerDownLeft />
            <span>К текущему вопросу</span>
          </button>
        </div>
      )}

      <div className="focus-eval">{renderEvaluation()}</div>
    </section>
  );
};
