import React from 'react';
import { InterviewStatus } from '../types';
import { AudioMeters } from './AudioMeters';
import { Sparkles, ShieldCheck } from 'lucide-react';

interface HeaderProps {
  status: InterviewStatus;
  candidateName?: string;
  role?: string;
  isCapturing?: boolean;
}

export const Header: React.FC<HeaderProps> = ({
  status,
  candidateName = 'Новое собеседование',
  role = 'Позиция не выбрана',
  isCapturing = false,
}) => {
  const getStatusBadge = () => {
    switch (status) {
      case 'recording':
        return (
          <span className="flex items-center space-x-1.5 px-3 py-1 text-xs font-semibold text-rose-300 bg-rose-950/60 border border-rose-800/80 rounded-full shadow-sm">
            <span className="w-2 h-2 rounded-full bg-rose-500 recording-pulse" />
            <span>ЗАПИСЬ (LIVE)</span>
          </span>
        );
      case 'paused':
        return (
          <span className="px-3 py-1 text-xs font-semibold text-amber-300 bg-amber-950/60 border border-amber-800/80 rounded-full">
            ПАУЗА
          </span>
        );
      case 'review':
        return (
          <span className="px-3 py-1 text-xs font-semibold text-blue-300 bg-blue-950/60 border border-blue-800/80 rounded-full">
            ПРОВЕРКА И ИТОГИ
          </span>
        );
      case 'finalized':
        return (
          <span className="flex items-center space-x-1 px-3 py-1 text-xs font-semibold text-emerald-300 bg-emerald-950/60 border border-emerald-800/80 rounded-full">
            <ShieldCheck className="w-3.5 h-3.5" />
            <span>ЗАВЕРШЕНО</span>
          </span>
        );
      default:
        return (
          <span className="px-3 py-1 text-xs font-semibold text-slate-400 bg-slate-800/80 border border-slate-700/80 rounded-full">
            ПОДГОТОВКА
          </span>
        );
    }
  };

  return (
    <header className="h-16 px-6 bg-slate-900/90 border-b border-slate-800/80 flex items-center justify-between select-none">
      {/* Brand & Context */}
      <div className="flex items-center space-x-4">
        <div className="flex items-center space-x-2">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-tr from-indigo-600 to-purple-500 flex items-center justify-center shadow-lg shadow-indigo-500/20">
            <Sparkles className="w-4 h-4 text-white" />
          </div>
          <span className="font-bold text-lg tracking-tight bg-gradient-to-r from-white via-slate-100 to-slate-400 bg-clip-text text-transparent">
            Nebula
          </span>
        </div>

        <div className="h-4 w-px bg-slate-800" />

        <div>
          <div className="flex items-center space-x-2">
            <h1 className="text-sm font-semibold text-slate-100">{candidateName}</h1>
            {getStatusBadge()}
          </div>
          <p className="text-xs text-slate-400">{role}</p>
        </div>
      </div>

      {/* Audio Monitoring */}
      <div className="flex items-center space-x-4">
        <AudioMeters isCapturing={isCapturing} />
      </div>
    </header>
  );
};
