import { isTauri, invoke } from '@tauri-apps/api/core';
import { AudioDevice, AudioLevels, InterviewDetails, InterviewPlan, AssessmentProposal, TranscriptSegment, InterviewStatus, SpeakerRole, CaptureMode, TrackManifest, JobTemplate, PaginatedInterviews, ReportRevisionSummary, FollowUpSuggestion, FollowUpsStateResponse, GenerateFollowUpsRequest, PatchFollowUpSuggestionRequest, FollowUpMode, JobStatusResponse, AiSettingsResponse, UpdateAiSettingsRequest, TestAiSettingsRequest, TestAiSettingsResponse } from '../types';

const API_BASE = 'http://127.0.0.1:8000/api/v1';

// Check if running inside Tauri window
export function isTauriEnvironment(): boolean {
  try {
    return isTauri();
  } catch {
    return typeof window !== 'undefined' && ('__TAURI_INTERNALS__' in window || '__TAURI__' in window);
  }
}

// Dynamically invoke Tauri command or use browser mock
async function invokeTauri<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  if (isTauriEnvironment()) {
    try {
      return await invoke<T>(cmd, args);
    } catch (e) {
      console.warn(`Tauri invoke ${cmd} failed:`, e);
      throw e;
    }
  }

  // If running in browser without Tauri:
  // Check if explicit demo mock mode was enabled via window.NEBULA_ENABLE_DEMO_MOCK
  const isDemoMode = typeof window !== 'undefined' && (window as unknown as { NEBULA_ENABLE_DEMO_MOCK?: boolean }).NEBULA_ENABLE_DEMO_MOCK === true;
  if (isDemoMode) {
    console.log(`[Explicit Demo Mode] Tauri invoke: ${cmd}`, args);
    if (cmd === 'list_audio_devices') {
      return [
        { id: 'demo_mic', name: 'Demo Микрофон (Mock)', is_default: true, channels: 1, sample_rate: 48000 },
        { id: 'demo_speaker', name: 'Demo Loopback (Mock)', is_default: false, channels: 2, sample_rate: 48000 },
      ] as unknown as T;
    }
    if (cmd === 'get_audio_levels') {
      return {
        interviewer_rms: 0.05,
        interviewer_peak: 0.1,
        candidate_rms: 0.05,
        candidate_peak: 0.1,
      } as unknown as T;
    }
    if (cmd === 'start_capture') {
      return { status: 'started', session_id: args?.sessionId || args?.session_id || 'demo' } as unknown as T;
    }
    if (cmd === 'pause_capture') {
      return { status: 'paused', session_id: 'demo', elapsed_ms: 1000 } as unknown as T;
    }
    if (cmd === 'resume_capture') {
      return { status: 'resumed', session_id: 'demo', epoch: 2, elapsed_ms: 1000 } as unknown as T;
    }
    if (cmd === 'stop_capture') {
      return {
        status: 'stopped',
        session_id: 'demo',
        total_chunks: 1,
        total_samples_interviewer: 16000,
        total_samples_candidate: 16000,
        total_duration_ms: 1000,
        drift_ms: 0,
        skew_ms: 0,
        dropped_samples: 0,
        manifests: [],
      } as unknown as T;
    }
    if (cmd === 'get_upload_progress') {
      return {
        session_id: (args?.sessionId as string) || 'demo',
        total_discovered: 10,
        total_acked: 10,
        total_failed: 0,
        in_flight: 0,
        is_active: true,
        last_error: null,
        total_chunks: 10,
        uploaded_chunks: 10,
      } as unknown as T;
    }
    if (cmd === 'get_active_session') {
      return {
        is_recording: false,
        is_paused: false,
        session_id: null,
        elapsed_ms: 0,
        epoch: 0,
      } as unknown as T;
    }
    if (cmd === 'get_system_config') {
      return {
        data_dir: '/tmp/nebula/data',
        capture_spool_dir: '/tmp/nebula/data/spool',
        backend_spool_dir: '/tmp/nebula/data/spool',
        backend_url: 'http://127.0.0.1:8000',
        db_path: '/tmp/nebula/data/nebula.db',
        backup_dir: '/tmp/nebula/data/backups',
      } as unknown as T;
    }
    return {} as T;
  }

  // Normal browser without capture capability: do NOT fake success or audio levels
  if (cmd === 'list_audio_devices') {
    return [] as unknown as T;
  }
  if (cmd === 'get_audio_levels') {
    return {
      interviewer_rms: 0,
      interviewer_peak: 0,
      candidate_rms: 0,
      candidate_peak: 0,
      shared_rms: 0,
      shared_peak: 0,
    } as unknown as T;
  }
  if (cmd === 'start_capture') {
    throw new Error('Захват аудио недоступен: веб-браузер не имеет доступа к Tauri аудиоядру. Запустите десктопное приложение Tauri.');
  }
  if (cmd === 'pause_capture') {
    return { status: 'paused', session_id: '', elapsed_ms: 0 } as unknown as T;
  }
  if (cmd === 'resume_capture') {
    return { status: 'resumed', session_id: '', epoch: 1, elapsed_ms: 0 } as unknown as T;
  }
  if (cmd === 'stop_capture') {
    return {
      status: 'stopped',
      session_id: '',
      total_chunks: 0,
      total_samples_interviewer: 0,
      total_samples_candidate: 0,
      total_duration_ms: 0,
      drift_ms: 0,
      skew_ms: 0,
      dropped_samples: 0,
      manifests: [],
    } as unknown as T;
  }
  if (cmd === 'get_upload_progress') {
    return {
      session_id: (args?.sessionId as string) || '',
      total_discovered: 0,
      total_acked: 0,
      total_failed: 0,
      in_flight: 0,
      is_active: false,
      last_error: null,
      total_chunks: 0,
      uploaded_chunks: 0,
    } as unknown as T;
  }
  if (cmd === 'get_active_session') {
    return {
      is_recording: false,
      is_paused: false,
      session_id: null,
      elapsed_ms: 0,
      epoch: 0,
    } as unknown as T;
  }
  if (cmd === 'get_system_config') {
    const res = await fetch(`${API_BASE}/system/config`);
    if (res.ok) return res.json();
    throw new Error('Failed to load system config');
  }
  return {} as T;
}

