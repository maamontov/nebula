import { AudioDevice, AudioLevels, InterviewDetails, InterviewPlan, AssessmentProposal, TranscriptSegment, InterviewStatus } from '../types';

const API_BASE = 'http://127.0.0.1:8000/api/v1';

// Check if running inside Tauri window
export function isTauriEnvironment(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

// Dynamically invoke Tauri command or use browser mock
async function invokeTauri<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (isTauriEnvironment()) {
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      return await invoke<T>(cmd, args);
    } catch (e) {
      console.warn(`Tauri invoke ${cmd} failed:`, e);
      throw e;
    }
  }

  // Browser development fallback
  console.log(`[Browser Mock] Tauri invoke: ${cmd}`, args);
  if (cmd === 'list_audio_devices') {
    return [
      { id: 'default_mic', name: 'Встроенный микрофон (MacBook Air)', is_default: true, channels: 1, sample_rate: 48000 },
      { id: 'usb_headset', name: 'USB Наушники с гарнитурой', is_default: false, channels: 2, sample_rate: 48000 },
      { id: 'loopback_dev', name: 'BlackHole 2ch (Virtual Loopback)', is_default: false, channels: 2, sample_rate: 48000 },
    ] as unknown as T;
  }
  if (cmd === 'get_audio_levels') {
    return {
      interviewer_rms: 0.05 + Math.random() * 0.15,
      interviewer_peak: 0.25,
      candidate_rms: 0.08 + Math.random() * 0.2,
      candidate_peak: 0.35,
    } as unknown as T;
  }
  if (cmd === 'start_capture') {
    return { status: 'started', session_id: args?.session_id } as unknown as T;
  }
  if (cmd === 'stop_capture') {
    return { status: 'stopped', total_chunks: 12, drift_ms: 4 } as unknown as T;
  }
  return {} as T;
}

// -------------------------------------------------------------
// Audio & Tauri IPC
// -------------------------------------------------------------
export async function getAudioDevices(): Promise<AudioDevice[]> {
  return invokeTauri<AudioDevice[]>('list_audio_devices');
}

export async function getAudioLevels(): Promise<AudioLevels> {
  return invokeTauri<AudioLevels>('get_audio_levels');
}

export async function startAudioCapture(
  sessionId: string,
  interviewerDevId: string,
  candidateDevId: string,
  spoolDir: string,
  consentGiven: boolean
): Promise<{ status: string }> {
  return invokeTauri('start_capture', {
    sessionId,
    interviewerDevId,
    candidateDevId,
    spoolDir,
    consentGiven,
  });
}

export async function stopAudioCapture(): Promise<{ status: string; total_chunks: number; drift_ms: number }> {
  return invokeTauri('stop_capture');
}

// -------------------------------------------------------------
// Backend API (FastAPI)
// -------------------------------------------------------------
export async function createInterview(data: {
  id: string;
  title: string;
  candidate_name: string;
  role: string;
  plan?: InterviewPlan;
}): Promise<InterviewDetails> {
  const res = await fetch(`${API_BASE}/interviews`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Create interview error: ${res.statusText}`);
  return res.json();
}

export async function getInterview(id: string): Promise<{
  interview: InterviewDetails;
  plan?: InterviewPlan;
  transcript_segments: TranscriptSegment[];
  assessment_proposals: AssessmentProposal[];
}> {
  const res = await fetch(`${API_BASE}/interviews/${id}`);
  if (!res.ok) throw new Error(`Get interview error: ${res.statusText}`);
  return res.json();
}

export async function updateInterviewStatus(
  id: string,
  currentStatus: InterviewStatus,
  targetStatus: InterviewStatus,
  consent?: { confirmedAt: string; version: string }
): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/interviews/${id}/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      current_status: currentStatus,
      target_status: targetStatus,
      consent_confirmed_at: consent?.confirmedAt,
      consent_version: consent?.version,
    }),
  });
  if (!res.ok) throw new Error(`Update status error: ${res.statusText}`);
  return res.json();
}

export async function approveAssessment(
  interviewId: string,
  proposalId: string,
  reviewedScores?: unknown[],
  reviewerNotes?: string
): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/assessments/${proposalId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      reviewed_scores: reviewedScores,
      reviewer_notes: reviewerNotes,
    }),
  });
  if (!res.ok) throw new Error(`Approve assessment error: ${res.statusText}`);
  return res.json();
}
