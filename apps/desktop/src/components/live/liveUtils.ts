import { AssessmentProposal, PlannedQuestion, TranscriptSegment } from '../../types';

export type ResolvedRole = 'candidate' | 'interviewer' | 'unknown';

/** mm:ss (or h:mm:ss) on the capture timeline. */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = m.toString().padStart(2, '0');
  const ss = s.toString().padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Explicit speaker role wins; otherwise the capture track decides (shared track stays unknown). */
export function resolveRole(seg: TranscriptSegment): ResolvedRole {
  if (seg.speaker_role === 'candidate' || seg.speaker_role === 'interviewer') return seg.speaker_role;
  if (seg.track_id === 'candidate' || seg.track_id === 'interviewer') return seg.track_id;
  return 'unknown';
}

export function questionTitle(q: PlannedQuestion | undefined): string {
  if (!q) return '';
  return q.title || q.prompt || q.text || q.id;
}

export function questionPrompt(q: PlannedQuestion | undefined): string {
  if (!q) return '';
  return q.prompt || q.text || q.title || '';
}

/** Average of a proposal's criterion scores normalised to the criteria scales, or null. */
export function proposalAverage(
  proposal: AssessmentProposal | undefined,
  question: PlannedQuestion | undefined
): { value: number; max: number } | null {
  if (!proposal || proposal.is_rejected) return null;
  const scored = proposal.scores.filter((s) => typeof s.score === 'number');
  if (scored.length === 0) return null;
  const maxFor = (criterionId: string) =>
    question?.criteria.find((c) => c.id === criterionId)?.max_score || 5;
  const max = scored.length === 1 ? maxFor(scored[0].criterion_id) : 5;
  const normalised = scored.map((s) => (s.score / maxFor(s.criterion_id)) * max);
  const value = normalised.reduce((a, b) => a + b, 0) / normalised.length;
  return { value: Math.round(value * 10) / 10, max };
}

export function formatScore(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

/** Human-readable reason for a speech recognition failure reported by the backend. */
export function describeSttError(raw: string | null | undefined): string {
  if (!raw) return 'сервис распознавания речи вернул ошибку';
  if (/api key|bearer|unauthori[sz]ed|\b401\b|\b403\b/i.test(raw)) return 'не задан или неверен API-ключ распознавания речи';
  if (/timed? ?out/i.test(raw)) return 'сервис распознавания речи не ответил вовремя';
  if (/connect|network|resolve|unreachable/i.test(raw)) return 'нет связи с сервисом распознавания речи';
  return raw;
}
