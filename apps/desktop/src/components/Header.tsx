import React from 'react';
import { CaptureMode } from '../types';
import { AudioMeters } from './AudioMeters';
import {
  ArrowRight,
  Briefcase,
  FileSpreadsheet,
  Moon,
  Settings,
  Sparkles,
  Sun,
} from 'lucide-react';

type Theme = 'light' | 'dark';

interface HeaderProps {
  isCapturing?: boolean;
  captureMode?: CaptureMode;
  currentScreen?: 'home' | 'templates' | 'setup' | 'live' | 'review' | 'settings';
  onNavigate?: (screen: 'home' | 'templates' | 'live' | 'settings') => void;
  activeRecordingSession?: {
    interviewId: string;
    candidateName: string;
    role: string;
    captureMode?: CaptureMode;
    isPaused?: boolean;
  } | null;
  /** The live screen renders its own session bar, so the top bar is hidden there. */
  showTopbar?: boolean;
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
  isCapturing = false,
  captureMode,
  currentScreen = 'home',
  onNavigate,
  activeRecordingSession = null,
  theme = 'light',
  onToggleTheme,
  showTopbar = true,
}) => {
  const isInterviewScreen = currentScreen === 'live' || currentScreen === 'review' || currentScreen === 'setup';

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

      {showTopbar && (
      <header className="app-topbar">
        <div className="topbar-context" />

        <div className="topbar-actions">
          {activeRecordingSession && currentScreen !== 'live' && (
            <div className={`recording-return-banner${activeRecordingSession.isPaused ? ' recording-return-paused' : ''}`}>
              <span className={`status-dot${activeRecordingSession.isPaused ? '' : ' recording-pulse'}`} aria-hidden="true" />
              <span className="recording-return-copy">
                {activeRecordingSession.isPaused ? 'Запись на паузе' : 'Идёт запись'}: <strong>{activeRecordingSession.candidateName}</strong>
              </span>
              <button type="button" onClick={() => onNavigate?.('live')} className="recording-return-button">
                <span>Вернуться к записи</span>
                <ArrowRight className="h-3.5 w-3.5" />
              </button>
            </div>
          )}

          {isCapturing && (
            <AudioMeters isCapturing={isCapturing} captureMode={captureMode || activeRecordingSession?.captureMode} />
          )}

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
      )}
    </>
  );
};
