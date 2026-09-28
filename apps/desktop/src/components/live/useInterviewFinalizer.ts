import { useCallback, useRef, useState } from 'react';
import { TrackManifest } from '../../types';
import {
  getActiveSession,
  getInterviewReadiness,
  getUploadProgress,
  InterviewReadiness,
  retryFailedTranscription,
  stopAudioCapture,
  stopInterview,
  updateInterviewStatus,
} from '../../services/api';
import { StopStep, StopStepKey, StopStepState } from './StopSessionDialog';
import { describeSttError } from './liveUtils';

const READINESS_ATTEMPTS = 120;
const READINESS_INTERVAL_MS = 500;
// After this long the dialog offers to open review without waiting for the transcript.
const LONG_WAIT_MS = 12000;
// Readiness states where all audio is stored and only the transcript is incomplete.
const INCOMPLETE_TRANSCRIPT_STATES = ['STT_FAILED', 'STT_IN_PROGRESS'];

const STEP_LABELS: Record<StopStepKey, string> = {
  capture: 'Остановка записи',
  finalize: 'Сохранение аудиодорожек',
  upload: 'Выгрузка и распознавание аудио',
  review: 'Подготовка экрана проверки',
};

const initialSteps = (alreadyStopped: boolean): StopStep[] =>
  (Object.keys(STEP_LABELS) as StopStepKey[]).map((key) => ({
    key,
    label: STEP_LABELS[key],
    state: alreadyStopped && (key === 'capture' || key === 'finalize') ? 'done' : 'pending',
  }));

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function describeNotReady(readiness: InterviewReadiness | null): string {
  switch (readiness?.state) {
    case 'AWAITING_CHUNKS':
    case 'AWAITING_MANIFEST':
      return 'Не все фрагменты аудио дошли до сервиса обработки. Они догружаются автоматически: можно закрыть окно и открыть интервью позже с главной страницы.';
    case 'STT_IN_PROGRESS':
    case 'PROCESSING':
      return 'Распознавание речи ещё идёт. Можно подождать или открыть проверку сейчас — недостающие реплики появятся позже.';
    default:
      return `Обработка аудио ещё не завершена${readiness?.details ? ` (${readiness.details})` : ''}.`;
  }
}

class NotReadyError extends Error {}

interface FinalizerOptions {
  interviewId: string;
  /** Recording was already stopped earlier (interview reopened in `processing`). */
  alreadyStopped?: boolean;
  onFinished: (interviewId: string) => void;
}

/**
 * Stops the recording and moves the interview to review. Every failure leaves the user a way out:
 * retry, open review with an incomplete transcript (audio is safe), or come back later.
 */
