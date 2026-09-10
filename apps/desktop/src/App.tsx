import React, { useState, useEffect } from 'react';
import { Header } from './components/Header';
import { SetupScreen } from './screens/SetupScreen';
import { LiveSessionScreen } from './screens/LiveSessionScreen';
import { ReviewScreen } from './screens/ReviewScreen';
import { InterviewPlan, InterviewStatus } from './types';
import { getActiveSession, getInterview } from './services/api';

type Screen = 'setup' | 'live' | 'review';

export const App: React.FC = () => {
  const [screen, setScreen] = useState<Screen>('setup');
  const [interviewId, setInterviewId] = useState<string>('');
  const [plan, setPlan] = useState<InterviewPlan | null>(null);

  useEffect(() => {
    getActiveSession().then(async (activeSessionId) => {
      if (activeSessionId) {
        try {
          const data = await getInterview(activeSessionId);
          if (data && data.interview) {
            setInterviewId(activeSessionId);
            if (data.plan) {
              setPlan(data.plan);
            }
            if (data.interview.status === 'review' || data.interview.status === 'finalized') {
              setScreen('review');
            } else {
              setScreen('live');
            }
          }
        } catch (e) {
          console.error('Failed to restore active session:', e);
        }
      }
    });
  }, []);

  const getStatus = (): InterviewStatus => {
    switch (screen) {
      case 'setup':
        return 'draft';
      case 'live':
        return 'recording';
      case 'review':
        return 'review';
      default:
        return 'draft';
    }
  };

  const handleInterviewStarted = (id: string, newPlan: InterviewPlan) => {
    setInterviewId(id);
    setPlan(newPlan);
    setScreen('live');
  };

  const handleFinishSession = (id: string) => {
    setInterviewId(id);
    setScreen('review');
  };

  const handleNewInterview = () => {
    setInterviewId('');
    setPlan(null);
    setScreen('setup');
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col select-none">
      <Header
        status={getStatus()}
        candidateName={plan?.title || 'Новое интервью'}
        role={plan?.role || 'Подготовка'}
        isCapturing={screen === 'live'}
      />

      <main className="flex-1 overflow-hidden">
        {screen === 'setup' && <SetupScreen onInterviewStarted={handleInterviewStarted} />}
        {screen === 'live' && plan && (
          <LiveSessionScreen
            interviewId={interviewId}
            plan={plan}
            onFinishSession={handleFinishSession}
          />
        )}
        {screen === 'review' && plan && (
          <ReviewScreen
            interviewId={interviewId}
            plan={plan}
            onNewInterview={handleNewInterview}
          />
        )}
      </main>
    </div>
  );
};
