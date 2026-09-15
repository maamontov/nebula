import React, { useEffect, useState } from 'react';
import { AudioLevels, CaptureMode } from '../types';
import { Mic, Radio, Volume2 } from 'lucide-react';
import { getAudioLevels } from '../services/api';

interface AudioMetersProps {
  isCapturing?: boolean;
  captureMode?: CaptureMode;
}

const initialLevels: AudioLevels = {
  interviewer_rms: 0,
  interviewer_peak: 0,
  candidate_rms: 0,
  candidate_peak: 0,
  shared_rms: 0,
  shared_peak: 0,
};

export const AudioMeters: React.FC<AudioMetersProps> = ({ isCapturing = true, captureMode }) => {
  const [levels, setLevels] = useState<AudioLevels>(initialLevels);
  const [hasTelemetry, setHasTelemetry] = useState(false);

  useEffect(() => {
    if (!isCapturing) {
      setHasTelemetry(false);
      return;
    }

    const interval = setInterval(async () => {
      try {
        setLevels(await getAudioLevels());
        setHasTelemetry(true);
      } catch {
        setLevels(initialLevels);
        setHasTelemetry(false);
      }
    }, 150);

    return () => clearInterval(interval);
  }, [isCapturing]);

  const toPercent = (rms: number) => Math.min(100, Math.max(0, Math.round(Math.sqrt(rms) * 100)));
  const isSingleSource =
    captureMode === 'single_source' ||
    (captureMode === undefined &&
      ((levels.shared_rms ?? 0) > 0 ||
        ((levels.shared_peak ?? 0) > 0 && levels.interviewer_peak === 0 && levels.candidate_peak === 0)));

  const getLevelClass = (peak: number, percent: number, defaultClass: string) => {
    if (peak > 0.9) return 'audio-level-fill audio-level-danger';
    if (percent > 60) return 'audio-level-fill audio-level-warning';
    return `audio-level-fill ${defaultClass}`;
  };

  const renderLevel = (percent: number, peak: number, defaultClass: string) =>
    hasTelemetry ? (
      <span className="audio-level-track" aria-hidden="true">
        <span className={getLevelClass(peak, percent, defaultClass)} style={{ width: `${percent}%` }} />
      </span>
    ) : (
      <span className="audio-no-data">нет данных</span>
    );

  if (isSingleSource) {
    const percent = toPercent(levels.shared_rms ?? 0);
    return (
      <div className="audio-monitor" aria-label="Мониторинг общего аудиовхода">
        <Radio className={`audio-monitor-icon${hasTelemetry && percent > 10 ? ' audio-monitor-icon-active' : ''}`} />
        <span className="audio-monitor-label">Общий аудиовход</span>
        {renderLevel(percent, levels.shared_peak ?? 0, 'audio-level-accent')}
      </div>
    );
  }

  const micPercent = toPercent(levels.interviewer_rms);
  const candidatePercent = toPercent(levels.candidate_rms);

  return (
    <div className="audio-monitor audio-monitor-dual" aria-label="Мониторинг аудио">
      <div className="audio-channel">
        <Mic className={`audio-monitor-icon${hasTelemetry && micPercent > 10 ? ' audio-monitor-icon-active' : ''}`} />
        <span className="audio-monitor-label">Интервьюер</span>
        {renderLevel(micPercent, levels.interviewer_peak, 'audio-level-accent')}
      </div>
      <div className="audio-channel">
        <Volume2 className={`audio-monitor-icon${hasTelemetry && candidatePercent > 10 ? ' audio-monitor-icon-success' : ''}`} />
        <span className="audio-monitor-label">Кандидат</span>
        {renderLevel(candidatePercent, levels.candidate_peak, 'audio-level-success')}
      </div>
    </div>
  );
};