export function useInterviewFinalizer({ interviewId, alreadyStopped = false, onFinished }: FinalizerOptions) {
  const [steps, setSteps] = useState<StopStep[]>(() => initialSteps(alreadyStopped));
  const [error, setError] = useState<string | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [canAcceptIncomplete, setCanAcceptIncomplete] = useState(false);
  const [waitingLong, setWaitingLong] = useState(false);
  const [captureStopped, setCaptureStopped] = useState(alreadyStopped);
  const progressRef = useRef<{
    captureStopped: boolean;
    interviewStopped: boolean;
    manifests: Array<TrackManifest | Record<string, unknown>>;
    sttFailed: boolean;
  }>({ captureStopped: alreadyStopped, interviewStopped: alreadyStopped, manifests: [], sttFailed: false });
  // A newer run (e.g. "don't wait") supersedes the one that is still polling.
  const runIdRef = useRef(0);

  const setStep = useCallback((key: StopStepKey, state: StopStepState, detail?: string | null) => {
    setSteps((prev) => prev.map((s) => (s.key === key ? { ...s, state, detail: detail === undefined ? s.detail : detail } : s)));
  }, []);

  const run = useCallback(
    async (options: { acceptIncomplete?: boolean } = {}) => {
      const runId = ++runIdRef.current;
      const isCurrent = () => runId === runIdRef.current;
      const progress = progressRef.current;
      const acceptIncomplete = Boolean(options.acceptIncomplete);

      setIsRunning(true);
      setError(null);
      setWaitingLong(false);
      // Steps that did not finish start over, so no stale error marks remain.
      setSteps((prev) => prev.map((s) => (s.state === 'done' ? s : { ...s, state: 'pending', detail: null })));

      let step: StopStepKey = 'capture';
      try {
        if (!progress.captureStopped) {
          setStep('capture', 'active');
          const session = await getActiveSession().catch(() => null);
          // The app may have been restarted mid-recording: then there is no capture to stop.
          const captureRunning = !session || session.session_id === interviewId;
          const result = captureRunning ? await stopAudioCapture() : { manifests: [] as TrackManifest[] };
          progress.manifests = Array.isArray(result.manifests)
            ? result.manifests
            : result.manifests && typeof result.manifests === 'object'
            ? Object.values(result.manifests)
            : [];
          progress.captureStopped = true;
          setCaptureStopped(true);
        }
        setStep('capture', 'done');

        step = 'finalize';
        if (!progress.interviewStopped) {
          setStep('finalize', 'active');
          await stopInterview(interviewId, progress.manifests);
          progress.interviewStopped = true;
        }
        setStep('finalize', 'done');

        step = 'upload';
        if (acceptIncomplete) {
          setStep('upload', 'done', 'стенограмма неполная — недостающие реплики можно перераспознать на экране проверки');
        } else {
          setStep('upload', 'active', null);
          if (progress.sttFailed) {
            // Explicit retry after a recognition failure: re-queue the failed jobs first.
            await retryFailedTranscription(interviewId);
            progress.sttFailed = false;
          }
          const startedAt = Date.now();
          let last: InterviewReadiness | null = null;
          let ready = false;
          for (let attempt = 0; attempt < READINESS_ATTEMPTS && !ready; attempt++) {
            if (!isCurrent()) return;
            try {
              const [upload, readiness] = await Promise.all([
                getUploadProgress(interviewId).catch(() => null),
                getInterviewReadiness(interviewId),
              ]);
              if (!isCurrent()) return;
              last = readiness;
              setCanAcceptIncomplete(INCOMPLETE_TRANSCRIPT_STATES.includes(readiness.state));
              if (readiness.is_ready) {
                ready = true;
                break;
              }
              if (readiness.state === 'STT_FAILED') {
                // Permanent failure: waiting will not help, report the reason right away.
                progress.sttFailed = true;
                const failed = readiness.stt_jobs?.failed ?? 0;
                throw new NotReadyError(
                  `Не удалось распознать ${failed > 0 ? `${failed} фрагм. ` : ''}речи: ${describeSttError(readiness.failed_error)}. ` +
                    'Аудио сохранено. Исправьте настройки распознавания и нажмите «Повторить» — или откройте проверку сейчас и перераспознайте запись позже.'
                );
              }
              const parts: string[] = [];
              if (upload && upload.total_chunks > 0) parts.push(`выгружено ${upload.uploaded_chunks} из ${upload.total_chunks} фрагментов`);
              const pending = readiness.stt_jobs?.pending ?? 0;
              if (pending > 0) parts.push(`распознаётся фрагментов: ${pending}`);
              setStep('upload', 'active', parts.join(' · ') || null);
            } catch (e) {
              if (e instanceof NotReadyError) throw e;
              // transient network error: keep polling
            }
            if (Date.now() - startedAt > LONG_WAIT_MS) setWaitingLong(true);
            await new Promise((r) => setTimeout(r, READINESS_INTERVAL_MS));
          }
          if (!ready) throw new NotReadyError(describeNotReady(last));
          setStep('upload', 'done', null);
        }

        step = 'review';
        if (!isCurrent()) return;
        setStep('review', 'active');
        await updateInterviewStatus(interviewId, 'processing', 'review', undefined, {
          allowIncompleteTranscript: acceptIncomplete,
        });
        setStep('review', 'done');
        onFinished(interviewId);
      } catch (e) {
        if (!isCurrent()) return;
        setStep(step, 'error');
        setError(errorText(e));
      } finally {
        if (isCurrent()) {
          setIsRunning(false);
          setWaitingLong(false);
        }
      }
    },
    [interviewId, onFinished, setStep]
  );

  /** Abandons an in-flight wait (the interview stays in `processing` and can be reopened). */
  const cancel = useCallback(() => {
    runIdRef.current += 1;
    setIsRunning(false);
    setWaitingLong(false);
  }, []);

  const reset = useCallback(() => {
    runIdRef.current += 1;
    setSteps(initialSteps(progressRef.current.captureStopped));
    setError(null);
    setIsRunning(false);
    setWaitingLong(false);
  }, []);

  return { steps, error, isRunning, canAcceptIncomplete, waitingLong, captureStopped, run, cancel, reset };
}
