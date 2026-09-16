import React from 'react';
import { InterviewStatus, CaptureMode } from '../types';
import { AudioMeters } from './AudioMeters';
import {
  ArrowRight,
  Briefcase,
  FileSpreadsheet,
  Moon,
  Settings,
  ShieldCheck,
  Sparkles,
  Sun,
} from 'lucide-react';

type Theme = 'light' | 'dark';

interface HeaderProps {
  status: InterviewStatus;
  candidateName?: string;
  role?: string;
  isCapturing?: boolean;
  captureMode?: CaptureMode;
  currentScreen?: 'home' | 'templates' | 'setup' | 'live' | 'review' | 'settings';
  onNavigate?: (screen: 'home' | 'templates' | 'live' | 'settings') => void;
  activeRecordingSession?: {
    interviewId: string;
    candidateName: string;
    role: string;
    captureMode?: CaptureMode;
  } | null;
  theme?: Theme;
  onToggleTheme?: () => void;
}

interface NavButtonProps {
  label: string;
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}

const NavButton: React.FC<NavButtonProps> = ({ label, active, onClick, children }) => (
  <button
    type="button"
    onClick={onClick}
    className={`sidebar-nav-item${active ? ' sidebar-nav-item-active' : ''}`}
    aria-current={active ? 'page' : undefined}
  >
    {children}
    <span>{label}</span>
  </button>
);

export const Header: React.FC<HeaderProps> = ({
  status,
  candidateName = 'Новое собеседование',
  role = 'Позиция не выбрана',
  isCapturing = false,
  captureMode,
  currentScreen = 'home',
  onNavigate,
  activeRecordingSession = null,
  theme = 'light',
  onToggleTheme,
}) => {
  const isInterviewScreen = currentScreen === 'live' || currentScreen === 'review' || currentScreen === 'setup';

  const getStatusBadge = () => {
    switch (status) {
      case 'recording':
        return (
          <span className="status-badge status-recording">
            <span className="status-dot recording-pulse" />
            <span>Запись</span>
          </span>
        );
      case 'paused':
        return <span className="status-badge status-paused">Пауза</span>;
      case 'review':
        return <span className="status-badge status-review">Проверка</span>;
      case 'finalized':
        return (
          <span className="status-badge status-finalized">
            <ShieldCheck className="h-3.5 w-3.5" />
            <span>Завершено</span>
          </span>
        );
      default:
        return <span className="status-badge status-draft">Подготовка</span>;
    }
  };

  return (
    <>
      <aside className="app-sidebar" aria-label="Основная навигация">
        <button type="button" className="sidebar-brand" onClick={() => onNavigate?.('home')} aria-label="Перейти к интервью">
          <span className="brand-mark" aria-hidden="true">
            <Sparkles className="h-4 w-4" />
          </span>
          <span className="brand-name">Nebula</span>
        </button>

        <nav className="sidebar-nav">
          <NavButton
            label="Интервью"
            active={isInterviewScreen || currentScreen === 'home'}
            onClick={() => onNavigate?.('home')}
          >
            <FileSpreadsheet className="sidebar-nav-icon" />
          </NavButton>
          <NavButton
            label="Должности и вопросы"
            active={currentScreen === 'templates'}
            onClick={() => onNavigate?.('templates')}
          >
            <Briefcase className="sidebar-nav-icon" />
          </NavButton>
          <NavButton
            label="Настройки"
            active={currentScreen === 'settings'}
            onClick={() => onNavigate?.('settings')}
          >
            <Settings className="sidebar-nav-icon" />
          </NavButton>
        </nav>

        <div className="sidebar-footer" aria-hidden="true">
          <span className="sidebar-footer-line" />
        </div>
      </aside>

      <header className="app-topbar">
        <div className="topbar-context">
          {isInterviewScreen && (
            <div className="interview-context">
              <div className="interview-context-title">
                <span className="interview-context-name">{candidateName}</span>
                {getStatusBadge()}
              </div>
              <span className="interview-context-role">{role}</span>
            </div>
          )}
        </div>

        <div className="topbar-actions">
          {activeRecordingSession && currentScreen !== 'live' && (
            <div className="recording-return-banner">
              <span className="status-dot recording-pulse" aria-hidden="true" />
              <span className="recording-return-copy">
                Идёт запись: <strong>{activeRecordingSession.candidateName}</strong>
              </span>
              <button type="button" onClick={() => onNavigate?.('live')} className="recording-return-button">
                <span>Вернуться к записи</span>
                <ArrowRight className="h-3.5 w-3.5" />
              </button>
            </div>
          )}

          <AudioMeters isCapturing={isCapturing} captureMode={captureMode || activeRecordingSession?.captureMode} />

          <button
            type="button"
            className="theme-toggle"
            onClick={onToggleTheme}
            aria-label={theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему'}
            title={theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'}
          >
            {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            <span className="theme-toggle-label">{theme === 'dark' ? 'Светлая' : 'Тёмная'}</span>
          </button>
        </div>
      </header>
    </>
  );
};