// -------------------------------------------------------------
// Audio & Tauri IPC
// -------------------------------------------------------------
export interface StartCaptureResult {
  status: string;
  session_id: string;
}

export type { TrackManifest };

export interface StopCaptureResult {
  status: string;
  session_id: string;
  total_chunks: number;
  total_samples_interviewer: number;
  total_samples_candidate: number;
  total_duration_ms: number;
  drift_ms: number;
  skew_ms: number;
  dropped_samples: number;
  manifests: TrackManifest[];
}

export interface SessionUploadProgress {
  session_id: string;
  total_discovered: number;
  total_acked: number;
  total_failed: number;
  in_flight: number;
  is_active: boolean;
  last_error: string | null;
  total_chunks: number;
  uploaded_chunks: number;
}

export interface ActiveSessionInfo {
  is_recording: boolean;
  is_paused: boolean;
  session_id: string | null;
  elapsed_ms: number;
  epoch: number;
}

export interface SystemConfig {
  capture_spool_dir: string;
  backend_url: string;
  data_dir?: string;
  backend_spool_dir?: string;
  db_path?: string;
  backup_dir?: string;
}

export async function getAudioDevices(): Promise<AudioDevice[]> {
  return invokeTauri<AudioDevice[]>('list_audio_devices');
}

export async function getAudioLevels(): Promise<AudioLevels> {
  return invokeTauri<AudioLevels>('get_audio_levels');
}

export interface StartAudioCaptureOptions {
  sessionId: string;
  interviewerDevId?: string;
  candidateDevId?: string;
  sharedDevId?: string;
  spoolDir?: string;
  consentGiven?: boolean;
  captureMode?: 'single_source' | 'dual_source';
}

