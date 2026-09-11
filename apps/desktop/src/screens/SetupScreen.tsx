import React, { useState, useEffect } from 'react';
import {
  AudioDevice,
  CaptureMode,
  InterviewPlan,
  JobTemplate,
  PlannedQuestion,
} from '../types';
import {
  getAudioDevices,
  createInterview,
  updateInterviewDraft,
  updateInterviewStatus,
  startAudioCapture,
  stopAudioCapture,
  getInterview,
  listJobTemplates,
  getInterviewPlanReadiness,
} from '../services/api';
import {
  User,
  Briefcase,
  Mic,
  Volume2,
  ShieldAlert,
  Play,
  Save,
  ChevronUp,
  ChevronDown,
  Plus,
  Trash2,
  ArrowLeft,
  RotateCcw,
  CheckCircle2,
  AlertCircle,
} from 'lucide-react';

interface SetupScreenProps {
  existingInterviewId?: string | null;
  onInterviewStarted: (interviewId: string, plan: InterviewPlan, captureMode?: CaptureMode) => void;
  onBackToHome?: () => void;
}

export const SetupScreen: React.FC<SetupScreenProps> = ({
  existingInterviewId,
  onInterviewStarted,
  onBackToHome,
}) => {
  const [candidateName, setCandidateName] = useState('');
  const [role, setRole] = useState('');
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [captureMode, setCaptureMode] = useState<CaptureMode>('dual_source');
  const [selectedMic, setSelectedMic] = useState<string>('');
  const [selectedSpeaker, setSelectedSpeaker] = useState<string>('');
  const [selectedShared, setSelectedShared] = useState<string>('');
  const [consentGiven, setConsentGiven] = useState(false);

  // Templates
  const [templates, setTemplates] = useState<JobTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string>('');
  const [showApplyTemplateWarning, setShowApplyTemplateWarning] = useState(false);
  const [pendingTemplateId, setPendingTemplateId] = useState<string | null>(null);

  // Plan state (isolated snapshot for this interview)
  const [plan, setPlan] = useState<InterviewPlan>({
    id: 'plan-custom',
    title: 'Подготовка интервью',
    role: '',
    questions: [],
  });

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isSavingDraft, setIsSavingDraft] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [draftSavedNotice, setDraftSavedNotice] = useState(false);
  const [currentInterviewId, setCurrentInterviewId] = useState<string | null>(existingInterviewId || null);

  // Load Audio Devices
  useEffect(() => {
    getAudioDevices()
      .then((devs) => {
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
      })
      .catch((err) => {
        console.warn('Failed to load audio devices:', err);
      });
  }, []);

  // Load Templates and Existing Interview
  useEffect(() => {
    async function init() {
      try {
        const tpls = await listJobTemplates(false);
        setTemplates(tpls);

        if (existingInterviewId) {
          const data = await getInterview(existingInterviewId);
          if (data && data.interview) {
            setCurrentInterviewId(existingInterviewId);
            setCandidateName(data.interview.candidate_name || '');
            setRole(data.interview.role || data.plan?.role || '');
            if (data.interview.template_id) {
              setSelectedTemplateId(data.interview.template_id);
            }
            if (data.plan) {
              setPlan(data.plan);
            }
            if (data.interview.capture_mode) {
              setCaptureMode(data.interview.capture_mode);
            }
          }
        } else {
          // New interview: default to first template if available
          if (tpls.length > 0) {
            const first = tpls[0];
            const resolvedRole = first.role?.trim() || first.title || '';
            setSelectedTemplateId(first.id);
            setRole(resolvedRole);
            setPlan({
              id: `plan-${Date.now()}`,
              title: first.title,
              role: resolvedRole,
              questions: JSON.parse(JSON.stringify(first.questions || [])),
            });
          }
        }
      } catch (err: any) {
        console.error('Error initializing setup screen:', err);
      }
    }
    init();
  }, [existingInterviewId]);

  const handleTemplateChange = (newTemplateId: string) => {
    if (!newTemplateId) return;
    const targetTpl = templates.find((t) => t.id === newTemplateId);
    if (!targetTpl) return;

    if (plan.questions.length > 0) {
      setPendingTemplateId(newTemplateId);
      setShowApplyTemplateWarning(true);
    } else {
      applyTemplate(targetTpl);
    }
  };

  const applyTemplate = (tpl: JobTemplate) => {
    setSelectedTemplateId(tpl.id);
    const resolvedRole = tpl.role?.trim() || tpl.title || '';
    setRole(resolvedRole);
    setPlan({
      id: `plan-${Date.now()}`,
      title: tpl.title,
      role: resolvedRole,
      questions: JSON.parse(JSON.stringify(tpl.questions || [])),
    });
    setPendingTemplateId(null);
    setShowApplyTemplateWarning(false);
  };

  const handleSaveDraft = async () => {
    setIsSavingDraft(true);
    setErrorMsg('');
    try {
      let invId = currentInterviewId;
      const effectiveRole = role.trim();
      const invTitle = `${effectiveRole || 'Интервью'} — ${candidateName.trim() || 'Черновик'}`;
      const updatedPlan = {
        ...plan,
        role: effectiveRole || plan.role || 'Позиция не указана',
      };

      if (!invId) {
        invId = `inv-${Date.now().toString(36)}`;
        await createInterview({
          id: invId,
          title: invTitle,
          candidate_name: candidateName.trim() || 'Без имени',
          role: effectiveRole || 'Позиция не указана',
          plan: updatedPlan,
          capture_mode: captureMode,
        });
        setCurrentInterviewId(invId);
      } else {
        await updateInterviewDraft(invId, {
          title: invTitle,
          candidate_name: candidateName.trim() || 'Без имени',
          role: effectiveRole || 'Позиция не указана',
          plan: updatedPlan,
          template_id: selectedTemplateId || undefined,
          capture_mode: captureMode,
        });
      }

      setDraftSavedNotice(true);
      setTimeout(() => setDraftSavedNotice(false), 3500);
    } catch (err: any) {
      setErrorMsg(`Ошибка сохранения черновика: ${err.message || err}`);
    } finally {
      setIsSavingDraft(false);
    }
  };

  const handleStart = async () => {
    if (!candidateName.trim()) {
      setErrorMsg('Укажите реальное ФИО кандидата перед началом записи.');
      return;
    }
    if (!role.trim()) {
      setErrorMsg('Укажите должность или роль кандидата.');
      return;
    }
    if (!consentGiven) {
      setErrorMsg('Необходимо подтвердить явное согласие участников перед началом записи.');
      return;
    }
    if (plan.questions.length === 0) {
      setErrorMsg('Добавьте хотя бы один вопрос в план интервью.');
      return;
    }

    // Validate plan questions and criteria
    for (const [idx, q] of plan.questions.entries()) {
      if (!q.title?.trim() && !q.prompt?.trim()) {
        setErrorMsg(`Вопрос #${idx + 1} должен содержать название или формулировку.`);
        return;
      }
      if (!q.criteria || q.criteria.length === 0) {
        setErrorMsg(`Вопрос «${q.title || idx + 1}» должен содержать хотя бы один критерий оценки.`);
        return;
      }
    }

    // Validate audio devices
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

    let invId = currentInterviewId;
    const effectiveRole = role.trim();
    const invTitle = `${effectiveRole} — ${candidateName.trim()}`;
    const updatedPlan = {
      ...plan,
      role: effectiveRole,
    };

    try {
      if (!invId) {
        invId = `inv-${Date.now().toString(36)}`;
        await createInterview({
          id: invId,
          title: invTitle,
          candidate_name: candidateName.trim(),
          role: effectiveRole,
          plan: updatedPlan,
          capture_mode: captureMode,
        });
        setCurrentInterviewId(invId);
      } else {
        await updateInterviewDraft(invId, {
          title: invTitle,
          candidate_name: candidateName.trim(),
          role: effectiveRole,
          plan: updatedPlan,
          template_id: selectedTemplateId || undefined,
          capture_mode: captureMode,
        });
      }

      // Check backend plan readiness
      const readiness = await getInterviewPlanReadiness(invId);
      if (!readiness.is_ready) {
        throw new Error(readiness.errors.join('; ') || 'План интервью не готов к проведению');
      }

      // 1. Advance state to READY
      await updateInterviewStatus(invId, 'draft', 'ready');

      // 2. Start audio capture
      if (captureMode === 'single_source') {
        await startAudioCapture({
          sessionId: invId,
          sharedDevId: selectedShared,
          consentGiven: true,
          captureMode: 'single_source',
        });
      } else {
        await startAudioCapture({
          sessionId: invId,
          interviewerDevId: selectedMic,
          candidateDevId: selectedSpeaker,
          consentGiven: true,
          captureMode: 'dual_source',
        });
      }

      // 3. Advance to RECORDING with consent confirmation
      await updateInterviewStatus(invId, 'ready', 'recording', {
        confirmedAt: new Date().toISOString(),
        version: 'consent-v1.0-ru',
      });

      onInterviewStarted(invId, plan, captureMode);
    } catch (err: any) {
      const message = typeof err === 'string'
        ? err
        : err?.message || (typeof err === 'object' ? JSON.stringify(err) : String(err));
      setErrorMsg(message || 'Ошибка запуска интервью');

      // Compensation: stop partially opened streams
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
    <div className="max-w-5xl mx-auto p-6 md:p-8 space-y-8 overflow-y-auto h-[calc(100vh-4rem)]">
      {/* Top Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 glass-panel p-6 rounded-2xl shadow-xl">
        <div className="flex items-center space-x-3">
          {onBackToHome && (
            <button
              onClick={onBackToHome}
              className="p-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-xl transition"
              title="Вернуться к списку собеседований"
            >
              <ArrowLeft className="w-5 h-5" />
            </button>
          )}
          <div>
            <h2 className="text-xl font-bold text-slate-100 flex items-center space-x-2">
              <span>{existingInterviewId ? 'Редактирование подготовки' : 'Подготовка интервью'}</span>
              {currentInterviewId && (
                <span className="text-xs font-mono text-slate-400 bg-slate-800 px-2 py-0.5 rounded border border-slate-700">
                  {currentInterviewId}
                </span>
              )}
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Кандидат → Должность → Вопросы → Сохранение черновика → Запись
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-3">
          {draftSavedNotice && (
            <span className="flex items-center space-x-1.5 text-xs text-emerald-400 font-medium">
              <CheckCircle2 className="w-4 h-4" />
              <span>Черновик сохранён</span>
            </span>
          )}

          <button
            onClick={handleSaveDraft}
            disabled={isSavingDraft || isSubmitting}
            className="flex items-center space-x-1.5 px-4 py-2 text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-xl transition disabled:opacity-50"
          >
            <Save className="w-3.5 h-3.5" />
            <span>{isSavingDraft ? 'Сохранение...' : 'Сохранить черновик'}</span>
          </button>
        </div>
      </div>

      {errorMsg && (
        <div className="p-4 bg-rose-950/60 border border-rose-800 rounded-xl flex items-center space-x-3 text-rose-300 text-sm shadow-md">
          <ShieldAlert className="w-5 h-5 flex-shrink-0 text-rose-400" />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* 1. Candidate & Position */}
      <div className="glass-panel p-6 rounded-2xl space-y-4 shadow-md">
        <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider flex items-center space-x-2">
          <span className="w-5 h-5 rounded-full bg-indigo-900/60 text-indigo-300 flex items-center justify-center text-[10px] font-mono">1</span>
          <span>Кандидат и должность</span>
        </h3>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-slate-300 flex items-center space-x-1.5">
              <User className="w-3.5 h-3.5 text-slate-400" />
              <span>ФИО Кандидата</span>
            </label>
            <input
              type="text"
              value={candidateName}
              onChange={(e) => setCandidateName(e.target.value)}
              placeholder="Введите ФИО реального кандидата"
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2 text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-slate-300 flex items-center space-x-1.5">
              <Briefcase className="w-3.5 h-3.5 text-slate-400" />
              <span>Шаблон должности</span>
            </label>
            <div className="flex items-center space-x-2">
              <div className="flex-1">
                <select
                  value={selectedTemplateId}
                  onChange={(e) => handleTemplateChange(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500 cursor-pointer"
                >
                  <option value="" disabled>Выберите шаблон должности...</option>
                  {templates.map((tpl) => (
                    <option key={tpl.id} value={tpl.id} className="bg-slate-900">
                      {tpl.title} ({tpl.level || 'Middle'})
                    </option>
                  ))}
                </select>
              </div>

              {selectedTemplateId && (
                <button
                  type="button"
                  onClick={() => {
                    const tpl = templates.find((t) => t.id === selectedTemplateId);
                    if (tpl) {
                      setPendingTemplateId(tpl.id);
                      setShowApplyTemplateWarning(true);
                    }
                  }}
                  title="Применить актуальные вопросы из шаблона"
                  className="p-2 bg-slate-900 hover:bg-slate-800 border border-slate-800 text-slate-300 rounded-lg transition"
                >
                  <RotateCcw className="w-4 h-4" />
                </button>
              )}
            </div>
          </div>
        </div>

        <div className="space-y-1.5">
          <label className="text-xs font-semibold text-slate-300">Название роли в интервью</label>
          <input
            type="text"
            value={role}
            onChange={(e) => {
              const val = e.target.value;
              setRole(val);
              setPlan((prev) => ({ ...prev, role: val }));
            }}
            placeholder="например, Backend или Data Science"
            className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2 text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
          />
        </div>
      </div>

      {/* 2. Questions Customization */}
      <div className="glass-panel p-6 rounded-2xl space-y-4 shadow-md">
        <div className="flex items-center justify-between border-b border-slate-800 pb-3">
          <div>
            <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider flex items-center space-x-2">
              <span className="w-5 h-5 rounded-full bg-indigo-900/60 text-indigo-300 flex items-center justify-center text-[10px] font-mono">2</span>
              <span>План вопросов собеседования</span>
            </h3>
            <p className="text-xs text-slate-400 mt-0.5">
              Копия плана для этого интервью. Изменения не затрагивают шаблон должности.
            </p>
          </div>

          <button
            type="button"
            onClick={() => {
              const newQ: PlannedQuestion = {
                id: `q-custom-${Date.now()}`,
                title: `Вопрос ${plan.questions.length + 1}`,
                prompt: '',
                weight: 1.0,
                criteria: [
                  {
                    id: `crit-${Date.now()}`,
                    title: 'Полнота ответа',
                    description: '',
                    min_score: 1.0,
                    max_score: 5.0,
                    weight: 1.0,
                  },
                ],
              };
              setPlan((prev) => ({ ...prev, questions: [...prev.questions, newQ] }));
            }}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-xs font-semibold text-indigo-300 hover:text-white bg-indigo-950/60 hover:bg-indigo-600 border border-indigo-800/80 rounded-lg transition"
          >
            <Plus className="w-3.5 h-3.5" />
            <span>Добавить вопрос</span>
          </button>
        </div>

        {plan.questions.length === 0 ? (
          <div className="p-8 text-center text-xs text-slate-500 border border-dashed border-slate-800 rounded-xl">
            Вопросы ещё не добавлены. Выберите шаблон выше или добавьте вопрос вручную.
          </div>
        ) : (
          <div className="space-y-4">
            {plan.questions.map((q, qIdx) => (
              <div
                key={q.id}
                className="bg-slate-950/80 border border-slate-800/90 p-4 rounded-xl space-y-3"
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center space-x-2 flex-1">
                    <span className="w-6 h-6 rounded-full bg-indigo-950 text-indigo-300 border border-indigo-800 flex items-center justify-center text-xs font-mono font-bold">
                      {qIdx + 1}
                    </span>
                    <input
                      type="text"
                      value={q.title || ''}
                      onChange={(e) => {
                        const updated = [...plan.questions];
                        updated[qIdx] = { ...q, title: e.target.value };
                        setPlan((prev) => ({ ...prev, questions: updated }));
                      }}
                      placeholder="Название вопроса..."
                      className="text-sm font-semibold bg-transparent border-b border-transparent hover:border-slate-700 focus:border-indigo-500 focus:outline-none text-slate-100 px-1 py-0.5 w-full"
                    />
                  </div>

                  <div className="flex items-center space-x-2 text-xs">
                    <div className="flex items-center space-x-1 bg-slate-900 border border-slate-800 rounded px-2 py-0.5">
                      <span className="text-slate-500 text-[11px]">Вес:</span>
                      <input
                        type="number"
                        step="0.1"
                        min="0.1"
                        value={q.weight}
                        onChange={(e) => {
                          const w = parseFloat(e.target.value) || 1.0;
                          const updated = [...plan.questions];
                          updated[qIdx] = { ...q, weight: w };
                          setPlan((prev) => ({ ...prev, questions: updated }));
                        }}
                        className="w-10 bg-transparent text-right font-mono text-slate-200 focus:outline-none"
                      />
                    </div>

                    <button
                      disabled={qIdx === 0}
                      onClick={() => {
                        const updated = [...plan.questions];
                        const t = updated[qIdx];
                        updated[qIdx] = updated[qIdx - 1];
                        updated[qIdx - 1] = t;
                        setPlan((prev) => ({ ...prev, questions: updated }));
                      }}
                      className="p-1 text-slate-400 hover:text-slate-200 disabled:opacity-30 rounded"
                    >
                      <ChevronUp className="w-4 h-4" />
                    </button>
                    <button
                      disabled={qIdx === plan.questions.length - 1}
                      onClick={() => {
                        const updated = [...plan.questions];
                        const t = updated[qIdx];
                        updated[qIdx] = updated[qIdx + 1];
                        updated[qIdx + 1] = t;
                        setPlan((prev) => ({ ...prev, questions: updated }));
                      }}
                      className="p-1 text-slate-400 hover:text-slate-200 disabled:opacity-30 rounded"
                    >
                      <ChevronDown className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => {
                        const updated = plan.questions.filter((_, i) => i !== qIdx);
                        setPlan((prev) => ({ ...prev, questions: updated }));
                      }}
                      className="p-1 text-slate-400 hover:text-rose-400 rounded"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>

                <textarea
                  rows={2}
                  value={q.prompt || q.text || ''}
                  onChange={(e) => {
                    const updated = [...plan.questions];
                    updated[qIdx] = { ...q, prompt: e.target.value, text: e.target.value };
                    setPlan((prev) => ({ ...prev, questions: updated }));
                  }}
                  placeholder="Формулировка вопроса..."
                  className="w-full bg-slate-900 border border-slate-800 rounded px-3 py-1.5 text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
                />

                {/* Criteria */}
                <div className="pl-4 border-l-2 border-slate-800 space-y-2">
                  <div className="flex items-center justify-between text-[11px] text-slate-400 font-semibold">
                    <span>Критерии рубрики</span>
                    <button
                      type="button"
                      onClick={() => {
                        const updated = [...plan.questions];
                        updated[qIdx].criteria.push({
                          id: `crit-${Date.now()}`,
                          title: 'Новый критерий',
                          description: '',
                          min_score: 1.0,
                          max_score: 5.0,
                          weight: 1.0,
                        });
                        setPlan((prev) => ({ ...prev, questions: updated }));
                      }}
                      className="text-indigo-400 hover:text-indigo-300"
                    >
                      + Добавить критерий
                    </button>
                  </div>

                  {q.criteria.map((c, cIdx) => (
                    <div key={c.id} className="flex items-center gap-2 text-xs">
                      <input
                        type="text"
                        value={c.title}
                        onChange={(e) => {
                          const updated = [...plan.questions];
                          updated[qIdx].criteria[cIdx].title = e.target.value;
                          setPlan((prev) => ({ ...prev, questions: updated }));
                        }}
                        className="bg-slate-900 border border-slate-800 rounded px-2 py-1 text-slate-200 flex-1 focus:outline-none"
                      />
                      <span className="text-[11px] text-slate-500 font-mono">
                        {c.min_score}–{c.max_score}
                      </span>
                      <button
                        onClick={() => {
                          if (q.criteria.length <= 1) return;
                          const updated = [...plan.questions];
                          updated[qIdx].criteria = updated[qIdx].criteria.filter((_, i) => i !== cIdx);
                          setPlan((prev) => ({ ...prev, questions: updated }));
                        }}
                        className="text-slate-500 hover:text-rose-400 p-1"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 3. Audio & Consent */}
      <div className="glass-panel p-6 rounded-2xl space-y-6 shadow-md">
        <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider flex items-center space-x-2">
          <span className="w-5 h-5 rounded-full bg-indigo-900/60 text-indigo-300 flex items-center justify-center text-[10px] font-mono">3</span>
          <span>Настройка звука и согласие</span>
        </h3>

        {/* Capture Mode Toggle */}
        <div className="flex space-x-3 bg-slate-950 p-1 rounded-xl border border-slate-800 max-w-md">
          <button
            type="button"
            onClick={() => handleModeChange('dual_source')}
            className={`flex-1 py-1.5 px-3 text-xs font-semibold rounded-lg transition ${
              captureMode === 'dual_source'
                ? 'bg-indigo-600 text-white shadow'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Два источника (Раздельно)
          </button>
          <button
            type="button"
            onClick={() => handleModeChange('single_source')}
            className={`flex-1 py-1.5 px-3 text-xs font-semibold rounded-lg transition ${
              captureMode === 'single_source'
                ? 'bg-indigo-600 text-white shadow'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Один общий источник
          </button>
        </div>

        {/* Audio Device Pickers */}
        {captureMode === 'single_source' ? (
          <div className="space-y-1.5 max-w-md">
            <label className="text-xs font-semibold text-slate-300">Общее аудиоустройство</label>
            <div className="relative">
              <Mic className="w-4 h-4 text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
              <select
                value={selectedShared}
                onChange={(e) => setSelectedShared(e.target.value)}
                className="w-full bg-slate-950 border border-slate-800 rounded-lg pl-9 pr-4 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500 cursor-pointer"
              >
                {devices.map((d) => (
                  <option key={d.id} value={d.id} className="bg-slate-900">
                    {d.name} {d.is_default ? '(По умолчанию)' : ''}
                  </option>
                ))}
              </select>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-slate-300">Микрофон интервьюера</label>
              <div className="relative">
                <Mic className="w-4 h-4 text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
                <select
                  value={selectedMic}
                  onChange={(e) => setSelectedMic(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg pl-9 pr-4 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500 cursor-pointer"
                >
                  {devices.map((d) => (
                    <option key={d.id} value={d.id} className="bg-slate-900">
                      {d.name} {d.is_default ? '(По умолчанию)' : ''}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-slate-300">Звук кандидата (Динамики / Встреча)</label>
              <div className="relative">
                <Volume2 className="w-4 h-4 text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
                <select
                  value={selectedSpeaker}
                  onChange={(e) => setSelectedSpeaker(e.target.value)}
                  className="w-full bg-slate-950 border border-slate-800 rounded-lg pl-9 pr-4 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500 cursor-pointer"
                >
                  {devices.map((d) => (
                    <option key={d.id} value={d.id} className="bg-slate-900">
                      {d.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </div>
        )}

        {/* Consent Checkbox */}
        <div className="p-4 bg-indigo-950/30 border border-indigo-800/60 rounded-xl space-y-2">
          <label className="flex items-start space-x-3 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={consentGiven}
              onChange={(e) => setConsentGiven(e.target.checked)}
              className="mt-0.5 rounded bg-slate-900 border-slate-700 text-indigo-600 focus:ring-0"
            />
            <div className="text-xs text-slate-300">
              <span className="font-semibold text-slate-100">Согласие на запись: </span>
              Кандидат и все участники встречи проинформированы о записи аудио и формировании стенограммы с оценкой компетенций.
            </div>
          </label>
        </div>

        {/* Start Button */}
        <div className="flex items-center justify-between pt-2">
          <span className="text-xs text-slate-500">
            После начала записи план и рубрика будут зафиксированы.
          </span>

          <button
            onClick={handleStart}
            disabled={isSubmitting}
            className="flex items-center space-x-2 px-6 py-3 rounded-xl font-bold text-sm bg-rose-600 hover:bg-rose-500 text-white transition shadow-lg shadow-rose-600/30 disabled:opacity-50"
          >
            <Play className="w-4 h-4 fill-white" />
            <span>{isSubmitting ? 'Запуск...' : 'Начать запись'}</span>
          </button>
        </div>
      </div>

      {/* Apply Template Warning Modal */}
      {showApplyTemplateWarning && pendingTemplateId && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-800 p-6 rounded-2xl max-w-md w-full shadow-2xl space-y-4">
            <h3 className="text-base font-bold text-slate-100 flex items-center space-x-2">
              <AlertCircle className="w-5 h-5 text-amber-400" />
              <span>Замена плана вопросами из шаблона</span>
            </h3>
            <p className="text-sm text-slate-300">
              Применение нового шаблона заменит текущие вопросы и критерии в черновике вопросами из выбранной должности.
            </p>
            <p className="text-xs text-slate-400">
              Все индивидуальные правки, сделанные для этого черновика, будут перезаписаны.
            </p>
            <div className="flex items-center justify-end space-x-3 pt-2">
              <button
                onClick={() => {
                  setShowApplyTemplateWarning(false);
                  setPendingTemplateId(null);
                }}
                className="px-4 py-2 text-xs font-semibold text-slate-400 hover:text-slate-200 bg-slate-800 rounded-lg transition"
              >
                Отмена
              </button>
              <button
                onClick={() => {
                  const tpl = templates.find((t) => t.id === pendingTemplateId);
                  if (tpl) applyTemplate(tpl);
                }}
                className="px-4 py-2 text-xs font-semibold text-white bg-amber-600 hover:bg-amber-500 rounded-lg transition"
              >
                Да, применить шаблон
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
