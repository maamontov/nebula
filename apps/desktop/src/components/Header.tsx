import React from 'react';
import { InterviewStatus } from '../types';
import { AudioMeters } from './AudioMeters';
import { Sparkles, ShieldCheck, Briefcase, FileSpreadsheet, ArrowRight } from 'lucide-react';

interface HeaderProps {
  status: InterviewStatus;
  candidateName?: string;
  role?: string;
  isCapturing?: boolean;
  currentScreen?: 'home' | 'templates' | 'setup' | 'live' | 'review';
  onNavigate?: (screen: 'home' | 'templates' | 'live') => void;
  activeRecordingSession?: {
    interviewId: string;
    candidateName: string;
    role: string;
  } | null;
}

export const Header: React.FC<HeaderProps> = ({
  status,
  candidateName = 'Новое собеседование',
  role = 'Позиция не выбрана',
  isCapturing = false,
  currentScreen = 'home',
  onNavigate,
  activeRecordingSession = null,
}) => {
  const getStatusBadge = () => {
    switch (status) {
      case 'recording':
        return (
          <span className="flex items-center space-x-1.5 px-2.5 py-0.5 text-xs font-semibold text-rose-300 bg-rose-950/60 border border-rose-800/80 rounded-full shadow-sm whitespace-nowrap shrink-0">
            <span className="w-2 h-2 rounded-full bg-rose-500 recording-pulse" />
            <span>ЗАПИСЬ (LIVE)</span>
          </span>
        );
      case 'paused':
        return (
          <span className="px-2.5 py-0.5 text-xs font-semibold text-amber-300 bg-amber-950/60 border border-amber-800/80 rounded-full whitespace-nowrap shrink-0">
            ПАУЗА
          </span>
        );
      case 'review':
        return (
          <span className="px-2.5 py-0.5 text-xs font-semibold text-blue-300 bg-blue-950/60 border border-blue-800/80 rounded-full whitespace-nowrap shrink-0">
            ПРОВЕРКА И ИТОГИ
          </span>
        );
      case 'finalized':
        return (
          <span className="flex items-center space-x-1 px-2.5 py-0.5 text-xs font-semibold text-emerald-300 bg-emerald-950/60 border border-emerald-800/80 rounded-full whitespace-nowrap shrink-0">
            <ShieldCheck className="w-3 h-3" />
            <span>ЗАВЕРШЕНО</span>
          </span>
        );
      default:
        return (
          <span className="px-2.5 py-0.5 text-xs font-semibold text-slate-400 bg-slate-800/80 border border-slate-700/80 rounded-full whitespace-nowrap shrink-0">
            ПОДГОТОВКА
          </span>
        );
    }
  };

  const isInterviewScreen = currentScreen === 'live' || currentScreen === 'review' || currentScreen === 'setup';

  return (
    <header className="h-16 px-6 bg-slate-900/90 border-b border-slate-800/80 flex items-center justify-between select-none">
      {/* Left: Brand & Navigation */}
      <div className="flex items-center space-x-6">
        <div
          onClick={() => onNavigate?.('home')}
          className="flex items-center space-x-2.5 cursor-pointer hover:opacity-90 transition"
        >
          <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-indigo-600 to-purple-500 flex items-center justify-center shadow-lg shadow-indigo-500/20">
            <Sparkles className="w-4 h-4 text-white" />
          </div>
          <span className="font-bold text-lg tracking-tight bg-gradient-to-r from-white via-slate-100 to-slate-400 bg-clip-text text-transparent">
            Nebula
          </span>
        </div>

        {/* Global Navigation Tabs */}
        <nav className="flex items-center space-x-1 bg-slate-950/60 p-1 rounded-xl border border-slate-800/80">
          <button
            type="button"
            onClick={() => onNavigate?.('home')}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition cursor-pointer ${
              currentScreen === 'home'
                ? 'bg-slate-800 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/80'
            }`}
          >
            <FileSpreadsheet className="w-3.5 h-3.5" />
            <span>Интервью</span>
          </button>

          <button
            type="button"
            onClick={() => onNavigate?.('templates')}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition cursor-pointer ${
              currentScreen === 'templates'
                ? 'bg-slate-800 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900/80'
            }`}
          >
            <Briefcase className="w-3.5 h-3.5" />
            <span>Должности и вопросы</span>
          </button>
        </nav>

        {/* Interview Context Info if on an interview screen */}
        {isInterviewScreen && (
          <div className="flex items-center space-x-3 pl-2 border-l border-slate-800">
            <div>
              <div className="flex items-center space-x-2">
                <h1 className="text-xs font-semibold text-slate-100 max-w-[180px] truncate">{candidateName}</h1>
                {getStatusBadge()}
              </div>
              <p className="text-[11px] text-slate-400 max-w-[180px] truncate">{role}</p>
            </div>
          </div>
        )}
      </div>

      {/* Center / Banner: Persistent Active Recording Reminder */}
      {activeRecordingSession && currentScreen !== 'live' && (
        <div className="flex items-center space-x-3 px-3.5 py-1.5 bg-rose-950/80 border border-rose-600/80 rounded-xl shadow-lg shadow-rose-950/50">
          <div className="flex items-center space-x-2">
            <span className="w-2.5 h-2.5 rounded-full bg-rose-500 recording-pulse shrink-0" />
            <span className="text-xs font-bold text-rose-200">
              Идёт запись:{' '}
              <span className="text-white font-semibold">{activeRecordingSession.candidateName}</span>
              {' • '}
              <span className="text-rose-300 font-normal">{activeRecordingSession.role}</span>
            </span>
          </div>
          <button
            type="button"
            onClick={() => onNavigate?.('live')}
            className="flex items-center space-x-1 px-2.5 py-1 bg-rose-600 hover:bg-rose-500 text-white text-xs font-bold rounded-lg shadow transition cursor-pointer shrink-0"
          >
            <span>Вернуться к записи</span>
            <ArrowRight className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* Right: Audio Monitoring */}
      <div className="flex items-center space-x-4">
        <AudioMeters isCapturing={isCapturing} />
      </div>
    </header>
  );
};