export async function startAudioCapture(
  optionsOrSessionId: StartAudioCaptureOptions | string,
  interviewerDevId?: string,
  candidateDevId?: string,
  sharedDevIdOrSpoolDir?: string,
  consentGiven: boolean = true,
  captureMode?: 'single_source' | 'dual_source',
  sharedDevId?: string
): Promise<StartCaptureResult> {
  let opts: StartAudioCaptureOptions;
  if (typeof optionsOrSessionId === 'object') {
    opts = optionsOrSessionId;
  } else {
    if (captureMode === 'single_source' && !sharedDevId && sharedDevIdOrSpoolDir) {
      opts = {
        sessionId: optionsOrSessionId,
        interviewerDevId,
        candidateDevId,
        sharedDevId: sharedDevIdOrSpoolDir,
        consentGiven,
        captureMode,
      };
    } else {
      opts = {
        sessionId: optionsOrSessionId,
        interviewerDevId,
        candidateDevId,
        sharedDevId,
        spoolDir: sharedDevIdOrSpoolDir,
        consentGiven,
        captureMode,
      };
    }
  }

  const effectiveShared = opts.sharedDevId || (opts.captureMode === 'single_source' ? (opts.interviewerDevId || opts.candidateDevId) : undefined);
  const effectiveInterviewer = opts.captureMode === 'single_source' ? (opts.interviewerDevId || effectiveShared) : opts.interviewerDevId;

  return invokeTauri<StartCaptureResult>('start_capture', {
    sessionId: opts.sessionId,
    interviewerDevId: effectiveInterviewer || undefined,
    candidateDevId: opts.candidateDevId || undefined,
    sharedDevId: effectiveShared || undefined,
    spoolDir: opts.spoolDir || undefined,
    consentGiven: opts.consentGiven !== undefined ? opts.consentGiven : true,
    captureMode: opts.captureMode || 'dual_source',
  });
}

export async function pauseAudioCapture(): Promise<{ status: string; session_id: string; elapsed_ms: number }> {
  return invokeTauri('pause_capture');
}

export async function resumeAudioCapture(): Promise<{ status: string; session_id: string; epoch: number; elapsed_ms: number }> {
  return invokeTauri('resume_capture');
}

export async function stopAudioCapture(): Promise<StopCaptureResult> {
  return invokeTauri<StopCaptureResult>('stop_capture');
}

export async function getUploadProgress(sessionId: string): Promise<SessionUploadProgress> {
  const res = await invokeTauri<any>('get_upload_progress', { sessionId });
  const totalDiscovered = res?.total_discovered ?? res?.total_chunks ?? 0;
  const totalAcked = res?.total_acked ?? res?.uploaded_chunks ?? 0;
  return {
    session_id: res?.session_id || sessionId,
    total_discovered: totalDiscovered,
    total_acked: totalAcked,
    total_failed: res?.total_failed ?? 0,
    in_flight: res?.in_flight ?? res?.in_flight_chunks ?? 0,
    is_active: res?.is_active ?? false,
    last_error: res?.last_error ?? null,
    total_chunks: totalDiscovered,
    uploaded_chunks: totalAcked,
  };
}

export async function getActiveSession(): Promise<ActiveSessionInfo> {
  return invokeTauri<ActiveSessionInfo>('get_active_session');
}

export async function getSystemConfig(): Promise<SystemConfig> {
  try {
    return await invokeTauri<SystemConfig>('get_system_config');
  } catch {
    const res = await fetch(`${API_BASE}/system/config`);
    if (res.ok) return res.json();
    throw new Error('Failed to load system config');
  }
}

/**
 * Provider and model profiles the backend actually uses for live work.
 * Kept separate from the Tauri system config: the desktop shell knows nothing about them.
 */
export interface ActiveModelInfo {
  provider_id: string;
  provider_name: string;
  model_profile_id: string;
  upstream_model_id: string;
}

export interface ActiveModelsInfo {
  llm: ActiveModelInfo;
  stt: ActiveModelInfo;
}

export async function getActiveModels(): Promise<ActiveModelsInfo> {
  const res = await fetch(`${API_BASE}/system/models`);
  if (!res.ok) throw new Error(`Get active models error: ${res.statusText}`);
  return res.json();
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
  capture_mode?: CaptureMode;
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
  human_assessments?: Array<{
    id: string;
    question_id: string;
    rubric_revision_id: string;
    transcript_revision_id: string;
    reviewer_id?: string;
    scores: Array<{ criterion_id: string; score: number; explanation?: string }>;
    reviewer_notes?: string;
    is_manually_adjusted: boolean;
    is_stale: boolean;
    updated_at: string;
  }>;
  scoring?: {
    final_score_100: number | null;
    coverage_percentage: number;
    total_planned_weight: number;
    evaluated_weight: number;
  };
  asked_followups?: FollowUpSuggestion[];
}> {
  const res = await fetch(`${API_BASE}/interviews/${id}`);
  if (!res.ok) throw new Error(`Get interview error: ${res.statusText}`);
  return res.json();
}

