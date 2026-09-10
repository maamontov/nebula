import React, { useState, useEffect } from 'react';
import { AudioDevice, InterviewPlan } from '../types';
import { getAudioDevices, createInterview, updateInterviewStatus, startAudioCapture } from '../services/api';
import { User, Briefcase, Mic, Volume2, ShieldAlert, Play } from 'lucide-react';

interface SetupScreenProps {
  onInterviewStarted: (interviewId: string, plan: InterviewPlan) => void;
}

const DEFAULT_PLAN: InterviewPlan = {
  id: 'plan-default',
  title: 'Senior Backend / Systems Engineer',
  role: 'Senior Backend Engineer',
  questions: [
    {
      id: 'q-saga',
      title: 'Распределённые транзакции (Saga)',
      prompt: 'Как вы проектируете распределённые транзакции в микросервисах? Расскажите про паттерн Saga и механизмы компенсации.',
      text: 'Как вы проектируете распределённые транзакции в микросервисах? Расскажите про паттерн Saga и механизмы компенсации.',
      order_index: 0,
      weight: 1.5,
      criteria: [
        {
          id: 'crit-arch-saga',
          title: 'Понимание Saga & Orchestration',
          description: 'Понимание различий оркестрации и хореографии, идемпотентность компенсирующих транзакций',
          min_score: 1.0,
          max_score: 5.0,
          weight: 1.0,
        },
      ],
    },
    {
      id: 'q-storage',
      title: 'Выбор хранилища и партиционирование',
      prompt: 'В каких случаях вы выбираете PostgreSQL вместо NoSQL и как организуете партиционирование при высоких нагрузках?',
      text: 'В каких случаях вы выбираете PostgreSQL вместо NoSQL и как организуете партиционирование при высоких нагрузках?',
      order_index: 1,
      weight: 1.0,
      criteria: [
        {
          id: 'crit-db-tradeoffs',
          title: 'Выбор хранилища и шардирование',
          description: 'Знание ACID, индексов, типов репликации и партиций по диапазонам/хешу',
          min_score: 1.0,
          max_score: 5.0,
          weight: 1.0,
        },
      ],
    },
  ],
};

