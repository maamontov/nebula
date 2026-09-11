export type TrackType = 'interviewer' | 'candidate' | 'shared';

export type CaptureMode = 'single_source' | 'dual_source';

export type SpeakerRole = 'candidate' | 'interviewer' | 'unknown';

export type InterviewStatus =
  | 'draft'
  | 'ready'
  | 'recording'
  | 'paused'
  | 'processing'
  | 'review'
  | 'finalized'
  | 'deleted';

export interface AudioDevice {
  id: string;
  name: string;
  is_default: boolean;
  channels: number;
  sample_rate: number;
}

export interface AudioLevels {
  interviewer_rms: number;
  interviewer_peak: number;
  candidate_rms: number;
  candidate_peak: number;
  shared_rms?: number;
  shared_peak?: number;
}

export interface TrackManifest {
  interview_id: string;
  track_id: TrackType | string;
  capture_epoch: number;
  total_chunks: number;
  total_duration_ms: number;
  is_sealed: boolean;
  gaps?: Array<{
    track_id: TrackType | string;
    start_time_ms: number;
    end_time_ms: number;
    reason: string;
  }>;
  total_samples?: number;
  dropped_samples?: number;
}

export interface RubricCriterion {
  id: string;
  title: string;
  description: string;
  min_score: number;
  max_score: number;
  weight: number;
  levels_description?: Record<number, string>;
}

export interface PlannedQuestion {
  id: string;
  title?: string;
  prompt?: string;
  text?: string;
  order_index?: number;
  weight: number;
  criteria: RubricCriterion[];
}

export interface InterviewPlan {
  id: string;
  title: string;
  role: string;
  candidate_name?: string;
  questions: PlannedQuestion[];
}

export interface TranscriptSegment {
  id: string;
  track_id: TrackType;
  start_time_ms: number;
  end_time_ms: number;
  text: string;
  is_final: boolean;
  speaker_role?: SpeakerRole;
  parent_segment_id?: string | null;
}

export interface EvidenceQuote {
  segment_id: string;
  exact_quote: string;
}

export interface CriterionScore {
  criterion_id: string;
  score: number;
  explanation: string;
  evidence: EvidenceQuote[];
}

export interface AssessmentProposal {
  id: string;
  interview_id: string;
  question_id: string;
  model_profile_id: string;
  scores: CriterionScore[];
  critical_errors: string[];
  is_approved: boolean;
  is_rejected?: boolean;
  validation_errors?: string[];
  provider_id?: string;
  fallback_metadata?: Record<string, any>;
  reviewed_scores?: CriterionScore[];
  reviewer_notes?: string;
  created_at: string;
}

export interface HumanAssessment {
  id: string;
  interview_id: string;
  question_id: string;
  rubric_revision_id: string;
  transcript_revision_id: string;
  reviewer_id?: string;
  scores: Array<{
    criterion_id: string;
    score: number;
    explanation?: string;
  }>;
  reviewer_notes?: string;
  is_manually_adjusted: boolean;
  is_stale: boolean;
  stale_reason?: string;
  is_excluded?: boolean;
  exclusion_reason?: string;
  created_at: string;
  updated_at: string;
}

export interface InterviewDetails {
  id: string;
  title: string;
  candidate_name: string;
  role: string;
  status: InterviewStatus;
  capture_mode?: CaptureMode;
  expected_tracks?: string[];
  active_rubric_revision_id?: string;
  active_transcript_revision_id?: string;
  consent_confirmed_at?: string;
  consent_version?: string;
  template_id?: string;
  template_version?: number;
  finalized_checksum?: string;
  created_at?: string;
  updated_at?: string;
}

export interface JobTemplate {
  id: string;
  title: string;
  role: string;
  level: string;
  description: string;
  questions: PlannedQuestion[];
  version: number;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface InterviewListItem {
  id: string;
  title: string;
  candidate_name: string;
  role: string;
  status: InterviewStatus;
  created_at: string;
  updated_at: string;
  template_id?: string;
  template_version?: number;
  final_score_100?: number | null;
  coverage_percentage?: number | null;
  hiring_recommendation?: string | null;
  is_reopened?: boolean;
  latest_report_revision?: number | null;
  last_finalized_score?: number | null;
  last_finalized_recommendation?: string | null;
  last_finalized_revision?: number | null;
}

export interface PaginatedInterviews {
  items: InterviewListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface ReportRevisionSummary {
  id: string;
  interview_id: string;
  revision_number: number;
  final_score_100: number | null;
  coverage_percentage: number;
  question_scores: Record<string, number | null>;
  summary_markdown: string;
  hiring_recommendation?: string;
  confirmed_by?: string;
  sha256_checksum: string;
  canonical_snapshot?: Record<string, any>;
  created_at: string;
  is_current?: boolean;
  reopen_reason?: string;
}