export async function reviewAssessment(
  interviewId: string,
  questionId: string,
  data: {
    expected_transcript_revision: string;
    scores: Array<{ criterion_id: string; score: number; explanation?: string }>;
    reviewer_notes?: string;
    reviewer_id?: string;
    is_manually_adjusted?: boolean;
    is_excluded?: boolean;
    exclusion_reason?: string;
  }
): Promise<{ status: string; assessment_id: string; question_id: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/assessments/${questionId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    if (res.status === 409) {
      const errBody = await res.json().catch(() => ({ detail: 'Конфликт версий стенограммы' }));
      throw new Error(`409: ${errBody.detail || 'Конфликт ревизии'}`);
    }
    throw new Error(`Review assessment error: ${res.statusText}`);
  }
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

export async function pauseInterview(interviewId: string): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/pause`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(`Pause interview error: ${res.statusText}`);
  return res.json();
}

export async function resumeInterview(interviewId: string): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/resume`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(`Resume interview error: ${res.statusText}`);
  return res.json();
}

export async function stopInterview(
  interviewId: string,
  manifests?: Array<TrackManifest | Record<string, unknown>>
): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/stop`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ manifests: manifests || [] }),
  });
  if (!res.ok) throw new Error(`Stop interview error: ${res.statusText}`);
  return res.json();
}

export async function getInterviewJobsStatus(
  interviewId: string,
  options?: { jobIds?: string[]; limit?: number }
): Promise<JobStatusResponse> {
  const params = new URLSearchParams();
  if (options?.jobIds && options.jobIds.length > 0) {
    for (const jid of options.jobIds) {
      params.append('job_ids', jid);
    }
  }
  if (options?.limit !== undefined) {
    params.set('limit', String(options.limit));
  }
  const query = params.toString() ? `?${params.toString()}` : '';
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/jobs/status${query}`);
  if (!res.ok) throw new Error(`Get jobs status error: ${res.statusText}`);
  return res.json();
}

export interface InterviewReadiness {
  is_ready: boolean;
  state: string;
  details: string;
  missing_chunks?: Record<string, number[]>;
  stt_jobs?: { completed: number; pending: number; failed: number };
  manifests?: Record<string, unknown>;
}

export async function getInterviewReadiness(interviewId: string): Promise<InterviewReadiness> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/readiness`);
  if (!res.ok) throw new Error(`Get interview readiness error: ${res.statusText}`);
  return res.json();
}

export async function approveAssessment(
  interviewId: string,
  proposalId: string,
  reviewedScores?: unknown[],
  reviewerNotes?: string,
  questionId?: string
): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/assessments/${proposalId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      reviewed_scores: reviewedScores,
      reviewer_notes: reviewerNotes,
      question_id: questionId,
    }),
  });
  if (!res.ok) throw new Error(`Approve assessment error: ${res.statusText}`);
  return res.json();
}

export async function enqueueJob(
  interviewId: string,
  jobType: string,
  payload: Record<string, unknown>,
  maxAttempts: number = 3
): Promise<{ status: string; job_id: string; existing?: boolean }> {
  const jobId = `job-${Date.now()}-${Math.random().toString(36).substring(2, 7)}`;
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/jobs/enqueue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: jobId,
      type: jobType,
      payload,
      max_attempts: maxAttempts,
    }),
  });
  if (!res.ok) throw new Error(`Enqueue job error: ${res.statusText}`);
  return res.json();
}

export async function exportInterview(interviewId: string, revisionNumber?: number): Promise<Record<string, unknown>> {
  const url = revisionNumber !== undefined
    ? `${API_BASE}/interviews/${interviewId}/export?revision_number=${revisionNumber}`
    : `${API_BASE}/interviews/${interviewId}/export`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Export interview error: ${res.statusText}`);
  return res.json();
}