export const SetupScreen: React.FC<SetupScreenProps> = ({ onInterviewStarted }) => {
  const [candidateName, setCandidateName] = useState('Алексей Смирнов');
  const [role, setRole] = useState('Senior Backend Engineer');
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [selectedMic, setSelectedMic] = useState<string>('');
  const [selectedSpeaker, setSelectedSpeaker] = useState<string>('');
  const [consentGiven, setConsentGiven] = useState(false);
  const [plan] = useState<InterviewPlan>(DEFAULT_PLAN);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');

  useEffect(() => {
    getAudioDevices().then((devs) => {
      setDevices(devs);
      if (devs.length > 0) {
        setSelectedMic(devs[0].id);
        setSelectedSpeaker(devs.length > 1 ? devs[1].id : devs[0].id);
      }
    });
  }, []);

  const handleStart = async () => {
    if (!consentGiven) {
      setErrorMsg('Необходимо зафиксировать явное согласие участников перед началом записи.');
      return;
    }
    if (!candidateName.trim()) {
      setErrorMsg('Укажите ФИО кандидата.');
      return;
    }

    setIsSubmitting(true);
    setErrorMsg('');

    try {
      const interviewId = `inv-${Date.now().toString(36)}`;
      await createInterview({
        id: interviewId,
        title: `${role} — ${candidateName}`,
        candidate_name: candidateName,
        role: role,
        plan: plan,
      });

      // Update status to RECORDING with consent audit timestamp
      await updateInterviewStatus(interviewId, 'draft', 'ready');
      await updateInterviewStatus(interviewId, 'ready', 'recording', {
        confirmedAt: new Date().toISOString(),
        version: 'consent-v1.0-ru',
      });

      // Start audio capture in background
      await startAudioCapture(interviewId, selectedMic, selectedSpeaker, './spool', true);

      onInterviewStarted(interviewId, plan);
    } catch (err: any) {
      setErrorMsg(err.message || 'Ошибка запуска интервью');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="max-w-5xl mx-auto p-8 space-y-8 overflow-y-auto h-[calc(100vh-4rem)]">
      <div>
        <h2 className="text-2xl font-bold text-slate-100">Новое интервью</h2>
        <p className="text-sm text-slate-400 mt-1">
          Настройте план вопросов, выберите аудиоустройства и зафиксируйте согласие участников.
        </p>
      </div>

      {errorMsg && (
        <div className="p-4 bg-rose-950/50 border border-rose-800 rounded-lg flex items-center space-x-3 text-rose-300 text-sm">
          <ShieldAlert className="w-5 h-5 flex-shrink-0" />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Candidate & Role Info */}
      <div className="glass-panel p-6 rounded-xl space-y-4">
        <h3 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">Кандидат и позиция</h3>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">ФИО Кандидата</label>
            <div className="relative">
              <User className="w-4 h-4 text-slate-500 absolute left-3 top-2.5" />
              <input
                type="text"
                value={candidateName}
                onChange={(e) => setCandidateName(e.target.value)}
                className="w-full bg-slate-900 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
                placeholder="Иван Иванов"
              />
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">Целевая роль</label>
            <div className="relative">
              <Briefcase className="w-4 h-4 text-slate-500 absolute left-3 top-2.5" />
              <input
                type="text"
                value={role}
                onChange={(e) => setRole(e.target.value)}
                className="w-full bg-slate-900 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
                placeholder="Senior Backend Engineer"
              />
            </div>
          </div>
        </div>
      </div>

      {/* Audio Device Selection */}
      <div className="glass-panel p-6 rounded-xl space-y-4">
        <h3 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">Аудиозахват (2 канала)</h3>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">Микрофон интервьюера</label>
            <div className="relative">
              <Mic className="w-4 h-4 text-slate-500 absolute left-3 top-2.5" />
              <select
                value={selectedMic}
                onChange={(e) => setSelectedMic(e.target.value)}
                className="w-full bg-slate-900 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
              >
                {devices.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} {d.is_default ? '(По умолчанию)' : ''}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-400 mb-1">Звук кандидата (Loopback / Звонок)</label>
            <div className="relative">
              <Volume2 className="w-4 h-4 text-slate-500 absolute left-3 top-2.5" />
              <select
                value={selectedSpeaker}
                onChange={(e) => setSelectedSpeaker(e.target.value)}
                className="w-full bg-slate-900 border border-slate-700 rounded-lg pl-9 pr-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
              >
                {devices.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>
      </div>

      {/* Questions Plan */}
      <div className="glass-panel p-6 rounded-xl space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">План вопросов ({plan.questions.length})</h3>
        </div>
        <div className="space-y-3">
          {plan.questions.map((q, idx) => (
            <div key={q.id} className="p-3 bg-slate-900/60 border border-slate-800 rounded-lg flex items-start justify-between">
              <div className="space-y-1">
                <div className="flex items-center space-x-2">
                  <span className="text-xs font-bold text-indigo-400">Вопрос #{idx + 1}</span>
                  <span className="text-xs text-slate-500">(Вес: {q.weight})</span>
                </div>
                <p className="text-sm text-slate-200">{q.text}</p>
                <div className="flex items-center space-x-2 pt-1">
                  {q.criteria.map((c) => (
                    <span key={c.id} className="text-[11px] px-2 py-0.5 bg-slate-800 text-slate-400 rounded">
                      {c.title} (1-5)
                    </span>
                  ))}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Privacy Consent (Mandatory Gate) */}
      <div className="p-4 bg-indigo-950/30 border border-indigo-800/60 rounded-xl flex items-start space-x-3">
        <input
          type="checkbox"
          id="consent-check"
          checked={consentGiven}
          onChange={(e) => setConsentGiven(e.target.checked)}
          className="mt-1 w-4 h-4 text-indigo-600 rounded bg-slate-900 border-slate-700 focus:ring-indigo-500"
        />
        <label htmlFor="consent-check" className="text-xs text-slate-300 leading-relaxed cursor-pointer select-none">
          <strong className="text-slate-100">Фиксация согласия на запись (152-ФЗ / GDPR):</strong> Я подтверждаю, что кандидат и интервьюер проинформированы о ведении аудиозаписи и дали явное согласие на обработку технического содержания ответов.
        </label>
      </div>

      {/* Action CTA */}
      <div className="flex justify-end pb-8">
        <button
          onClick={handleStart}
          disabled={!consentGiven || isSubmitting}
          className="flex items-center space-x-2 px-6 py-3 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white font-medium text-sm rounded-lg shadow-lg shadow-indigo-600/30 transition"
        >
          <Play className="w-4 h-4 fill-white" />
          <span>{isSubmitting ? 'Запуск сессии...' : 'Начать интервью'}</span>
        </button>
      </div>
    </div>
  );
};
