import React, { useState, useEffect } from 'react';
import { Header } from './components/Header';
import { HomeScreen } from './screens/HomeScreen';
import { JobTemplatesScreen } from './screens/JobTemplatesScreen';
import { SetupScreen } from './screens/SetupScreen';
import { LiveSessionScreen } from './screens/LiveSessionScreen';
import { ReviewScreen } from './screens/ReviewScreen';
import { InterviewPlan, InterviewStatus, CaptureMode } from './types';
import { getActiveSession, getInterview } from './services/api';
import { AlertCircle, X } from 'lucide-react';

type Screen = 'home' | 'templates' | 'setup' | 'live' | 'review';

export const App: React.FC = () => {
  const [screen, setScreen] = useState<Screen>('home');
  const [interviewId, setInterviewId] = useState<string>('');
  const [plan, setPlan] = useState<InterviewPlan | null>(null);
  const [interviewStatus, setInterviewStatus] = useState<InterviewStatus>('draft');

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
              if (data.plan) {
                setPlan(data.plan);
                setActiveRecordingSession({
                  interviewId: activeSessionId,
                  candidateName: data.plan.title,
                  role: data.plan.role,
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

  const handleNavigate = (targetScreen: 'home' | 'templates' | 'live') => {
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
    setInterviewStatus('draft');
    setScreen('setup');
  };

  const handleOpenInterview = async (id: string, status: InterviewStatus) => {
    if (activeRecordingSession && activeRecordingSession.interviewId === id) {
      setInterviewId(id);
      setPlan(activeRecordingSession.plan);
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

      if (actualStatus === 'draft' || actualStatus === 'ready') {
        setSetupExistingInterviewId(id);
        setScreen('setup');
      } else if (actualStatus === 'recording' || actualStatus === 'paused') {
        const mode = data.interview?.capture_mode as CaptureMode | undefined;
        if (data.plan) {
          setActiveRecordingSession({
            interviewId: id,
            candidateName: data.plan.title,
            role: data.plan.role,
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

  const handleInterviewStarted = (id: string, newPlan: InterviewPlan, captureMode?: CaptureMode) => {
    setInterviewId(id);
    setPlan(newPlan);
    setInterviewStatus('recording');
    setActiveRecordingSession({
      interviewId: id,
      candidateName: newPlan.title,
      role: newPlan.role,
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

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col select-none">
      <Header
        status={getStatus()}
        candidateName={
          screen === 'live' || screen === 'review' || screen === 'setup'
            ? plan?.title || 'Новое интервью'
            : undefined
        }
        role={
          screen === 'live' || screen === 'review' || screen === 'setup'
            ? plan?.role || 'Подготовка'
            : undefined
        }
        isCapturing={Boolean(activeRecordingSession)}
        captureMode={activeRecordingSession?.captureMode}
        currentScreen={screen}
        onNavigate={handleNavigate}
        activeRecordingSession={activeRecordingSession}
      />

      <main className="flex-1 overflow-hidden relative">
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
              onFinishSession={handleFinishSession}
            />
          </div>
        )}

        {/* Screen: Review */}
        {screen === 'review' && plan && (
          <ReviewScreen
            interviewId={interviewId}
            plan={plan}
            onNewInterview={handleNewInterview}
            onBackToHome={handleBackToHome}
          />
        )}
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