export async function getSummary(interviewId: string): Promise<{
  interview_id: string;
  has_summary: boolean;
  is_confirmed: boolean;
  model_profile_id?: string;
  summary?: any;
  confirmed_markdown?: string;
  confirmed_recommendation?: string;
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/summary`);
  if (!res.ok) throw new Error(`Get summary error: ${res.statusText}`);
  return res.json();
}

export async function confirmSummary(
  interviewId: string,
  data: {
    reviewer_id: string;
    confirmed_markdown: string;
    confirmed_recommendation: string;
    expected_transcript_revision?: string;
  }
): Promise<{ status: string; interview_id: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/summary/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    if (res.status === 409) {
      const errBody = await res.json().catch(() => ({ detail: 'Конфликт ревизий: стенограмма была обновлена' }));
      throw new Error(`409: ${errBody.detail || 'Конфликт ревизий'}`);
    }
    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errBody.detail || `Confirm summary error: ${res.statusText}`);
  }
  return res.json();
}

export async function finalizeInterviewReport(
  interviewId: string,
  data: {
    summary_markdown: string;
    hiring_recommendation: string;
    confirmed_by: string;
    audio_limitations?: string[];
    expected_transcript_revision?: string;
  }
): Promise<{
  status: string;
  final_score_100: number | null;
  coverage_percentage: number;
  sha256_checksum: string;
  report_id: string;
  revision_number: number;
  snapshot: any;
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/report/finalize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    if (res.status === 409) {
      const errBody = await res.json().catch(() => ({ detail: 'Конфликт финализации: обнаружены устаревшие данные или несовпадение ревизий' }));
      throw new Error(`409: ${errBody.detail || 'Конфликт финализации'}`);
    }
    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errBody.detail || `Finalize report error: ${res.statusText}`);
  }
  return res.json();
}

export async function getTranscriptRevisions(interviewId: string): Promise<{
  interview_id: string;
  active_revision_id: string;
  revisions: Array<{
    revision_id: string;
    segment_count: number;
    is_active: boolean;
  }>;
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/revisions/transcript`);
  if (!res.ok) throw new Error(`Get transcript revisions error: ${res.statusText}`);
  return res.json();
}

export async function getTranscriptSegmentsByRevision(
  interviewId: string,
  revisionId: string
): Promise<{
  interview_id: string;
  revision_id: string;
  segments: TranscriptSegment[];
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/revisions/transcript/${revisionId}/segments`);
  if (!res.ok) throw new Error(`Get transcript segments by revision error: ${res.statusText}`);
  return res.json();
}

export async function getAssociations(interviewId: string): Promise<{
  interview_id: string;
  associations: Array<{
    id: string;
    question_id: string;
    segment_id: string;
    confidence: number;
    is_ambiguous: boolean;
    is_manually_adjusted: boolean;
    notes: string;
  }>;
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/associations`);
  if (!res.ok) throw new Error(`Get associations error: ${res.statusText}`);
  return res.json();
}

export async function reassociateSegment(
  interviewId: string,
  segmentId: string,
  newQuestionId: string,
  notes: string = "Ручная привязка интервьюером",
  expectedRevisionId?: string
): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/segments/${segmentId}/associate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      new_question_id: newQuestionId,
      notes,
      ...(expectedRevisionId ? { expected_revision_id: expectedRevisionId } : {}),
    }),
  });
  if (!res.ok) throw new Error(`Reassociate error: ${res.statusText}`);
  return res.json();
}

export async function addTranscriptSegment(
  interviewId: string,
  segment: {
    id: string;
    track_id: string;
    start_time_ms: number;
    end_time_ms: number;
    text: string;
    is_final?: boolean;
  }
): Promise<{ status: string; segment_id: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/segments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      id: segment.id,
      track_id: segment.track_id,
      start_time_ms: segment.start_time_ms,
      end_time_ms: segment.end_time_ms,
      text: segment.text,
      is_final: segment.is_final ?? true,
    }),
  });
  if (!res.ok) throw new Error(`Add segment error: ${res.statusText}`);
  return res.json();
}

