import React, { useState, useEffect } from 'react';
import { AudioDevice, CaptureMode, InterviewPlan } from '../types';
import { getAudioDevices, createInterview, updateInterviewStatus, startAudioCapture, stopAudioCapture } from '../services/api';
import { User, Briefcase, Mic, Volume2, ShieldAlert, Play, Radio, Layers } from 'lucide-react';

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
  const [captureMode, setCaptureMode] = useState<CaptureMode>('dual_source');
  const [selectedMic, setSelectedMic] = useState<string>('');
  const [selectedSpeaker, setSelectedSpeaker] = useState<string>('');
  const [selectedShared, setSelectedShared] = useState<string>('');
  const [consentGiven, setConsentGiven] = useState(false);
  const [plan] = useState<InterviewPlan>(DEFAULT_PLAN);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [preparedInterviewId, setPreparedInterviewId] = useState<string | null>(null);

  useEffect(() => {
    getAudioDevices().then((devs) => {
      setDevices(devs);
      if (devs.length === 1) {
        setCaptureMode('single_source');
        setSelectedShared(devs[0].id);
        setSelectedMic(devs[0].id);
      } else if (devs.length > 1) {
        setSelectedMic(devs[0].id);
        setSelectedSpeaker(devs[1].id);
        setSelectedShared(devs[0].id);
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

    if (captureMode === 'single_source') {
      if (!selectedShared) {
        setErrorMsg('Необходимо выбрать аудиоустройство для захвата общего звука.');
        return;
      }
    } else {
      if (!selectedMic || !selectedSpeaker) {
        setErrorMsg('Необходимо выбрать аудиоустройства для микрофона и динамиков/встречи.');
        return;
      }
      if (selectedMic === selectedSpeaker) {
        setErrorMsg('Канал интервьюера и канал кандидата должны использовать разные аудиоустройства (или выберите режим "Один общий источник").');
        return;
      }
    }

    setIsSubmitting(true);
    setErrorMsg('');

    let currentInterviewId = preparedInterviewId;
    try {
      if (!currentInterviewId) {
        currentInterviewId = `inv-${Date.now().toString(36)}`;
        await createInterview({
          id: currentInterviewId,
          title: `${role} — ${candidateName}`,
          candidate_name: candidateName,
          role: role,
          plan: plan,
          capture_mode: captureMode,
        });
        setPreparedInterviewId(currentInterviewId);
      }

      // S4: 1. Advance state to READY first
      await updateInterviewStatus(currentInterviewId, 'draft', 'ready');

      // S4: 2. Start audio capture (if it fails, interview remains READY, not corrupted RECORDING)
      if (captureMode === 'single_source') {
        await startAudioCapture({
          sessionId: currentInterviewId,
          sharedDevId: selectedShared,
          consentGiven: true,
          captureMode: 'single_source',
        });
      } else {
        await startAudioCapture({
          sessionId: currentInterviewId,
          interviewerDevId: selectedMic,
          candidateDevId: selectedSpeaker,
          consentGiven: true,
          captureMode: 'dual_source',
        });
      }

      // S4: 3. Audio capture verified running -> advance to RECORDING with consent timestamp
      await updateInterviewStatus(currentInterviewId, 'ready', 'recording', {
        confirmedAt: new Date().toISOString(),
        version: 'consent-v1.0-ru',
      });

      onInterviewStarted(currentInterviewId, plan);
    } catch (err: any) {
      // S3: Extract exact error string from Tauri rejection or HTTP error
      const message = typeof err === 'string'
        ? err
        : err?.message || (typeof err === 'object' ? JSON.stringify(err) : String(err));
      setErrorMsg(message || 'Ошибка запуска интервью');

      // S4: Compensation - clean up any partially opened audio streams
      await stopAudioCapture().catch(() => {});
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleModeChange = (mode: CaptureMode) => {
    setCaptureMode(mode);
    if (mode === 'single_source' && !selectedShared && devices.length > 0) {
      const defaultDev = devices.find((d) => d.is_default) || devices[0];
      setSelectedShared(selectedMic || defaultDev.id);
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

      {/* Audio Capture Mode & Device Selection */}
      <div className="glass-panel p-6 rounded-xl space-y-5">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-200 uppercase tracking-wider">
            Режим аудиозахвата
          </h3>
          <div className="flex items-center space-x-2 bg-slate-900 p-1 rounded-lg border border-slate-800 text-xs">
            <button
              type="button"
              onClick={() => handleModeChange('dual_source')}
              className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md font-medium transition ${
                captureMode === 'dual_source'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <Layers className="w-3.5 h-3.5" />
              <span>2 канала (Раздельно)</span>
            </button>
            <button
              type="button"
              onClick={() => handleModeChange('single_source')}
              className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md font-medium transition ${
                captureMode === 'single_source'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <Radio className="w-3.5 h-3.5" />
              <span>1 общий источник (Микшер / Микрофон)</span>
            </button>
          </div>
        </div>

        {devices.length === 1 && captureMode === 'dual_source' && (
          <div className="p-3 bg-amber-950/40 border border-amber-800/60 rounded-lg text-xs text-amber-300 flex items-center justify-between">
            <span>Обнаружено только 1 аудиоустройство ввода. Для продолжения рекомендуется режим одного источника.</span>
            <button
              type="button"
              onClick={() => handleModeChange('single_source')}
              className="px-2.5 py-1 bg-amber-700 hover:bg-amber-600 text-white font-medium rounded text-xs ml-3 whitespace-nowrap"
            >
              Включить 1 источник
            </button>
          </div>
        )}

        {captureMode === 'single_source' ? (
          <div>
            <label className="flex items-center space-x-2 text-xs font-medium text-slate-400 mb-1.5">
              <Radio className="w-4 h-4 text-indigo-400" />
              <span>Общее аудиоустройство (вход / микрофон / микс встречи)</span>
            </label>
            <select
              value={selectedShared}
              onChange={(e) => setSelectedShared(e.target.value)}
              className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2.5 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
            >
              {devices.length === 0 && <option value="">Устройства ввода не обнаружены</option>}
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} {d.is_default ? '(По умолчанию)' : ''}
                </option>
              ))}
            </select>
            <p className="text-[11px] text-slate-500 mt-1.5">
              В режиме одного источника обе стороны записываются в единую дорожку. Реплики интервьюера и кандидата размечаются на этапе ревью вручную.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="flex items-center space-x-2 text-xs font-medium text-slate-400 mb-1.5">
                <Mic className="w-4 h-4 text-indigo-400" />
                <span>Микрофон интервьюера</span>
              </label>
              <select
                value={selectedMic}
                onChange={(e) => setSelectedMic(e.target.value)}
                className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2.5 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
              >
                {devices.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} {d.is_default ? '(По умолчанию)' : ''}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="flex items-center space-x-2 text-xs font-medium text-slate-400 mb-1.5">
                <Volume2 className="w-4 h-4 text-indigo-400" />
                <span>Звук кандидата (Loopback / Звонок)</span>
              </label>
              <select
                value={selectedSpeaker}
                onChange={(e) => setSelectedSpeaker(e.target.value)}
                className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2.5 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
              >
                {devices.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
        )}
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

      {/* Action CTA & Validation Hint */}
      <div className="space-y-3 pb-8">
        {errorMsg && (
          <div className="p-4 bg-rose-950/60 border border-rose-700 rounded-xl flex items-center space-x-3 text-rose-200 text-sm shadow-md">
            <ShieldAlert className="w-5 h-5 flex-shrink-0 text-rose-400" />
            <div className="flex-1 font-mono text-xs break-all">{errorMsg}</div>
          </div>
        )}

        <div className="flex items-center justify-between">
          <div className="text-xs text-slate-400">
            {!consentGiven ? (
              <span className="text-amber-400/90 flex items-center space-x-1">
                <span>Подтвердите согласие участников для разблокировки старта</span>
              </span>
            ) : captureMode === 'dual_source' && (!selectedMic || !selectedSpeaker) ? (
              <span className="text-amber-400/90">Выберите оба аудиоустройства</span>
            ) : captureMode === 'single_source' && !selectedShared ? (
              <span className="text-amber-400/90">Выберите аудиоустройство</span>
            ) : (
              <span className="text-emerald-400/90">Все обязательные параметры заполнены</span>
            )}
          </div>

          <button
            onClick={handleStart}
            disabled={!consentGiven || isSubmitting || (captureMode === 'dual_source' && (!selectedMic || !selectedSpeaker)) || (captureMode === 'single_source' && !selectedShared)}
            className="flex items-center space-x-2 px-6 py-3 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-white font-medium text-sm rounded-lg shadow-lg shadow-indigo-600/30 transition"
          >
            <Play className="w-4 h-4 fill-white" />
            <span>{isSubmitting ? 'Запуск сессии...' : 'Начать интервью'}</span>
          </button>
        </div>
      </div>
    </div>
  );
};
