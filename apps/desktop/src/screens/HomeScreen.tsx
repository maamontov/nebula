import React, { useState, useEffect, useCallback } from 'react';
import { InterviewListItem, InterviewStatus } from '../types';
import { listInterviews, deleteInterview } from '../services/api';
import { CustomSelect } from '../components/CustomSelect';
import {
  Search,
  Filter,
  Calendar,
  Briefcase,
  ChevronLeft,
  ChevronRight,
  ShieldCheck,
  Radio,
  Clock,
  Trash2,
  AlertCircle,
  RotateCcw,
  CheckCircle2,
  FileEdit,
  Sparkles,
  ThumbsUp,
  AlertTriangle,
  XCircle,
} from 'lucide-react';

interface HomeScreenProps {
  onOpenInterview: (interviewId: string, status: InterviewStatus) => void;
  onNewInterview: () => void;
  hasActiveRecording: boolean;
}

export const HomeScreen: React.FC<HomeScreenProps> = ({
  onOpenInterview,
  onNewInterview,
  hasActiveRecording,
}) => {
  const [items, setItems] = useState<InterviewListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(15);
  const [isLoading, setIsLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Filters
  const [searchQuery, setSearchQuery] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [dateFilter, setDateFilter] = useState('');
  const [showFilters, setShowFilters] = useState(false);

  // Available roles collected from interviews for dynamic filter
  const [availableRoles, setAvailableRoles] = useState<string[]>([]);

  // Delete modal state
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const fetchInterviews = useCallback(async () => {
    setIsLoading(true);
    setErrorMsg(null);
    try {
      let fromDate: string | undefined = undefined;
      if (dateFilter === 'today') {
        const d = new Date();
        d.setHours(0, 0, 0, 0);
        fromDate = d.toISOString();
      } else if (dateFilter === '7d') {
        const d = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);
        fromDate = d.toISOString();
      } else if (dateFilter === '30d') {
        const d = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000);
        fromDate = d.toISOString();
      }

      const offset = (page - 1) * pageSize;
      const res = await listInterviews({
        limit: pageSize,
        offset,
        search: searchQuery.trim() || undefined,
        role: roleFilter || undefined,
        status: statusFilter || undefined,
        from_date: fromDate,
      });

      setItems(res.items);
      setTotal(res.total);

      // Collect unique roles for filter dropdown
      const roles = new Set<string>();
      res.items.forEach((item) => {
        if (item.role) roles.add(item.role);
      });
      setAvailableRoles((prev) => Array.from(new Set([...prev, ...Array.from(roles)])));
    } catch (err: any) {
      setErrorMsg(err.message || 'Ошибка загрузки собеседований');
    } finally {
      setIsLoading(false);
    }
  }, [page, pageSize, searchQuery, roleFilter, statusFilter, dateFilter]);

  useEffect(() => {
    fetchInterviews();
  }, [fetchInterviews]);

  const handleDelete = async () => {
    if (!deletingId) return;
    setIsDeleting(true);
    try {
      await deleteInterview(deletingId);
      setDeletingId(null);
      await fetchInterviews();
    } catch (err: any) {
      alert(`Ошибка удаления интервью: ${err.message || err}`);
    } finally {
      setIsDeleting(false);
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const activeFiltersCount = [roleFilter, statusFilter, dateFilter].filter(Boolean).length;

  const formatDateTime = (isoString: string) => {
    if (!isoString) return '—';
    try {
      const d = new Date(isoString);
      return d.toLocaleDateString('ru-RU', {
        day: 'numeric',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return isoString;
    }
  };

  const getCandidateDisplay = (item: InterviewListItem) => {
    if (item.candidate_name && item.candidate_name.trim() && item.candidate_name !== 'Не указан' && item.candidate_name !== 'Без имени') {
      return item.candidate_name;
    }
    if (item.title && (item.title.includes('—') || item.title.includes('-'))) {
      const parts = item.title.split(/[—-]/);
      if (parts.length >= 2) {
        const candidatePart = parts[parts.length - 1].trim();
        if (candidatePart && candidatePart !== 'Черновик' && candidatePart !== 'Без имени') {
          return candidatePart;
        }
      }
    }
    return item.candidate_name || 'Не указан';
  };

  const getRoleDisplay = (item: InterviewListItem) => {
    if (item.role && item.role.trim() && item.role !== 'Позиция не указана') {
      return item.role;
    }
    if (item.title && (item.title.includes('—') || item.title.includes('-'))) {
      const parts = item.title.split(/[—-]/);
      if (parts.length >= 2) {
        const rolePart = parts[0].trim();
        if (rolePart && rolePart !== 'Интервью') {
          return rolePart;
        }
      }
    }
    return item.role || item.title || 'Позиция не указана';
  };

  const getStatusBadge = (item: InterviewListItem) => {
    if (item.is_reopened) {
      return (
        <span className="inline-flex items-center space-x-1 px-2.5 py-1 text-xs font-semibold text-amber-300 bg-amber-950/70 border border-amber-700/80 rounded-full">
          <RotateCcw className="w-3 h-3 text-amber-400" />
          <span>Переоткрыто (ред. {(item.latest_report_revision ?? 1) + 1})</span>
        </span>
      );
    }

    switch (item.status) {
      case 'recording':
        return (
          <span className="inline-flex items-center space-x-1.5 px-2.5 py-1 text-xs font-semibold text-rose-300 bg-rose-950/70 border border-rose-800 rounded-full">
            <span className="w-2 h-2 rounded-full bg-rose-500 animate-pulse" />
            <span>Запись (LIVE)</span>
          </span>
        );
      case 'paused':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 text-xs font-semibold text-amber-300 bg-amber-950/70 border border-amber-800 rounded-full">
            <Clock className="w-3 h-3 text-amber-400" />
            <span>Пауза</span>
          </span>
        );
      case 'ready':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 text-xs font-semibold text-cyan-300 bg-cyan-950/70 border border-cyan-800 rounded-full">
            <CheckCircle2 className="w-3 h-3 text-cyan-400" />
            <span>Готово к записи</span>
          </span>
        );
      case 'processing':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 text-xs font-semibold text-purple-300 bg-purple-950/70 border border-purple-800 rounded-full">
            <Radio className="w-3 h-3 text-purple-400 animate-spin" />
            <span>Обработка</span>
          </span>
        );
      case 'review':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 text-xs font-semibold text-indigo-300 bg-indigo-950/70 border border-indigo-800 rounded-full">
            <FileEdit className="w-3 h-3 text-indigo-400" />
            <span>Проверка</span>
          </span>
        );
      case 'finalized':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 text-xs font-semibold text-emerald-300 bg-emerald-950/70 border border-emerald-800 rounded-full">
            <ShieldCheck className="w-3 h-3 text-emerald-400" />
            <span>Завершено</span>
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-1 text-xs font-semibold text-slate-400 bg-slate-800/80 border border-slate-700 rounded-full">
            <span>Черновик</span>
          </span>
        );
    }
  };

  const getDecisionBadge = (rec?: string | null) => {
    if (!rec) return null;
    switch (rec.toUpperCase()) {
      case 'STRONG_HIRE':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 text-xs font-bold text-emerald-300 bg-emerald-950/80 border border-emerald-600/80 rounded-lg shadow-sm">
            <Sparkles className="w-3 h-3 text-emerald-400" />
            <span>Strong Hire</span>
          </span>
        );
      case 'HIRE':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 text-xs font-bold text-teal-300 bg-teal-950/80 border border-teal-600/80 rounded-lg shadow-sm">
            <CheckCircle2 className="w-3 h-3 text-teal-400" />
            <span>Hire</span>
          </span>
        );
      case 'LEAN_HIRE':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 text-xs font-bold text-cyan-300 bg-cyan-950/80 border border-cyan-600/80 rounded-lg shadow-sm">
            <ThumbsUp className="w-3 h-3 text-cyan-400" />
            <span>Lean Hire</span>
          </span>
        );
      case 'LEAN_NO_HIRE':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 text-xs font-bold text-amber-300 bg-amber-950/80 border border-amber-600/80 rounded-lg shadow-sm">
            <AlertTriangle className="w-3 h-3 text-amber-400" />
            <span>Lean No Hire</span>
          </span>
        );
      case 'NO_HIRE':
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 text-xs font-bold text-rose-300 bg-rose-950/80 border border-rose-600/80 rounded-lg shadow-sm">
            <XCircle className="w-3 h-3 text-rose-400" />
            <span>No Hire</span>
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 text-xs font-medium text-slate-300 bg-slate-800 border border-slate-700 rounded-lg">
            <span>{rec}</span>
          </span>
        );
    }
  };

  return (
    <div className={`home-screen${!errorMsg && items.length === 0 ? ' home-screen-empty' : ''} max-w-7xl mx-auto p-6 md:p-8 space-y-6 overflow-y-auto h-[calc(100vh-4rem)]`}>
      {/* Header Banner */}
      <div className="home-heading flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100 flex items-center space-x-3">
            <span>Собеседования</span>
            {total > 0 && <span className="home-total-count">{total}</span>}
          </h1>
        </div>

      </div>

      {/* Filters Toolbar */}
      <div className={`filter-toolbar p-3 rounded-xl bg-slate-900/60 border border-slate-800${showFilters ? ' filter-toolbar-expanded' : ''}`}>
        <div className="filter-toolbar-primary">
        {/* Search */}
        <div className="relative flex-1 min-w-[240px]">
          <Search className="w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            placeholder="Поиск по собеседованиям"
            value={searchQuery}
            onChange={(e) => {
              setSearchQuery(e.target.value);
              setPage(1);
            }}
            className="form-control w-full bg-slate-950 border border-slate-800 rounded-lg pl-9 pr-4 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-indigo-500"
          />
        </div>

        <button
          type="button"
          className={`filter-toggle${showFilters ? ' filter-toggle-active' : ''}`}
          onClick={() => setShowFilters((current) => !current)}
          aria-expanded={showFilters}
        >
          <Filter className="w-4 h-4" />
          <span>Фильтры</span>
          {activeFiltersCount > 0 && <span className="filter-count">{activeFiltersCount}</span>}
        </button>
        </div>

        <div className="filter-toolbar-secondary" aria-hidden={!showFilters} inert={!showFilters}>
          {/* Role Filter */}
          <div className="filter-control flex items-center space-x-1.5 bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm">
          <Briefcase className="w-4 h-4 text-slate-400" />
          <CustomSelect
            value={roleFilter}
            onChange={(e) => {
              setRoleFilter(e.target.value);
              setPage(1);
            }}
            className="bg-transparent text-slate-200 focus:outline-none text-sm cursor-pointer"
          >
            <option value="" className="bg-slate-900">Все должности</option>
            {availableRoles.map((r) => (
              <option key={r} value={r} className="bg-slate-900">{r}</option>
            ))}
          </CustomSelect>
          </div>

          {/* Status Filter */}
          <div className="filter-control flex items-center space-x-1.5 bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm">
          <ShieldCheck className="w-4 h-4 text-slate-400" />
          <CustomSelect
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value);
              setPage(1);
            }}
            className="bg-transparent text-slate-200 focus:outline-none text-sm cursor-pointer"
          >
            <option value="" className="bg-slate-900">Все статусы</option>
            <option value="draft" className="bg-slate-900">Черновик</option>
            <option value="ready" className="bg-slate-900">Готово к записи</option>
            <option value="recording" className="bg-slate-900">Запись</option>
            <option value="paused" className="bg-slate-900">Пауза</option>
            <option value="processing" className="bg-slate-900">Обработка</option>
            <option value="review" className="bg-slate-900">Проверка</option>
            <option value="finalized" className="bg-slate-900">Завершено</option>
            <option value="reopened" className="bg-slate-900">Переоткрыто</option>
          </CustomSelect>
          </div>

          {/* Date Filter */}
          <div className="filter-control flex items-center space-x-1.5 bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm">
          <Calendar className="w-4 h-4 text-slate-400" />
          <CustomSelect
            value={dateFilter}
            onChange={(e) => {
              setDateFilter(e.target.value);
              setPage(1);
            }}
            className="bg-transparent text-slate-200 focus:outline-none text-sm cursor-pointer"
          >
            <option value="" className="bg-slate-900">За всё время</option>
            <option value="today" className="bg-slate-900">Сегодня</option>
            <option value="7d" className="bg-slate-900">Последние 7 дней</option>
            <option value="30d" className="bg-slate-900">Последние 30 дней</option>
          </CustomSelect>
          </div>
        </div>
      </div>

      {/* Main Table / List */}
      <div className={`interview-table-shell glass-panel rounded-2xl overflow-hidden shadow-xl border border-slate-800/80${
        !errorMsg && items.length === 0 ? ' interview-table-empty' : ''
      }`}>
        {isLoading ? (
          <div className="p-16 text-center text-slate-400 space-y-3">
            <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin mx-auto" />
            <p className="text-sm">Загрузка собеседований...</p>
          </div>
        ) : errorMsg ? (
          <div className="p-12 text-center text-rose-400 space-y-3">
            <AlertCircle className="w-8 h-8 mx-auto text-rose-500" />
            <p className="text-sm font-medium">{errorMsg}</p>
            <button
              onClick={fetchInterviews}
              className="px-4 py-1.5 text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-lg transition"
            >
              Повторить попытку
            </button>
          </div>
        ) : items.length === 0 ? (
          <div className="interview-empty-state p-16 text-center space-y-4">
            <div className="interview-empty-copy">
              <h3 className="text-base font-semibold text-slate-200">Собеседования не найдены</h3>
              <p className="text-xs text-slate-400 mt-1 max-w-sm mx-auto">
                {searchQuery || roleFilter || statusFilter || dateFilter
                  ? 'Измените запрос или фильтры.'
                  : 'Создайте первое интервью.'}
              </p>
            </div>
            {!searchQuery && !roleFilter && !statusFilter && !dateFilter && (
              <button
                onClick={onNewInterview}
                disabled={hasActiveRecording}
                className="interview-empty-action px-4 py-2 text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg transition shadow-md"
              >
                Создать первое интервью
              </button>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="interview-table-head border-b border-slate-800 bg-slate-900/80 text-xs font-semibold text-slate-400 uppercase tracking-wider">
                  <th className="py-3.5 px-6">Кандидат</th>
                  <th className="py-3.5 px-6">Должность</th>
                  <th className="py-3.5 px-6">Дата</th>
                  <th className="py-3.5 px-6">Статус</th>
                  <th className="py-3.5 px-6">Балл / Решение</th>
                  <th className="py-3.5 px-6 text-right">Действия</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 text-sm">
                {items.map((item) => {
                  const score = item.final_score_100 ?? item.last_finalized_score;
                  const recommendation = item.hiring_recommendation ?? item.last_finalized_recommendation;

                  return (
                    <tr
                      key={item.id}
                      onClick={() => onOpenInterview(item.id, item.status)}
                      className="interview-table-row hover:bg-slate-800/40 cursor-pointer transition group"
                    >
                      {/* Candidate */}
                      <td className="py-4 px-6">
                        <div className="font-semibold text-slate-100 group-hover:text-indigo-300 transition">
                          {getCandidateDisplay(item)}
                        </div>
                        <div className="text-xs text-slate-500 font-mono mt-0.5 truncate max-w-[200px]">
                          {item.id}
                        </div>
                      </td>

                      {/* Role */}
                      <td className="py-4 px-6">
                        <div className="text-slate-200">{getRoleDisplay(item)}</div>
                        {item.template_id && (
                          <div className="text-[11px] text-slate-500 mt-0.5">
                            Шаблон: {item.template_id}
                          </div>
                        )}
                      </td>

                      {/* Date */}
                      <td className="py-4 px-6 text-xs text-slate-400 whitespace-nowrap">
                        {formatDateTime(item.created_at)}
                      </td>

                      {/* Status */}
                      <td className="py-4 px-6 whitespace-nowrap">
                        {getStatusBadge(item)}
                      </td>

                      {/* Score / Decision */}
                      <td className="py-4 px-6 whitespace-nowrap">
                        {score !== null && score !== undefined ? (
                          <div className="flex items-center space-x-2">
                            <span
                              className={`font-mono font-bold text-sm ${
                                score >= 75
                                  ? 'text-emerald-400'
                                  : score >= 50
                                  ? 'text-amber-400'
                                  : 'text-rose-400'
                              }`}
                            >
                              {Math.round(score)} <span className="text-xs text-slate-500 font-normal">/ 100</span>
                            </span>
                            {getDecisionBadge(recommendation)}
                          </div>
                        ) : (
                          <span className="text-xs text-slate-500 font-mono">—</span>
                        )}
                      </td>

                      {/* Actions */}
                      <td className="py-4 px-6 text-right whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center justify-end space-x-2">
                          <button
                            onClick={() => onOpenInterview(item.id, item.status)}
                            className="px-3 py-1 text-xs font-semibold text-indigo-400 hover:text-white bg-indigo-950/40 hover:bg-indigo-600 border border-indigo-800/80 rounded-lg transition"
                          >
                            Открыть
                          </button>
                          <button
                            onClick={() => setDeletingId(item.id)}
                            title="Удалить собеседование"
                            className="p-1 text-slate-500 hover:text-rose-400 hover:bg-rose-950/40 rounded transition"
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination Footer */}
        {total > pageSize && (
          <div className="p-4 bg-slate-900/90 border-t border-slate-800 flex items-center justify-between text-xs text-slate-400">
            <div>
              Показано {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, total)} из {total}
            </div>
            <div className="flex items-center space-x-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                className="p-1.5 rounded-lg border border-slate-800 bg-slate-950 hover:bg-slate-800 disabled:opacity-40 disabled:cursor-not-allowed transition"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <span className="px-2 font-mono text-slate-200">
                {page} / {totalPages}
              </span>
              <button
                disabled={page >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                className="p-1.5 rounded-lg border border-slate-800 bg-slate-950 hover:bg-slate-800 disabled:opacity-40 disabled:cursor-not-allowed transition"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Delete Confirmation Modal */}
      {deletingId && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-800 p-6 rounded-2xl max-w-md w-full shadow-2xl space-y-4">
            <h3 className="text-base font-bold text-slate-100 flex items-center space-x-2">
              <Trash2 className="w-5 h-5 text-rose-500" />
              <span>Удаление собеседования</span>
            </h3>
            <p className="text-sm text-slate-400">
              Вы уверены, что хотите удалить собеседование <span className="font-mono text-slate-200">{deletingId}</span>?
              Все связанные аудиофайлы и данные будут помечены как удалённые.
            </p>
            <div className="flex items-center justify-end space-x-3 pt-2">
              <button
                onClick={() => setDeletingId(null)}
                disabled={isDeleting}
                className="px-4 py-2 text-xs font-semibold text-slate-400 hover:text-slate-200 bg-slate-800 rounded-lg transition"
              >
                Отмена
              </button>
              <button
                onClick={handleDelete}
                disabled={isDeleting}
                className="px-4 py-2 text-xs font-semibold text-white bg-rose-600 hover:bg-rose-500 rounded-lg transition"
              >
                {isDeleting ? 'Удаление...' : 'Да, удалить'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