export async function getInterviewHealth(interviewId: string): Promise<{
  interview_id: string;
  is_healthy: boolean;
  interviewer: { status: string; warnings: string[] };
  candidate: { status: string; warnings: string[] };
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/health`);
  if (!res.ok) throw new Error(`Get health error: ${res.statusText}`);
  return res.json();
}

export async function startBatchRetranscribe(
  interviewId: string,
  newRevisionId: string = 'trans-rev-2',
  oldRevisionId: string = 'trans-rev-1',
  segments?: unknown[]
): Promise<{ status: string; job_id: string; new_revision_id: string }> {
  const payload: Record<string, unknown> = {
    new_revision_id: newRevisionId,
    old_revision_id: oldRevisionId,
  };
  if (segments && segments.length > 0) {
    payload.segments = segments;
  }
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/batch-retranscribe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    let errDetail = res.statusText;
    try {
      const errData = await res.json();
      if (errData?.detail) errDetail = errData.detail;
    } catch (_) {}
    throw new Error(`Batch retranscribe error: ${errDetail}`);
  }
  return res.json();
}

export async function getTranscriptDiff(
  interviewId: string,
  fromRev: string = 'trans-rev-1',
  toRev: string = 'trans-rev-2'
): Promise<{
  interview_id: string;
  from_revision: string;
  to_revision: string;
  total_segment_diffs: number;
  question_reports: Array<{
    question_id: string;
    is_modified: boolean;
    diff_ratio: number;
    stale_reason?: string;
    broken_evidence_count: number;
    changed_segments_count: number;
  }>;
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/revisions/transcript/diff?from_rev=${fromRev}&to_rev=${toRev}`);
  if (!res.ok) throw new Error(`Get transcript diff error: ${res.statusText}`);
  return res.json();
}

export async function submitHumanReviewWithRevision(
  interviewId: string,
  questionId: string,
  payload: {
    expected_transcript_revision: string;
    scores: unknown[];
    reviewer_notes?: string;
    reviewer_id?: string;
    is_manually_adjusted?: boolean;
  }
): Promise<{ status: string; assessment_id: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/assessments/${questionId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (res.status === 409) {
    const err = await res.json();
    throw new Error(`CONFLICT_409: ${err.detail}`);
  }
  if (!res.ok) throw new Error(`Human review error: ${res.statusText}`);
  return res.json();
}

export async function getExecutiveSummary(interviewId: string): Promise<{
  interview_id: string;
  has_summary: boolean;
  is_confirmed: boolean;
  model_profile_id?: string;
  summary?: Record<string, unknown>;
  confirmed_markdown?: string;
  confirmed_recommendation?: string;
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/summary`);
  if (!res.ok) throw new Error(`Get summary error: ${res.statusText}`);
  return res.json();
}

export async function confirmExecutiveSummary(
  interviewId: string,
  payload: {
    reviewer_id: string;
    confirmed_markdown: string;
    confirmed_recommendation: string;
  }
): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/summary/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Confirm summary error: ${res.statusText}`);
  return res.json();
}

export async function finalizeAndSealReport(
  interviewId: string,
  payload: {
    confirmed_by: string;
    summary_markdown?: string;
    hiring_recommendation?: string;
  }
): Promise<{
  status: string;
  report_id: string;
  revision_number: number;
  final_score_100: number;
  coverage_percentage: number;
  sha256_checksum: string;
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/report/finalize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Finalize report error: ${res.statusText}`);
  return res.json();
}

// -------------------------------------------------------------
// Stage 7: Reliability, Privacy Lifecycle & Backup
// -------------------------------------------------------------
export async function deleteInterview(interviewId: string): Promise<{
  status: string;
  interview_id: string;
  success: boolean;
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error(`Delete interview error: ${res.statusText}`);
  return res.json();
}

export async function createDatabaseBackup(filename?: string): Promise<{
  status: string;
  target_path: string;
}> {
  const res = await fetch(`${API_BASE}/system/backup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(filename ? { target_path: filename } : {}),
  });
  if (!res.ok) throw new Error(`Create backup error: ${res.statusText}`);
  return res.json();
}

export async function checkDatabaseIntegrity(): Promise<{
  status: string;
  integrity_ok: boolean;
}> {
  const res = await fetch(`${API_BASE}/system/integrity`);
  if (!res.ok) throw new Error(`Check integrity error: ${res.statusText}`);
  return res.json();
}

