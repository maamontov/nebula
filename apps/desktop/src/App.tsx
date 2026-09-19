import React, { useState, useEffect } from 'react';
import { Header } from './components/Header';
import { HomeScreen } from './screens/HomeScreen';
import { JobTemplatesScreen } from './screens/JobTemplatesScreen';
import { SetupScreen } from './screens/SetupScreen';
import { LiveSessionScreen } from './screens/LiveSessionScreen';
import { ReviewScreen } from './screens/ReviewScreen';
import { SettingsScreen } from './screens/SettingsScreen';
import { InterviewPlan, InterviewStatus, CaptureMode } from './types';
import { getActiveSession, getBackendStatus, getInterview, restartBackend } from './services/api';
import type { BackendStatusInfo } from './services/api';
import { AlertCircle, X } from 'lucide-react';

type Screen = 'home' | 'templates' | 'setup' | 'live' | 'review' | 'settings';
type Theme = 'light' | 'dark';

export const App: React.FC = () => {
  const [theme, setTheme] = useState<Theme>(() => {
    if (typeof window === 'undefined') return 'light';
    return window.localStorage.getItem('nebula-theme') === 'dark' ? 'dark' : 'light';
  });
  const [screen, setScreen] = useState<Screen>('home');
  const [interviewId, setInterviewId] = useState<string>('');
  const [plan, setPlan] = useState<InterviewPlan | null>(null);
  const [interviewStatus, setInterviewStatus] = useState<InterviewStatus>('draft');
  const [currentCandidateName, setCurrentCandidateName] = useState<string>('');
  const [currentRole, setCurrentRole] = useState<string>('');

  // Persistent active recording state
  const [activeRecordingSession, setActiveRecordingSession] = useState<{
    interviewId: string;
    candidateName: string;
    role: string;
    plan: InterviewPlan;
    captureMode?: CaptureMode;
    isPaused?: boolean;
    elapsedMs?: number;
  } | null>(null);

  // Existing draft ID to resume in SetupScreen
  const [setupExistingInterviewId, setSetupExistingInterviewId] = useState<string | null>(null);

  // Warning modal for blocked actions
  const [warningModal, setWarningModal] = useState<string | null>(null);

  // Startup gate: embedded backend must be ready before the UI is usable.
  const [backendStatus, setBackendStatus] = useState<BackendStatusInfo | null>(null);
  const [isRestartingBackend, setIsRestartingBackend] = useState(false);

  /**
   * Опрашивает статус backend, пока он поднимается. `failed` больше не означает
   * аварийное завершение приложения: пользователь видит причину и может повторить запуск.
   */
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const status = await getBackendStatus();
        if (cancelled) return;
        setBackendStatus(status);
        if (status.state === 'starting') {
          timer = setTimeout(poll, 1000);
        }
      } catch (e) {
        if (cancelled) return;
        console.warn('getBackendStatus failed or running outside Tauri:', e);
        setBackendStatus({
          state: 'ready',
          mode: 'external',
          backend_url: 'http://127.0.0.1:17843',
          message: null,
          hint: null,
          log_path: null,
        });
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  const handleRetryBackend = async () => {
    setIsRestartingBackend(true);
    try {
      const status = await restartBackend();
      setBackendStatus(status);
      if (status.state === 'starting') {
        let attempts = 0;
        while (attempts < 120) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const next = await getBackendStatus();
          setBackendStatus(next);
          if (next.state !== 'starting') break;
          attempts += 1;
        }
      }
    } catch (e) {
      setBackendStatus({
        state: 'failed',
        mode: null,
        backend_url: 'http://127.0.0.1:17843',
        message: e instanceof Error ? e.message : String(e),
        hint: 'Перезапустите приложение.',
        log_path: null,
      });
    } finally {
      setIsRestartingBackend(false);
    }
  };

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    window.localStorage.setItem('nebula-theme', theme);
  }, [theme]);

  useEffect(() => {
    getActiveSession()
      .then(async (sessionInfo) => {
        if (sessionInfo && sessionInfo.is_recording && sessionInfo.session_id) {
          const activeSessionId = sessionInfo.session_id;
          try {
            const data = await getInterview(activeSessionId);
            if (data && data.interview) {
              setInterviewId(activeSessionId);
              const restoredStatus = sessionInfo.is_paused ? 'paused' : data.interview.status;
              setInterviewStatus(restoredStatus);
              const mode = data.interview.capture_mode as CaptureMode | undefined;
              const candName = data.interview.candidate_name || data.plan?.candidate_name || '';
              const candRole = data.interview.role || data.plan?.role || '';
              setCurrentCandidateName(candName);
              setCurrentRole(candRole);
              if (data.plan) {
                setPlan(data.plan);
                setActiveRecordingSession({
                  interviewId: activeSessionId,
                  candidateName: candName || 'Кандидат',
                  role: candRole || 'Должность не указана',
                  plan: data.plan,
                  captureMode: mode,
                  isPaused: sessionInfo.is_paused,
                  elapsedMs: sessionInfo.elapsed_ms,
                });
              }
              if (
                data.interview.status === 'review' ||
                data.interview.status === 'finalized' ||
                data.interview.status === 'processing'
              ) {
                setScreen('review');
              } else {
                setScreen('live');
              }
            }
          } catch (e) {
            console.error('Failed to restore active session:', e);
          }
        }
      })
      .catch((e) => {
        console.warn('getActiveSession failed or running outside Tauri:', e);
      });
  }, []);

  const getStatus = (): InterviewStatus => {
    if (activeRecordingSession && screen === 'live') {
      return activeRecordingSession.isPaused ? 'paused' : interviewStatus === 'paused' ? 'paused' : 'recording';
    }
    switch (screen) {
      case 'setup':
        return 'draft';
      case 'live':
        return 'recording';
      case 'review':
        return interviewStatus;
      default:
        return 'draft';
    }
  };

  const handleNavigate = (targetScreen: 'home' | 'templates' | 'live' | 'settings') => {
    if (targetScreen === 'live') {
      if (activeRecordingSession) {
        setInterviewId(activeRecordingSession.interviewId);
        setPlan(activeRecordingSession.plan);
        setScreen('live');
      } else if (interviewId && plan) {
        setScreen('live');
      }
    } else {
      setScreen(targetScreen);
    }
  };

  const handleNewInterview = () => {
    if (activeRecordingSession) {
      setWarningModal(
        'В данный момент идёт активная запись другого собеседования. Завершите или остановите текущую запись перед началом нового интервью.'
      );
      return;
    }
    setSetupExistingInterviewId(null);
    setInterviewId('');
    setPlan(null);
    setCurrentCandidateName('');
    setCurrentRole('');
    setInterviewStatus('draft');
    setScreen('setup');
  };

  const handleOpenInterview = async (id: string, status: InterviewStatus) => {
    if (activeRecordingSession && activeRecordingSession.interviewId === id) {
      setInterviewId(id);
      setPlan(activeRecordingSession.plan);
      setCurrentCandidateName(activeRecordingSession.candidateName);
      setCurrentRole(activeRecordingSession.role);
      setScreen('live');
      return;
    }

    if (activeRecordingSession && (status === 'recording' || status === 'paused')) {
      setWarningModal(
        'В данный момент уже идёт активная запись другого собеседования. Завершите текущую запись перед переходом.'
      );
      return;
    }

    try {
      const data = await getInterview(id);
      setInterviewId(id);
      if (data.plan) {
        setPlan(data.plan);
      }
      const actualStatus = data.interview?.status || status;
      setInterviewStatus(actualStatus);

      const candName = data.interview?.candidate_name || data.plan?.candidate_name || '';
      const candRole = data.interview?.role || data.plan?.role || '';
      setCurrentCandidateName(candName);
      setCurrentRole(candRole);

      if (actualStatus === 'draft' || actualStatus === 'ready') {
        setSetupExistingInterviewId(id);
        setScreen('setup');
      } else if (actualStatus === 'recording' || actualStatus === 'paused') {
        const mode = data.interview?.capture_mode as CaptureMode | undefined;
        if (data.plan) {
          setActiveRecordingSession({
            interviewId: id,
            candidateName: candName || 'Кандидат',
            role: candRole || 'Должность не указана',
            plan: data.plan,
            captureMode: mode,
            isPaused: actualStatus === 'paused',
          });
        }
        setScreen('live');
      } else {
        // processing, review, finalized
        setScreen('review');
      }
    } catch (err: any) {
      console.error('Failed to load interview:', err);
      alert(`Не удалось открыть собеседование: ${err.message || err}`);
    }
  };

  const handleInterviewStarted = (
    id: string,
    newPlan: InterviewPlan,
    captureMode?: CaptureMode,
    startedCandidateName?: string,
    startedRole?: string
  ) => {
    setInterviewId(id);
    setPlan(newPlan);
    setInterviewStatus('recording');
    const resolvedCandName = startedCandidateName || newPlan.candidate_name || currentCandidateName || 'Кандидат';
    const resolvedRole = startedRole || newPlan.role || currentRole || 'Должность не указана';
    setCurrentCandidateName(resolvedCandName);
    setCurrentRole(resolvedRole);
    setActiveRecordingSession({
      interviewId: id,
      candidateName: resolvedCandName,
      role: resolvedRole,
      plan: newPlan,
      captureMode,
      isPaused: false,
      elapsedMs: 0,
    });
    setScreen('live');
  };

  const handleFinishSession = (id: string) => {
    setActiveRecordingSession(null);
    setInterviewId(id);
    setInterviewStatus('review');
    setScreen('review');
  };

  const handleBackToHome = () => {
    setScreen('home');
  };

  // Backend ещё не ответил: показываем прогресс вместо пустого или частично
  // нерабочего интерфейса.
  if (!backendStatus || backendStatus.state === 'starting') {
    return (
      <div className="app-root min-h-screen flex flex-col items-center justify-center gap-4 select-none">
        <div className="flex items-center gap-3">
          <span className="w-4 h-4 rounded-full border-2 border-indigo-400 border-t-transparent animate-spin" />
          <h1 className="text-sm font-semibold text-slate-200">Nebula запускается…</h1>
        </div>
        <p className="text-xs text-slate-400">{backendStatus?.message || 'Подготовка встроенного backend'}</p>
      </div>
    );
  }

  // Backend не поднялся: показываем причину и даём retry вместо аварийного выхода.
  if (backendStatus.state === 'failed') {
    return (
      <div className="app-root min-h-screen flex flex-col items-center justify-center p-4 select-none">
        <div className="bg-slate-900 border border-slate-800 rounded-xl max-w-lg w-full p-6 space-y-4 shadow-2xl">
          <div className="flex items-center space-x-2 text-red-400">
            <AlertCircle className="w-5 h-5" />
            <h3 className="text-base font-bold text-white">Backend не запустился</h3>
          </div>

          <p className="text-xs text-slate-300 leading-relaxed">
            {backendStatus.message || 'Встроенный backend Nebula недоступен.'}
          </p>

          {backendStatus.hint && (
            <p className="text-xs text-amber-300/90 leading-relaxed">{backendStatus.hint}</p>
          )}

          {backendStatus.log_path && (
            <p className="text-[11px] text-slate-500 break-all">Лог: {backendStatus.log_path}</p>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              disabled={isRestartingBackend}
              onClick={handleRetryBackend}
              className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs font-semibold rounded-lg shadow transition cursor-pointer"
            >
              {isRestartingBackend ? 'Перезапуск…' : 'Повторить'}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="app-root min-h-screen flex flex-col select-none">
      <Header
        status={getStatus()}
        candidateName={
          screen === 'live' || screen === 'review' || screen === 'setup'
            ? currentCandidateName || plan?.candidate_name || (screen === 'setup' ? 'Подготовка интервью' : 'Кандидат')
            : undefined
        }
        role={
          screen === 'live' || screen === 'review' || screen === 'setup'
            ? currentRole || plan?.role || (screen === 'setup' ? 'Выбор должности' : 'Позиция не указана')
            : undefined
        }
        isCapturing={Boolean(activeRecordingSession)}
        captureMode={activeRecordingSession?.captureMode}
        currentScreen={screen}
        onNavigate={handleNavigate}
        activeRecordingSession={activeRecordingSession}
        theme={theme}
        onToggleTheme={() => setTheme((current) => (current === 'light' ? 'dark' : 'light'))}
      />

      <main className="app-main flex-1 overflow-hidden relative">
        {/* Screen: Home */}
        {screen === 'home' && (
          <HomeScreen
            onOpenInterview={handleOpenInterview}
            onNewInterview={handleNewInterview}
            hasActiveRecording={Boolean(activeRecordingSession)}
          />
        )}

        {/* Screen: Templates */}
        {screen === 'templates' && <JobTemplatesScreen />}

        {/* Screen: Setup */}
        {screen === 'setup' && (
          <SetupScreen
            existingInterviewId={setupExistingInterviewId}
            onInterviewStarted={handleInterviewStarted}
            onBackToHome={handleBackToHome}
          />
        )}

        {/* Persistent Live Session Screen when active recording is underway */}
        {activeRecordingSession && (
          <div className={screen === 'live' ? 'h-full w-full' : 'hidden'}>
            <LiveSessionScreen
              interviewId={activeRecordingSession.interviewId}
              plan={activeRecordingSession.plan}
              candidateName={activeRecordingSession.candidateName}
              role={activeRecordingSession.role}
              onFinishSession={handleFinishSession}
            />
          </div>
        )}

        {/* Live Session Screen if not in activeRecordingSession (fallback) */}
        {!activeRecordingSession && screen === 'live' && plan && (
          <div className="h-full w-full">
            <LiveSessionScreen
              interviewId={interviewId}
              plan={plan}
              candidateName={currentCandidateName}
              role={currentRole}
              onFinishSession={handleFinishSession}
            />
          </div>
        )}

        {/* Screen: Review */}
        {screen === 'review' && plan && (
          <ReviewScreen
            interviewId={interviewId}
            plan={plan}
            candidateName={currentCandidateName}
            role={currentRole}
            onNewInterview={handleNewInterview}
            onBackToHome={handleBackToHome}
          />
        )}

        {/* Screen: Settings */}
        {screen === 'settings' && <SettingsScreen />}
      </main>

      {/* Warning Modal */}
      {warningModal && (
        <div className="fixed inset-0 bg-black/75 backdrop-blur-xs z-50 flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-800 rounded-xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center justify-between">
              <div className="flex items-center space-x-2 text-amber-400">
                <AlertCircle className="w-5 h-5" />
                <h3 className="text-base font-bold text-white">Внимание</h3>
              </div>
              <button
                type="button"
                onClick={() => setWarningModal(null)}
                className="text-slate-400 hover:text-white cursor-pointer"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <p className="text-xs text-slate-300 leading-relaxed">{warningModal}</p>

            <div className="flex justify-end pt-2">
              <button
                type="button"
                onClick={() => setWarningModal(null)}
                className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold rounded-lg shadow transition cursor-pointer"
              >
                Понятно
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
