import React, { useState, useEffect, useCallback } from 'react';
import { JobTemplate, PlannedQuestion, RubricCriterion } from '../types';
import {
  listJobTemplates,
  createJobTemplate,
  updateJobTemplate,
  duplicateJobTemplate,
  archiveJobTemplate,
  unarchiveJobTemplate,
  deleteJobTemplate,
  copyQuestionToTemplate,
} from '../services/api';
import {
  Briefcase,
  Plus,
  Copy,
  Archive,
  ArchiveRestore,
  Trash2,
  ChevronUp,
  ChevronDown,
  Save,
  Search,
  CheckCircle2,
  AlertCircle,
  Layers,
  ArrowRight,
} from 'lucide-react';

export const JobTemplatesScreen: React.FC = () => {
  const [templates, setTemplates] = useState<JobTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [saveSuccessMsg, setSaveSuccessMsg] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  // Active editable form state
  const [editingTemplate, setEditingTemplate] = useState<{
    id?: string;
    title: string;
    role: string;
    level: string;
    description: string;
    questions: PlannedQuestion[];
  }>({
    title: '',
    role: '',
    level: 'Middle',
    description: '',
    questions: [],
  });

  // Modal for copying question to another template
  const [copyQuestionModal, setCopyQuestionModal] = useState<{
    questionId: string;
    questionTitle: string;
    targetTemplateId: string;
  } | null>(null);

  const fetchTemplates = useCallback(async () => {
    setIsLoading(true);
    try {
      const data = await listJobTemplates(includeArchived);
      setTemplates(data);
      if (data.length > 0 && !selectedTemplateId) {
        setSelectedTemplateId(data[0].id);
      }
    } catch (err: any) {
      setErrorMsg(err.message || 'Ошибка загрузки шаблонов должностей');
    } finally {
      setIsLoading(false);
    }
  }, [includeArchived, selectedTemplateId]);

  useEffect(() => {
    fetchTemplates();
  }, [fetchTemplates]);

  // Load selected template into editor
  useEffect(() => {
    if (!selectedTemplateId) return;
    const tpl = templates.find((t) => t.id === selectedTemplateId);
    if (tpl) {
      setEditingTemplate({
        id: tpl.id,
        title: tpl.title,
        role: tpl.role,
        level: tpl.level || 'Middle',
        description: tpl.description || '',
        questions: JSON.parse(JSON.stringify(tpl.questions || [])),
      });
      setSaveSuccessMsg(null);
    }
  }, [selectedTemplateId, templates]);

  const handleCreateNew = () => {
    const newTpl = {
      title: 'Новая должность',
      role: 'Software Engineer',
      level: 'Middle',
      description: '',
      questions: [
        {
          id: `q-${Date.now()}-1`,
          title: 'Основной вопрос',
          prompt: 'Сформулируйте вопрос для кандидата...',
          weight: 1.0,
          criteria: [
            {
              id: `crit-${Date.now()}-1`,
              title: 'Качество ответа',
              description: 'Критерии оценки и ожидаемые аспекты ответа',
              min_score: 1.0,
              max_score: 5.0,
              weight: 1.0,
              levels_description: {
                1: 'Ответ не раскрыт',
                3: 'Базовое понимание',
                5: 'Глубокие экспертные знания',
              },
            },
          ],
        },
      ],
    };
    setEditingTemplate(newTpl);
    setSelectedTemplateId(null);
    setSaveSuccessMsg(null);
  };

  const handleSave = async () => {
    if (!editingTemplate.title.trim()) {
      alert('Укажите название должности');
      return;
    }
    if (!editingTemplate.role.trim()) {
      alert('Укажите роль');
      return;
    }
    if (editingTemplate.questions.length === 0) {
      alert('Добавьте хотя бы один вопрос');
      return;
    }

    // Validate questions and criteria
    for (const [idx, q] of editingTemplate.questions.entries()) {
      if (!q.title?.trim() && !q.prompt?.trim()) {
        alert(`Вопрос #${idx + 1} должен содержать название или формулировку`);
        return;
      }
      if (!q.criteria || q.criteria.length === 0) {
        alert(`Вопрос «${q.title || idx + 1}» должен содержать хотя бы один критерий оценки`);
        return;
      }
    }

    setIsSaving(true);
    setSaveSuccessMsg(null);
    try {
      if (editingTemplate.id) {
        const updated = await updateJobTemplate(editingTemplate.id, {
          title: editingTemplate.title,
          role: editingTemplate.role,
          level: editingTemplate.level,
          description: editingTemplate.description,
          questions: editingTemplate.questions,
        });
        setSaveSuccessMsg('Шаблон должности успешно сохранён');
        await fetchTemplates();
        setSelectedTemplateId(updated.id);
      } else {
        const created = await createJobTemplate({
          title: editingTemplate.title,
          role: editingTemplate.role,
          level: editingTemplate.level,
          description: editingTemplate.description,
          questions: editingTemplate.questions,
        });
        setSaveSuccessMsg('Должность успешно создана');
        await fetchTemplates();
        setSelectedTemplateId(created.id);
      }
      setTimeout(() => setSaveSuccessMsg(null), 3000);
    } catch (err: any) {
      alert(`Ошибка сохранения: ${err.message || err}`);
    } finally {
      setIsSaving(false);
    }
  };

  const handleDuplicate = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const dup = await duplicateJobTemplate(id);
      await fetchTemplates();
      setSelectedTemplateId(dup.id);
    } catch (err: any) {
      alert(`Ошибка дублирования: ${err.message || err}`);
    }
  };

  const handleToggleArchive = async (tpl: JobTemplate, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      if (tpl.is_archived) {
        await unarchiveJobTemplate(tpl.id);
      } else {
        await archiveJobTemplate(tpl.id);
      }
      await fetchTemplates();
    } catch (err: any) {
      alert(`Ошибка архивации: ${err.message || err}`);
    }
  };

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('Вы уверены, что хотите удалить эту должность?')) return;
    try {
      await deleteJobTemplate(id);
      if (selectedTemplateId === id) {
        setSelectedTemplateId(null);
      }
      await fetchTemplates();
    } catch (err: any) {
      alert(`Ошибка удаления: ${err.message || err}`);
    }
  };

  // Question editing actions
  const handleAddQuestion = () => {
    const newQ: PlannedQuestion = {
      id: `q-${Date.now()}-${editingTemplate.questions.length + 1}`,
      title: `Новый вопрос ${editingTemplate.questions.length + 1}`,
      prompt: '',
      weight: 1.0,
      order_index: editingTemplate.questions.length,
      criteria: [
        {
          id: `crit-${Date.now()}-1`,
          title: 'Критерий оценки',
          description: '',
          min_score: 1.0,
          max_score: 5.0,
          weight: 1.0,
          levels_description: {
            1: 'Неудовлетворительно',
            3: 'Соответствует ожиданиям',
            5: 'Превосходит ожидания',
          },
        },
      ],
    };
    setEditingTemplate((prev) => ({
      ...prev,
      questions: [...prev.questions, newQ],
    }));
  };

  const handleDuplicateQuestion = (idx: number) => {
    const target = editingTemplate.questions[idx];
    const copyQ: PlannedQuestion = {
      ...JSON.parse(JSON.stringify(target)),
      id: `q-${Date.now()}`,
      title: `${target.title || 'Вопрос'} (Копия)`,
      order_index: editingTemplate.questions.length,
    };
    const updated = [...editingTemplate.questions];
    updated.splice(idx + 1, 0, copyQ);
    setEditingTemplate((prev) => ({ ...prev, questions: updated }));
  };

  const handleDeleteQuestion = (idx: number) => {
    if (editingTemplate.questions.length <= 1) {
      alert('В должности должен оставаться хотя бы один вопрос.');
      return;
    }
    const updated = editingTemplate.questions.filter((_, i) => i !== idx);
    setEditingTemplate((prev) => ({ ...prev, questions: updated }));
  };

  const handleMoveQuestion = (idx: number, direction: 'up' | 'down') => {
    const targetIdx = direction === 'up' ? idx - 1 : idx + 1;
    if (targetIdx < 0 || targetIdx >= editingTemplate.questions.length) return;
    const updated = [...editingTemplate.questions];
    const temp = updated[idx];
    updated[idx] = updated[targetIdx];
    updated[targetIdx] = temp;
    setEditingTemplate((prev) => ({ ...prev, questions: updated }));
  };

  const handleAddCriterion = (qIdx: number) => {
    const q = editingTemplate.questions[qIdx];
    const newCrit: RubricCriterion = {
      id: `crit-${Date.now()}-${q.criteria.length + 1}`,
      title: `Критерий ${q.criteria.length + 1}`,
      description: '',
      min_score: 1.0,
      max_score: 5.0,
      weight: 1.0,
    };
    const updatedQuestions = [...editingTemplate.questions];
    updatedQuestions[qIdx] = {
      ...q,
      criteria: [...q.criteria, newCrit],
    };
    setEditingTemplate((prev) => ({ ...prev, questions: updatedQuestions }));
  };

  const handleDeleteCriterion = (qIdx: number, critIdx: number) => {
    const q = editingTemplate.questions[qIdx];
    if (q.criteria.length <= 1) {
      alert('У каждого вопроса должен быть хотя бы один критерий оценки.');
      return;
    }
    const updatedCrit = q.criteria.filter((_, i) => i !== critIdx);
    const updatedQuestions = [...editingTemplate.questions];
    updatedQuestions[qIdx] = { ...q, criteria: updatedCrit };
    setEditingTemplate((prev) => ({ ...prev, questions: updatedQuestions }));
  };

  const handleConfirmCopyQuestion = async () => {
    if (!copyQuestionModal || !copyQuestionModal.targetTemplateId) return;
    try {
      if (!editingTemplate.id) {
        alert('Сначала сохраните текущую должность перед копированием вопроса.');
        return;
      }
      await copyQuestionToTemplate(
        editingTemplate.id,
        copyQuestionModal.questionId,
        copyQuestionModal.targetTemplateId
      );
      alert('Вопрос успешно скопирован в выбранную должность!');
      setCopyQuestionModal(null);
      await fetchTemplates();
    } catch (err: any) {
      alert(`Ошибка копирования вопроса: ${err.message || err}`);
    }
  };

  const filteredTemplates = templates.filter((t) => {
    if (!searchQuery) return true;
    const q = searchQuery.toLowerCase();
    return (
      t.title.toLowerCase().includes(q) ||
      t.role.toLowerCase().includes(q) ||
      (t.level && t.level.toLowerCase().includes(q))
    );
  });

  return (
    <div className="h-[calc(100vh-4rem)] flex overflow-hidden">
      {/* Left Column: Templates List */}
      <div className="w-80 md:w-96 border-r border-slate-800 bg-slate-900/60 flex flex-col h-full">
        {/* Header */}
        <div className="p-4 border-b border-slate-800 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-bold text-slate-100 flex items-center space-x-2">
              <Briefcase className="w-4 h-4 text-indigo-400" />
              <span>Должности</span>
            </h2>
            <button
              onClick={handleCreateNew}
              className="flex items-center space-x-1 px-3 py-1.5 text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg transition shadow"
            >
              <Plus className="w-3.5 h-3.5" />
              <span>Создать</span>
            </button>
          </div>

          {/* Search */}
          <div className="relative">
            <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              placeholder="Поиск должности..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full bg-slate-950 border border-slate-800 rounded-lg pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-indigo-500"
            />
          </div>

          {/* Archived Toggle */}
          <label className="flex items-center space-x-2 text-xs text-slate-400 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
              className="rounded bg-slate-950 border-slate-800 text-indigo-600 focus:ring-0"
            />
            <span>Показывать архивные</span>
          </label>

          {errorMsg && (
            <div className="p-2 bg-rose-950/60 border border-rose-800 text-rose-300 text-xs rounded-lg flex items-center space-x-2">
              <AlertCircle className="w-4 h-4 shrink-0 text-rose-400" />
              <span>{errorMsg}</span>
            </div>
          )}
        </div>

        {/* Template Cards List */}
        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {isLoading ? (
            <div className="p-8 text-center text-xs text-slate-500">Загрузка должностей...</div>
          ) : filteredTemplates.length === 0 ? (
            <div className="p-8 text-center text-xs text-slate-500">
              {searchQuery ? 'Ничего не найдено' : 'Нет созданных должностей'}
            </div>
          ) : (
            filteredTemplates.map((tpl) => {
              const isSelected = selectedTemplateId === tpl.id;
              return (
                <div
                  key={tpl.id}
                  onClick={() => setSelectedTemplateId(tpl.id)}
                  className={`p-3 rounded-xl cursor-pointer border transition text-left space-y-2 group ${
                    isSelected
                      ? 'bg-indigo-950/40 border-indigo-600/80 shadow-md'
                      : 'bg-slate-950/50 hover:bg-slate-800/40 border-slate-800/80'
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <h3
                        className={`text-sm font-semibold truncate ${
                          isSelected ? 'text-indigo-200' : 'text-slate-100'
                        }`}
                      >
                        {tpl.title}
                      </h3>
                      <div className="flex items-center space-x-2 mt-1">
                        <span className="px-1.5 py-0.5 text-[10px] font-medium bg-slate-800 text-slate-300 rounded border border-slate-700">
                          {tpl.level || 'Middle'}
                        </span>
                        <span className="text-[11px] text-slate-400 truncate max-w-[120px]">
                          {tpl.role}
                        </span>
                      </div>
                    </div>

                    {tpl.is_archived && (
                      <span className="px-1.5 py-0.5 text-[10px] font-semibold text-amber-300 bg-amber-950/60 border border-amber-800 rounded">
                        Архив
                      </span>
                    )}
                  </div>

                  <div className="flex items-center justify-between text-[11px] text-slate-500 pt-1 border-t border-slate-800/60">
                    <span>
                      {tpl.questions?.length || 0}{' '}
                      {tpl.questions?.length === 1 ? 'вопрос' : 'вопросов'} • v{tpl.version}
                    </span>

                    <div className="flex items-center space-x-1 opacity-80 group-hover:opacity-100">
                      <button
                        onClick={(e) => handleDuplicate(tpl.id, e)}
                        title="Скопировать должность"
                        className="p-1 hover:text-indigo-300 rounded"
                      >
                        <Copy className="w-3.5 h-3.5" />
                      </button>
                      <button
                        onClick={(e) => handleToggleArchive(tpl, e)}
                        title={tpl.is_archived ? 'Восстановить' : 'Архивировать'}
                        className="p-1 hover:text-amber-300 rounded"
                      >
                        {tpl.is_archived ? (
                          <ArchiveRestore className="w-3.5 h-3.5" />
                        ) : (
                          <Archive className="w-3.5 h-3.5" />
                        )}
                      </button>
                      <button
                        onClick={(e) => handleDelete(tpl.id, e)}
                        title="Удалить должность"
                        className="p-1 hover:text-rose-400 rounded"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      {/* Right Column: Template & Questions Editor */}
      <div className="flex-1 overflow-y-auto p-6 md:p-8 space-y-6">
        {/* Editor Top Bar */}
        <div className="flex items-center justify-between border-b border-slate-800 pb-4">
          <div>
            <h2 className="text-xl font-bold text-slate-100">
              {editingTemplate.id ? 'Редактирование должности' : 'Создание новой должности'}
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Настройте требования, перечень вопросов, критерии и веса для интервью
            </p>
          </div>

          <div className="flex items-center space-x-3">
            {saveSuccessMsg && (
              <span className="flex items-center space-x-1.5 text-xs text-emerald-400 font-medium">
                <CheckCircle2 className="w-4 h-4" />
                <span>{saveSuccessMsg}</span>
              </span>
            )}
            <button
              onClick={handleSave}
              disabled={isSaving}
              className="flex items-center space-x-2 px-5 py-2 text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl transition shadow-lg shadow-indigo-600/30 disabled:opacity-50"
            >
              <Save className="w-4 h-4" />
              <span>{isSaving ? 'Сохранение...' : 'Сохранить должность'}</span>
            </button>
          </div>
        </div>

        {/* Basic Fields */}
        <div className="glass-panel p-6 rounded-2xl space-y-4 shadow-md">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="md:col-span-2 space-y-1.5">
              <label className="text-xs font-semibold text-slate-300">Название должности</label>
              <input
                type="text"
                value={editingTemplate.title}
                onChange={(e) =>
                  setEditingTemplate((prev) => ({ ...prev, title: e.target.value }))
                }
                placeholder="например, Senior Backend / Systems Engineer"
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2 text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-slate-300">Уровень (Grade)</label>
              <select
                value={editingTemplate.level}
                onChange={(e) =>
                  setEditingTemplate((prev) => ({ ...prev, level: e.target.value }))
                }
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500 cursor-pointer"
              >
                <option value="Junior">Junior</option>
                <option value="Middle">Middle</option>
                <option value="Senior">Senior</option>
                <option value="Lead">Lead</option>
                <option value="Principal / Architect">Principal / Architect</option>
              </select>
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-slate-300">Роль / Специализация</label>
            <input
              type="text"
              value={editingTemplate.role}
              onChange={(e) =>
                setEditingTemplate((prev) => ({ ...prev, role: e.target.value }))
              }
              placeholder="например, Backend Engineer, Frontend, DevOps"
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2 text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-slate-300">Описание и требования</label>
            <textarea
              rows={2}
              value={editingTemplate.description}
              onChange={(e) =>
                setEditingTemplate((prev) => ({ ...prev, description: e.target.value }))
              }
              placeholder="Ключевые требования к кандидату и фокус интервью..."
              className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2 text-sm text-slate-100 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
        </div>

        {/* Questions Section */}
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-base font-bold text-slate-100 flex items-center space-x-2">
                <span>Вопросы для интервью</span>
                <span className="px-2 py-0.5 text-xs bg-slate-800 text-slate-300 rounded-full font-mono">
                  {editingTemplate.questions.length}
                </span>
              </h3>
              <p className="text-xs text-slate-400">
                Задайте формулировку, вес вопроса и рубрики с критериями оценки
              </p>
            </div>

            <button
              onClick={handleAddQuestion}
              className="flex items-center space-x-1.5 px-3.5 py-1.5 text-xs font-semibold text-indigo-300 hover:text-white bg-indigo-950/60 hover:bg-indigo-600 border border-indigo-800/80 rounded-lg transition"
            >
              <Plus className="w-3.5 h-3.5" />
              <span>Добавить вопрос</span>
            </button>
          </div>

          <div className="space-y-4">
            {editingTemplate.questions.map((q, qIdx) => (
              <div
                key={q.id}
                className="glass-panel p-5 rounded-2xl border border-slate-800/90 space-y-4 shadow-sm"
              >
                {/* Question Header */}
                <div className="flex items-center justify-between gap-3 border-b border-slate-800/60 pb-3">
                  <div className="flex items-center space-x-2">
                    <span className="w-6 h-6 rounded-full bg-indigo-950 text-indigo-300 border border-indigo-800 flex items-center justify-center text-xs font-bold font-mono">
                      {qIdx + 1}
                    </span>
                    <input
                      type="text"
                      value={q.title || ''}
                      onChange={(e) => {
                        const val = e.target.value;
                        const updated = [...editingTemplate.questions];
                        updated[qIdx] = { ...q, title: val };
                        setEditingTemplate((prev) => ({ ...prev, questions: updated }));
                      }}
                      placeholder="Короткое название вопроса..."
                      className="font-semibold text-sm bg-transparent border-b border-transparent hover:border-slate-700 focus:border-indigo-500 focus:outline-none text-slate-100 px-1 py-0.5 min-w-[280px]"
                    />
                  </div>

                  <div className="flex items-center space-x-1.5 text-xs">
                    {/* Weight */}
                    <div className="flex items-center space-x-1 bg-slate-950 border border-slate-800 rounded px-2 py-1 mr-2">
                      <span className="text-slate-500 text-[11px]">Вес:</span>
                      <input
                        type="number"
                        step="0.1"
                        min="0.1"
                        max="10.0"
                        value={q.weight}
                        onChange={(e) => {
                          const w = parseFloat(e.target.value) || 1.0;
                          const updated = [...editingTemplate.questions];
                          updated[qIdx] = { ...q, weight: w };
                          setEditingTemplate((prev) => ({ ...prev, questions: updated }));
                        }}
                        className="w-12 bg-transparent text-right font-mono text-slate-200 focus:outline-none"
                      />
                    </div>

                    {/* Move buttons */}
                    <button
                      disabled={qIdx === 0}
                      onClick={() => handleMoveQuestion(qIdx, 'up')}
                      title="Переместить выше"
                      className="p-1 text-slate-400 hover:text-slate-100 disabled:opacity-30 rounded hover:bg-slate-800"
                    >
                      <ChevronUp className="w-4 h-4" />
                    </button>
                    <button
                      disabled={qIdx === editingTemplate.questions.length - 1}
                      onClick={() => handleMoveQuestion(qIdx, 'down')}
                      title="Переместить ниже"
                      className="p-1 text-slate-400 hover:text-slate-100 disabled:opacity-30 rounded hover:bg-slate-800"
                    >
                      <ChevronDown className="w-4 h-4" />
                    </button>

                    {/* Duplicate question */}
                    <button
                      onClick={() => handleDuplicateQuestion(qIdx)}
                      title="Продублировать вопрос"
                      className="p-1 text-slate-400 hover:text-indigo-300 rounded hover:bg-slate-800"
                    >
                      <Copy className="w-4 h-4" />
                    </button>

                    {/* Copy to another template */}
                    {editingTemplate.id && (
                      <button
                        onClick={() =>
                          setCopyQuestionModal({
                            questionId: q.id,
                            questionTitle: q.title || `Вопрос #${qIdx + 1}`,
                            targetTemplateId:
                              templates.find((t) => t.id !== editingTemplate.id)?.id || '',
                          })
                        }
                        title="Скопировать в другую должность"
                        className="p-1 text-slate-400 hover:text-teal-300 rounded hover:bg-slate-800"
                      >
                        <ArrowRight className="w-4 h-4" />
                      </button>
                    )}

                    {/* Delete question */}
                    <button
                      onClick={() => handleDeleteQuestion(qIdx)}
                      title="Удалить вопрос"
                      className="p-1 text-slate-400 hover:text-rose-400 rounded hover:bg-slate-800"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>

                {/* Prompt */}
                <div className="space-y-1">
                  <label className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
                    Полная формулировка вопроса / гайд интервьюеру
                  </label>
                  <textarea
                    rows={2}
                    value={q.prompt || q.text || ''}
                    onChange={(e) => {
                      const val = e.target.value;
                      const updated = [...editingTemplate.questions];
                      updated[qIdx] = { ...q, prompt: val, text: val };
                      setEditingTemplate((prev) => ({ ...prev, questions: updated }));
                    }}
                    placeholder="Подробный вопрос и наводящие подсказки..."
                    className="w-full bg-slate-950 border border-slate-800/80 rounded-lg px-3 py-2 text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
                  />
                </div>

                {/* Criteria Sublist */}
                <div className="space-y-3 pt-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-slate-300 flex items-center space-x-1.5">
                      <Layers className="w-3.5 h-3.5 text-indigo-400" />
                      <span>Критерии оценки и рубрика</span>
                    </span>
                    <button
                      onClick={() => handleAddCriterion(qIdx)}
                      className="text-[11px] text-indigo-400 hover:text-indigo-300 font-semibold flex items-center space-x-1"
                    >
                      <Plus className="w-3 h-3" />
                      <span>Добавить критерий</span>
                    </button>
                  </div>

                  <div className="space-y-2.5">
                    {q.criteria.map((crit, critIdx) => (
                      <div
                        key={crit.id}
                        className="bg-slate-950/70 border border-slate-800/80 p-3 rounded-xl space-y-2.5"
                      >
                        <div className="flex items-center justify-between gap-3">
                          <input
                            type="text"
                            value={crit.title}
                            onChange={(e) => {
                              const val = e.target.value;
                              const updatedQuestions = [...editingTemplate.questions];
                              const updatedCrits = [...q.criteria];
                              updatedCrits[critIdx] = { ...crit, title: val };
                              updatedQuestions[qIdx] = { ...q, criteria: updatedCrits };
                              setEditingTemplate((prev) => ({
                                ...prev,
                                questions: updatedQuestions,
                              }));
                            }}
                            placeholder="Название критерия (например, 'Знание архитектуры')"
                            className="text-xs font-semibold bg-transparent border-b border-transparent hover:border-slate-700 focus:border-indigo-500 focus:outline-none text-slate-200 flex-1 px-1"
                          />

                          <div className="flex items-center space-x-3 text-xs text-slate-400">
                            <span className="font-mono text-[11px]">
                              Шкала: {crit.min_score} – {crit.max_score}
                            </span>
                            <div className="flex items-center space-x-1">
                              <span className="text-[11px] text-slate-500">Вес:</span>
                              <input
                                type="number"
                                step="0.1"
                                min="0.1"
                                value={crit.weight}
                                onChange={(e) => {
                                  const w = parseFloat(e.target.value) || 1.0;
                                  const updatedQuestions = [...editingTemplate.questions];
                                  const updatedCrits = [...q.criteria];
                                  updatedCrits[critIdx] = { ...crit, weight: w };
                                  updatedQuestions[qIdx] = { ...q, criteria: updatedCrits };
                                  setEditingTemplate((prev) => ({
                                    ...prev,
                                    questions: updatedQuestions,
                                  }));
                                }}
                                className="w-10 bg-slate-900 border border-slate-800 rounded px-1.5 py-0.5 text-right font-mono text-slate-200 focus:outline-none"
                              />
                            </div>
                            <button
                              onClick={() => handleDeleteCriterion(qIdx, critIdx)}
                              title="Удалить критерий"
                              className="p-1 hover:text-rose-400 rounded"
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          </div>
                        </div>

                        <input
                          type="text"
                          value={crit.description || ''}
                          onChange={(e) => {
                            const val = e.target.value;
                            const updatedQuestions = [...editingTemplate.questions];
                            const updatedCrits = [...q.criteria];
                            updatedCrits[critIdx] = { ...crit, description: val };
                            updatedQuestions[qIdx] = { ...q, criteria: updatedCrits };
                            setEditingTemplate((prev) => ({
                              ...prev,
                              questions: updatedQuestions,
                            }));
                          }}
                          placeholder="Что оценивается: ожидаемые термины, подходы, ошибки..."
                          className="w-full bg-slate-900/60 border border-slate-800/60 rounded px-2.5 py-1 text-xs text-slate-300 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
                        />
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Copy Question Modal */}
      {copyQuestionModal && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-800 p-6 rounded-2xl max-w-md w-full shadow-2xl space-y-4">
            <h3 className="text-base font-bold text-slate-100 flex items-center space-x-2">
              <Copy className="w-5 h-5 text-indigo-400" />
              <span>Копирование вопроса</span>
            </h3>
            <p className="text-sm text-slate-400">
              Скопировать вопрос <span className="text-slate-200 font-semibold">«{copyQuestionModal.questionTitle}»</span> в другую должность?
            </p>

            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-slate-300">Целевая должность</label>
              <select
                value={copyQuestionModal.targetTemplateId}
                onChange={(e) =>
                  setCopyQuestionModal((prev) =>
                    prev ? { ...prev, targetTemplateId: e.target.value } : null
                  )
                }
                className="w-full bg-slate-950 border border-slate-800 rounded-lg px-3.5 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500 cursor-pointer"
              >
                {templates
                  .filter((t) => t.id !== editingTemplate.id)
                  .map((t) => (
                    <option key={t.id} value={t.id} className="bg-slate-900">
                      {t.title} ({t.level})
                    </option>
                  ))}
              </select>
            </div>

            <div className="flex items-center justify-end space-x-3 pt-2">
              <button
                onClick={() => setCopyQuestionModal(null)}
                className="px-4 py-2 text-xs font-semibold text-slate-400 hover:text-slate-200 bg-slate-800 rounded-lg transition"
              >
                Отмена
              </button>
              <button
                onClick={handleConfirmCopyQuestion}
                disabled={!copyQuestionModal.targetTemplateId}
                className="px-4 py-2 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-500 rounded-lg transition disabled:opacity-50"
              >
                Скопировать вопрос
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
