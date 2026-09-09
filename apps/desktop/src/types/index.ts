export type TrackType = 'interviewer' | 'candidate';

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
}

export interface RubricCriterion {
  id: string;
  title: string;
  description: string;
  min_score: number;
  max_score: number;
  weight: number;
}

export interface PlannedQuestion {
  id: string;
  text: string;
  order_index: number;
  weight: number;
  criteria: RubricCriterion[];
}

export interface InterviewPlan {
  id: string;
  title: string;
  role: string;
  questions: PlannedQuestion[];
}

export interface TranscriptSegment {
  id: string;
  track_id: TrackType;
  start_time_ms: number;
  end_time_ms: number;
  text: string;
  is_final: boolean;
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
  reviewed_scores?: CriterionScore[];
  reviewer_notes?: string;
  created_at: string;
}

export interface InterviewDetails {
  id: string;
  title: string;
  candidate_name: string;
  role: string;
  status: InterviewStatus;
  consent_confirmed_at?: string;
  consent_version?: string;
}
