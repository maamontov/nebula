import { forwardRef, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowLeftRight, MessageSquare } from 'lucide-react';
import { PlannedQuestion, SpeakerRole, TranscriptSegment } from '../../types';
import { pluralRu } from '../../utils/plural';
import { formatClock, questionTitle, resolveRole, ResolvedRole } from './liveUtils';

export interface LiveTranscriptHandle {
  scrollToSegment: (segmentId: string) => void;
  scrollToQuestion: (questionId: string) => void;
}

interface LiveTranscriptProps {
  segments: TranscriptSegment[];
  segmentQuestions: Record<string, { question_id: string; is_ambiguous: boolean }>;
  questions: PlannedQuestion[];
  currentQuestionId: string | null;
  highlightedSegmentId: string | null;
  updatingRoleId: string | null;
  isPaused: boolean;
  onSetRole: (segmentId: string, role: SpeakerRole) => void;
}

const SPEAKER_LABEL: Record<ResolvedRole, string> = {
  candidate: 'Кандидат',
  interviewer: 'Интервьюер',
  unknown: 'Не указан',
};

const NEAR_BOTTOM_PX = 96;

export const LiveTranscript = forwardRef<LiveTranscriptHandle, LiveTranscriptProps>(
  ({ segments, segmentQuestions, questions, currentQuestionId, highlightedSegmentId, updatingRoleId, isPaused, onSetRole }, ref) => {
    const scrollRef = useRef<HTMLDivElement>(null);
    const nearBottomRef = useRef(true);
    const prevCountRef = useRef(0);
    const [hasUnseen, setHasUnseen] = useState(false);

    const isNearBottom = () => {
      const el = scrollRef.current;
      if (!el) return true;
      return el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
    };

    const scrollToBottom = (smooth: boolean) => {
      const el = scrollRef.current;
      if (!el) return;
      el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
      nearBottomRef.current = true;
      setHasUnseen(false);
    };

    const scrollIntoCenter = (elementId: string) => {
      const target = document.getElementById(elementId);
      if (!target) return;
      nearBottomRef.current = false;
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };

    useImperativeHandle(ref, () => ({
      scrollToSegment: (segmentId) => scrollIntoCenter(`seg-${segmentId}`),
      scrollToQuestion: (questionId) => {
        const divider = scrollRef.current?.querySelector<HTMLElement>(`[data-question-divider="${CSS.escape(questionId)}"]`);
        const firstSegment = divider?.nextElementSibling;
        if (!firstSegment) return;
        nearBottomRef.current = false;
        firstSegment.scrollIntoView({ behavior: 'smooth', block: 'start' });
      },
    }));

    // Follow new speech only when the reader is already at the bottom and only when segments were
    // appended; a periodic refresh of the same transcript must never move the scroll position.
    useLayoutEffect(() => {
      const added = segments.length > prevCountRef.current;
      const first = prevCountRef.current === 0;
      prevCountRef.current = segments.length;
      if (!added) return;
      if (first || nearBottomRef.current) {
        scrollToBottom(!first);
      } else {
        setHasUnseen(true);
      }
    }, [segments.length]);

    const handleScroll = () => {
      nearBottomRef.current = isNearBottom();
      if (nearBottomRef.current) setHasUnseen(false);
    };

    const questionIndex = (qid: string) => questions.findIndex((q) => q.id === qid);

    // Consecutive segments of the same question form a group; the sticky divider stays inside
    // its group so the next question's divider pushes it away instead of overlapping it.
    const groups: Array<{ questionId: string | null; segments: TranscriptSegment[] }> = [];
    for (const seg of segments) {
      const qid = segmentQuestions[seg.id]?.question_id ?? null;
      const last = groups[groups.length - 1];
      if (last && last.questionId === qid) last.segments.push(seg);
      else groups.push({ questionId: qid, segments: [seg] });
    }

    const renderSegment = (seg: TranscriptSegment) => {
      const role = resolveRole(seg);
      const otherRole: SpeakerRole = role === 'candidate' ? 'interviewer' : 'candidate';
      const busy = updatingRoleId === seg.id;
      return (
        <div
          key={seg.id}
          id={`seg-${seg.id}`}
          className={`tseg tseg-${role}${highlightedSegmentId === seg.id ? ' tseg-highlight' : ''}`}
        >
          <div className="tseg-meta">
            <span className="tseg-speaker">{SPEAKER_LABEL[role]}</span>
            <span className="tseg-time">{formatClock(seg.start_time_ms)}</span>
          </div>
          <div>
            <p className="tseg-text">{seg.text}</p>
            {role === 'unknown' && (
              <div className="tseg-role-ask">
                <span>Кто говорит?</span>
                <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => onSetRole(seg.id, 'candidate')}>
                  Кандидат
                </button>
                <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => onSetRole(seg.id, 'interviewer')}>
                  Интервьюер
                </button>
              </div>
            )}
          </div>
          {role !== 'unknown' && (
            <div className="tseg-tools">
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={busy}
                onClick={() => onSetRole(seg.id, otherRole)}
                title={`Отметить как реплику: ${SPEAKER_LABEL[otherRole].toLowerCase()}`}
              >
                <ArrowLeftRight />
                <span>{SPEAKER_LABEL[otherRole]}</span>
              </button>
            </div>
          )}
        </div>
      );
    };

    const rows = groups.map((group) => {
      const idx = group.questionId ? questionIndex(group.questionId) : -1;
      const first = group.segments[0];
      return (
        <section key={`group-${first.id}`} className="tgroup">
          <div
            className={`tdivider${group.questionId && group.questionId === currentQuestionId ? ' tdivider-current' : ''}`}
            data-question-divider={group.questionId ?? ''}
          >
            <span>{idx >= 0 ? `Вопрос ${idx + 1} · ${questionTitle(questions[idx])}` : 'Вне плана вопросов'}</span>
            <span className="tdivider-time">{formatClock(first.start_time_ms)}</span>
          </div>
          {group.segments.map(renderSegment)}
        </section>
      );
    });

    return (
      <div className="transcript-wrap">
        <div className="transcript-head">
          <span className="pane-title">Стенограмма</span>
          <span className="pane-meta">{segments.length > 0 ? `${segments.length} ${pluralRu(segments.length, ['реплика', 'реплики', 'реплик'])}` : ''}</span>
        </div>
        <div ref={scrollRef} className="transcript" onScroll={handleScroll}>
          {segments.length === 0 ? (
            <div className="transcript-empty">
              <MessageSquare />
              <span>{isPaused ? 'Запись на паузе' : 'Ждём первую реплику — текст появится через несколько секунд после речи'}</span>
            </div>
          ) : (
            rows
          )}
        </div>
        {hasUnseen && (
          <button type="button" className="btn btn-primary btn-sm jump-latest" onClick={() => scrollToBottom(true)}>
            <ArrowDown />
            <span>Новые реплики</span>
          </button>
        )}
      </div>
    );
  }
);

LiveTranscript.displayName = 'LiveTranscript';
