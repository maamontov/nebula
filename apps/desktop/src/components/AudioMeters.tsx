import React, { useEffect, useState } from 'react';
import { AudioLevels, CaptureMode } from '../types';
import { getAudioLevels } from '../services/api';
import { Mic, Volume2, Radio } from 'lucide-react';

interface AudioMetersProps {
  isCapturing?: boolean;
  captureMode?: CaptureMode;
}

export const AudioMeters: React.FC<AudioMetersProps> = ({ isCapturing = true, captureMode }) => {
  const [levels, setLevels] = useState<AudioLevels>({
    interviewer_rms: 0,
    interviewer_peak: 0,
    candidate_rms: 0,
    candidate_peak: 0,
    shared_rms: 0,
    shared_peak: 0,
  });

  useEffect(() => {
    if (!isCapturing) return;

    const interval = setInterval(async () => {
      try {
        const lvl = await getAudioLevels();
        setLevels(lvl);
      } catch (err) {
        // Fallback for silence
        setLevels({
          interviewer_rms: 0,
          interviewer_peak: 0,
          candidate_rms: 0,
          candidate_peak: 0,
          shared_rms: 0,
          shared_peak: 0,
        });
      }
    }, 150);

    return () => clearInterval(interval);
  }, [isCapturing]);

  // Convert RMS (0.0 - 1.0) to bar percentage (0 - 100%) with log scale for audio perception
  const toPercent = (rms: number) => Math.min(100, Math.max(2, Math.round(Math.sqrt(rms) * 100)));

  const isSingleSource = captureMode === 'single_source' || (captureMode === undefined && (levels.shared_rms ?? 0) > 0);

  const sharedPercent = toPercent(levels.shared_rms ?? 0);
  const micPercent = toPercent(levels.interviewer_rms);
  const spkPercent = toPercent(levels.candidate_rms);

  const hasSharedTelemetry = (levels.shared_peak ?? 0) > 0 || (levels.shared_rms ?? 0) > 0;
  const hasDualTelemetry = levels.interviewer_peak > 0 || levels.candidate_peak > 0 || levels.interviewer_rms > 0 || levels.candidate_rms > 0;

  if (isSingleSource) {
    return (
      <div className="flex items-center space-x-6 px-4 py-2 bg-slate-900/80 border border-slate-800 rounded-lg shadow-sm">
        <div className="flex items-center space-x-2">
          <Radio className={`w-4 h-4 ${hasSharedTelemetry && sharedPercent > 10 ? 'text-indigo-400' : 'text-slate-500'}`} />
          <span className="text-xs font-medium text-slate-400 w-24">Общий аудиовход</span>
          {hasSharedTelemetry ? (
            <div className="w-36 h-2.5 bg-slate-950 rounded-full overflow-hidden p-0.5 flex items-center border border-slate-800">
              <div
                className={`h-full rounded-full transition-all duration-100 ${
                  (levels.shared_peak ?? 0) > 0.9 ? 'bg-red-500' : sharedPercent > 60 ? 'bg-amber-400' : 'bg-indigo-500'
                }`}
                style={{ width: `${sharedPercent}%` }}
              />
            </div>
          ) : (
            <span className="text-[10px] text-slate-500 font-mono">нет данных</span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-center space-x-6 px-4 py-2 bg-slate-900/80 border border-slate-800 rounded-lg shadow-sm">
      {/* Interviewer (Mic) */}
      <div className="flex items-center space-x-2">
        <Mic className={`w-4 h-4 ${hasDualTelemetry && micPercent > 10 ? 'text-indigo-400' : 'text-slate-500'}`} />
        <span className="text-xs font-medium text-slate-400 w-16">Интервьюер</span>
        {hasDualTelemetry ? (
          <div className="w-24 h-2.5 bg-slate-950 rounded-full overflow-hidden p-0.5 flex items-center border border-slate-800">
            <div
              className={`h-full rounded-full transition-all duration-100 ${
                levels.interviewer_peak > 0.9 ? 'bg-red-500' : micPercent > 60 ? 'bg-amber-400' : 'bg-indigo-500'
              }`}
              style={{ width: `${micPercent}%` }}
            />
          </div>
        ) : (
          <span className="text-[10px] text-slate-500 font-mono">нет данных</span>
        )}
      </div>

      {/* Candidate (Loopback / Remote) */}
      <div className="flex items-center space-x-2">
        <Volume2 className={`w-4 h-4 ${hasDualTelemetry && spkPercent > 10 ? 'text-emerald-400' : 'text-slate-500'}`} />
        <span className="text-xs font-medium text-slate-400 w-16">Кандидат</span>
        {hasDualTelemetry ? (
          <div className="w-24 h-2.5 bg-slate-950 rounded-full overflow-hidden p-0.5 flex items-center border border-slate-800">
            <div
              className={`h-full rounded-full transition-all duration-100 ${
                levels.candidate_peak > 0.9 ? 'bg-red-500' : spkPercent > 60 ? 'bg-amber-400' : 'bg-emerald-500'
              }`}
              style={{ width: `${spkPercent}%` }}
            />
          </div>
        ) : (
          <span className="text-[10px] text-slate-500 font-mono">нет данных</span>
        )}
      </div>
    </div>
  );
};