export async function setSegmentSpeakerRole(
  interviewId: string,
  segmentId: string,
  speakerRole: SpeakerRole,
  expectedRevisionId?: string
): Promise<{ status: string; segment: TranscriptSegment }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/segments/${segmentId}/speaker-role`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      speaker_role: speakerRole,
      ...(expectedRevisionId ? { expected_revision_id: expectedRevisionId } : {}),
    }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function splitSegment(
  interviewId: string,
  segmentId: string,
  payload: {
    split_time_ms: number;
    text_part1: string;
    text_part2: string;
    role_part1: SpeakerRole;
    role_part2: SpeakerRole;
    expected_revision_id?: string;
  }
): Promise<{ status: string; segment_part1: TranscriptSegment; segment_part2: TranscriptSegment }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/segments/${segmentId}/split`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// -------------------------------------------------------------
// Workspace: Interviews Listing, Filtering, and Drafts
// -------------------------------------------------------------
export async function listInterviews(params?: {
  limit?: number;
  offset?: number;
  search?: string;
  role?: string;
  status?: string;
  from_date?: string;
  to_date?: string;
}): Promise<PaginatedInterviews> {
  const query = new URLSearchParams();
  if (params?.limit !== undefined) query.set('limit', params.limit.toString());
  if (params?.offset !== undefined) query.set('offset', params.offset.toString());
  if (params?.search) query.set('search', params.search);
  if (params?.role) query.set('role', params.role);
  if (params?.status) query.set('status', params.status);
  if (params?.from_date) query.set('from_date', params.from_date);
  if (params?.to_date) query.set('to_date', params.to_date);

  const qs = query.toString();
  const url = qs ? `${API_BASE}/interviews?${qs}` : `${API_BASE}/interviews`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`List interviews error: ${res.statusText}`);
  return res.json();
}

export async function updateInterviewDraft(
  id: string,
  data: {
    title?: string;
    candidate_name?: string;
    role?: string;
    plan?: InterviewPlan;
    template_id?: string;
    template_version?: number;
    capture_mode?: CaptureMode;
  }
): Promise<any> {
  const res = await fetch(`${API_BASE}/interviews/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getInterviewPlanReadiness(
  interviewId: string
): Promise<{ is_ready: boolean; errors: string[] }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/plan/readiness`);
  if (!res.ok) throw new Error(`Plan readiness check failed: ${res.statusText}`);
  return res.json();
}

export async function reopenInterviewRevision(
  interviewId: string,
  reviewerId: string,
  reason: string
): Promise<{ status: string; interview_id: string; reopened_at: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/revisions/reopen`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reviewer_id: reviewerId, reason }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getReportRevisions(
  interviewId: string
): Promise<ReportRevisionSummary[]> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/revisions/reports`);
  if (!res.ok) throw new Error(`Get report revisions failed: ${res.statusText}`);
  return res.json();
}

// -------------------------------------------------------------
// Workspace: Job Templates API
// -------------------------------------------------------------
export async function listJobTemplates(includeArchived = false): Promise<JobTemplate[]> {
  const url = includeArchived
    ? `${API_BASE}/job-templates?include_archived=true`
    : `${API_BASE}/job-templates`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`List job templates error: ${res.statusText}`);
  return res.json();
}

export async function getJobTemplate(templateId: string): Promise<JobTemplate> {
  const res = await fetch(`${API_BASE}/job-templates/${templateId}`);
  if (!res.ok) throw new Error(`Get job template error: ${res.statusText}`);
  return res.json();
}

