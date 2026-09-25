import React from 'react';
import { InterviewStatus, CaptureMode } from '../types';
import { AudioMeters } from './AudioMeters';
import {
  ArrowRight,
  Briefcase,
  FileSpreadsheet,
  Moon,
  Plus,
  Settings,
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
  onNewInterview?: () => void;
  hasActiveRecording?: boolean;
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
  isCapturing = false,
  captureMode,
  currentScreen = 'home',
  onNavigate,
  activeRecordingSession = null,
  theme = 'light',
  onToggleTheme,
  onNewInterview,
  hasActiveRecording = false,
}) => {
  const isInterviewScreen = currentScreen === 'live' || currentScreen === 'review' || currentScreen === 'setup';

  return (
    <aside className="app-sidebar" aria-label="Основная навигация">
        <button type="button" className="sidebar-brand" onClick={() => onNavigate?.('home')} aria-label="Перейти к интервью">
          <span className="brand-mark" aria-hidden="true">
            <Sparkles className="h-4 w-4" />
          </span>
          <span className="brand-name">Nebula</span>
        </button>

        <button
          type="button"
          className="sidebar-new-action"
          onClick={onNewInterview}
          disabled={hasActiveRecording}
          title={hasActiveRecording ? 'Запись уже активна' : 'Создать новое интервью'}
        >
          <Plus className="sidebar-nav-icon" />
          <span>Новое интервью</span>
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

        <div className="sidebar-footer">
          <span className="sidebar-footer-line" />
          {activeRecordingSession && currentScreen !== 'live' && (
            <button type="button" onClick={() => onNavigate?.('live')} className="sidebar-recording-return">
              <span className="status-dot recording-pulse" aria-hidden="true" />
              <span className="sidebar-recording-copy">
                <strong>{activeRecordingSession.candidateName}</strong>
                <small>Идёт запись</small>
              </span>
              <ArrowRight className="h-3.5 w-3.5" />
            </button>
          )}

          <div className="sidebar-audio-status">
            <span className="sidebar-utility-label">Аудио</span>
            <AudioMeters isCapturing={isCapturing} captureMode={captureMode || activeRecordingSession?.captureMode} />
          </div>

          <button
            type="button"
            className="theme-toggle sidebar-theme-toggle"
            onClick={onToggleTheme}
            aria-label={theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему'}
            title={theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'}
          >
            {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            <span className="theme-toggle-label">{theme === 'dark' ? 'Светлая' : 'Тёмная'}</span>
          </button>
        </div>
    </aside>
  );
};
