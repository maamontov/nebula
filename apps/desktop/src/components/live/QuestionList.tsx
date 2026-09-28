import React from 'react';
import { AlertTriangle, ArrowRight, Check, Flag, Loader2 } from 'lucide-react';
import { AssessmentProposal, PlannedQuestion } from '../../types';
import { formatClock, formatScore, proposalAverage, questionTitle } from './liveUtils';

export type EvalStatus = 'idle' | 'scheduled' | 'running' | 'failed';

interface QuestionListProps {
  questions: PlannedQuestion[];
  currentQuestionId: string | null;
  focusedQuestionId: string | null;
  firstMarkMs: Record<string, number>;
  proposals: Record<string, AssessmentProposal | undefined>;
  evalStatus: Record<string, EvalStatus>;
  nextQuestion: PlannedQuestion | null;
  isMarking: boolean;
  disabled: boolean;
  onSelect: (questionId: string) => void;
  onNext: () => void;
  onFinish: () => void;
}

export const QuestionList: React.FC<QuestionListProps> = ({
  questions,
  currentQuestionId,
  focusedQuestionId,
  firstMarkMs,
  proposals,
  evalStatus,
  nextQuestion,
  isMarking,
  disabled,
  onSelect,
  onNext,
  onFinish,
}) => {
  const askedCount = questions.filter((q) => firstMarkMs[q.id] !== undefined).length;

  const renderStatus = (q: PlannedQuestion, isCurrent: boolean) => {
    const status = evalStatus[q.id];
    const avg = proposalAverage(proposals[q.id], q);
    const asked = firstMarkMs[q.id] !== undefined;

    if (isCurrent) {
      return (
        <span>{asked ? `Сейчас · с ${formatClock(firstMarkMs[q.id])}` : 'Сейчас'}</span>
      );
    }
    if (status === 'running' || status === 'scheduled') {
      return (
        <>
          <Loader2 className="w-3 h-3 spin" />
          <span>Оцениваем ответ…</span>
        </>
      );
    }
    if (status === 'failed' || proposals[q.id]?.is_rejected) {
      return (
        <>
          <AlertTriangle className="w-3 h-3" />
          <span>Оценку нужно проверить</span>
        </>
      );
    }
    if (avg) {
      return (
        <>
          <span className="score-pill">
            {formatScore(avg.value)}
            <small>/{avg.max}</small>
          </span>
          <span>предв. оценка</span>
        </>
      );
    }
    if (asked) return <span>Задан в {formatClock(firstMarkMs[q.id])}</span>;
    return <span>Не задан</span>;
  };

  return (
    <aside className="live-pane" aria-label="План вопросов">
      <div className="live-pane-head">
        <span className="pane-title">Вопросы</span>
        <span className="pane-meta">
          задано {askedCount} из {questions.length}
        </span>
      </div>

      <div className="live-pane-scroll">
        <div className="qlist" role="list">
          {questions.map((q, idx) => {
            const isCurrent = q.id === currentQuestionId;
            const isFocused = q.id === focusedQuestionId && !isCurrent;
            const asked = firstMarkMs[q.id] !== undefined;
            const stateClass = isCurrent ? 'qitem-current' : asked ? 'qitem-asked' : 'qitem-pending';
            return (
              <button
                key={q.id}
                type="button"
                role="listitem"
                className={`qitem ${stateClass}${isFocused ? ' qitem-focused' : ''}`}
                onClick={() => onSelect(q.id)}
                aria-current={isCurrent ? 'step' : undefined}
                title={isCurrent ? 'Текущий вопрос' : 'Открыть вопрос (текущий вопрос не изменится)'}
              >
                <span className="qnum">{asked && !isCurrent ? <Check /> : idx + 1}</span>
                <span className="qitem-body">
                  <span className="qitem-title">{questionTitle(q)}</span>
                  <span className="qitem-status">{renderStatus(q, isCurrent)}</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="qlist-footer">
        {nextQuestion ? (
          <>
            <span className="qlist-next-label" title={questionTitle(nextQuestion)}>
              Дальше: {questionTitle(nextQuestion)}
            </span>
            <button type="button" className="btn btn-primary btn-block" onClick={onNext} disabled={disabled || isMarking}>
              {isMarking ? <Loader2 className="spin" /> : <ArrowRight />}
              <span>Следующий вопрос</span>
            </button>
          </>
        ) : (
          <>
            <span className="qlist-next-label">Все вопросы плана заданы</span>
            <button type="button" className="btn btn-secondary btn-block" onClick={onFinish} disabled={disabled}>
              <Flag />
              <span>Завершить интервью</span>
            </button>
          </>
        )}
      </div>
    </aside>
  );
};