export async function createJobTemplate(data: {
  id?: string;
  title: string;
  role: string;
  level?: string;
  description?: string;
  questions?: any[];
}): Promise<JobTemplate> {
  const res = await fetch(`${API_BASE}/job-templates`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function updateJobTemplate(
  templateId: string,
  data: {
    title?: string;
    role?: string;
    level?: string;
    description?: string;
    questions?: any[];
  }
): Promise<JobTemplate> {
  const res = await fetch(`${API_BASE}/job-templates/${templateId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function duplicateJobTemplate(
  templateId: string,
  titleSuffix = ' (Копия)'
): Promise<JobTemplate> {
  const res = await fetch(`${API_BASE}/job-templates/${templateId}/duplicate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title_suffix: titleSuffix }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function archiveJobTemplate(templateId: string): Promise<JobTemplate> {
  const res = await fetch(`${API_BASE}/job-templates/${templateId}/archive`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function unarchiveJobTemplate(templateId: string): Promise<JobTemplate> {
  const res = await fetch(`${API_BASE}/job-templates/${templateId}/unarchive`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function deleteJobTemplate(
  templateId: string
): Promise<{ status: string; template_id: string }> {
  const res = await fetch(`${API_BASE}/job-templates/${templateId}`, {
    method: 'DELETE',
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const data = await res.json();
      msg = data.detail || msg;
    } catch {
      msg = await res.text();
    }
    throw new Error(msg);
  }
  return res.json();
}

export async function copyQuestionToTemplate(
  sourceTemplateId: string,
  questionId: string,
  targetTemplateId: string
): Promise<JobTemplate> {
  const res = await fetch(
    `${API_BASE}/job-templates/${sourceTemplateId}/questions/${questionId}/copy-to/${targetTemplateId}`,
    {
      method: 'POST',
    }
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// -------------------------------------------------------------
// Adaptive Follow-up Questions API
// -------------------------------------------------------------
export async function getFollowUpsState(
  interviewId: string,
  questionId: string,
  mode?: FollowUpMode
): Promise<FollowUpsStateResponse> {
  const url = mode
    ? `${API_BASE}/interviews/${interviewId}/followups?question_id=${encodeURIComponent(questionId)}&mode=${encodeURIComponent(mode)}`
    : `${API_BASE}/interviews/${interviewId}/followups?question_id=${encodeURIComponent(questionId)}`;
  const res = await fetch(url);
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errBody.detail || `Get follow-ups state error: ${res.statusText}`);
  }
  return res.json();
}

export async function generateFollowUps(
  interviewId: string,
  data: GenerateFollowUpsRequest
): Promise<{
  status: string;
  request_id?: string;
  job_id?: string;
  mode?: string;
  is_cached?: boolean;
  wait_reason?: string;
  cooldown_remaining_sec?: number;
  can_generate?: boolean;
}> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/followups/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errBody.detail || `Generate follow-ups error: ${res.statusText}`);
  }
  return res.json();
}

export async function patchFollowUpSuggestion(
  interviewId: string,
  suggestionId: string,
  data: PatchFollowUpSuggestionRequest
): Promise<FollowUpSuggestion> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/followups/${suggestionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    if (res.status === 409) {
      const errBody = await res.json().catch(() => ({ detail: 'Конфликт версии решения карточки' }));
      throw new Error(`409: ${errBody.detail || 'Конфликт версии решения'}`);
    }
    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errBody.detail || `Patch suggestion error: ${res.statusText}`);
  }
  return res.json();
}

export async function retryFollowUpRequest(
  interviewId: string,
  requestId: string
): Promise<{ status: string; request_id: string; job_id: string }> {
  const res = await fetch(`${API_BASE}/interviews/${interviewId}/followups/requests/${requestId}/retry`, {
    method: 'POST',
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errBody.detail || `Retry follow-up request error: ${res.statusText}`);
  }
  return res.json();
}

// -------------------------------------------------------------
// AI Provider & Model Settings API
// -------------------------------------------------------------

export async function getAiSettings(): Promise<AiSettingsResponse> {
  const res = await fetch(`${API_BASE}/settings/ai`);
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errBody.detail || `Get AI settings error: ${res.statusText}`);
  }
  return res.json();
}

export async function updateAiSettings(data: UpdateAiSettingsRequest): Promise<AiSettingsResponse> {
  const res = await fetch(`${API_BASE}/settings/ai`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = typeof errBody.detail === 'string' ? errBody.detail : JSON.stringify(errBody.detail);
    if (res.status === 409) {
      throw new Error(`409: ${detail || 'Конфликт ревизии или активный пайплайн'}`);
    }
    if (res.status === 422) {
      throw new Error(`422: ${detail || 'Ошибка валидации параметров'}`);
    }
    throw new Error(detail || `Update AI settings error: ${res.statusText}`);
  }
  return res.json();
}

export async function testAiSettings(data: TestAiSettingsRequest): Promise<TestAiSettingsResponse> {
  const res = await fetch(`${API_BASE}/settings/ai/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const errBody = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = typeof errBody.detail === 'string' ? errBody.detail : JSON.stringify(errBody.detail);
    throw new Error(detail || `Test AI settings error: ${res.statusText}`);
  }
  return res.json();
}
