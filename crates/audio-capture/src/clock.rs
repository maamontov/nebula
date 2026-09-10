use std::time::Instant;
use crate::types::TrackType;

#[derive(Debug, Clone)]
pub struct MonotonicInterviewClock {
    epoch: u32,
    accumulated_active_ms: u64,
    last_resume_instant: Option<Instant>,
    interviewer_offset_ms: Option<u64>,
    candidate_offset_ms: Option<u64>,
    shared_offset_ms: Option<u64>,
}

impl MonotonicInterviewClock {
    pub fn new(epoch: u32) -> Self {
        let now = Instant::now();
        Self {
            epoch,
            accumulated_active_ms: 0,
            last_resume_instant: Some(now),
            interviewer_offset_ms: None,
            candidate_offset_ms: None,
            shared_offset_ms: None,
        }
    }

    pub fn epoch(&self) -> u32 {
        self.epoch
    }

    pub fn is_paused(&self) -> bool {
        self.last_resume_instant.is_none()
    }

    pub fn pause(&mut self) -> u64 {
        if let Some(resumed) = self.last_resume_instant.take() {
            self.accumulated_active_ms += resumed.elapsed().as_millis() as u64;
        }
        self.accumulated_active_ms
    }

    pub fn resume(&mut self) -> u32 {
        if self.last_resume_instant.is_none() {
            self.last_resume_instant = Some(Instant::now());
            self.epoch += 1;
        }
        self.epoch
    }

    pub fn elapsed_ms(&self) -> u64 {
        match self.last_resume_instant {
            Some(resumed) => self.accumulated_active_ms + (resumed.elapsed().as_millis() as u64),
            None => self.accumulated_active_ms,
        }
    }

    pub fn set_track_start_offset_ms(&mut self, track: TrackType, offset_ms: u64) {
        match track {
            TrackType::Interviewer => self.interviewer_offset_ms = Some(offset_ms),
            TrackType::Candidate => self.candidate_offset_ms = Some(offset_ms),
            TrackType::Shared => self.shared_offset_ms = Some(offset_ms),
        }
    }

    pub fn mark_track_start(&mut self, track: TrackType) -> u64 {
        let elapsed = self.elapsed_ms();
        self.set_track_start_offset_ms(track, elapsed);
        elapsed
    }

    pub fn track_start_offset_ms(&self, track: TrackType) -> Option<u64> {
        match track {
            TrackType::Interviewer => self.interviewer_offset_ms,
            TrackType::Candidate => self.candidate_offset_ms,
            TrackType::Shared => self.shared_offset_ms,
        }
    }

    pub fn initial_skew_ms(&self) -> Option<i64> {
        match (self.interviewer_offset_ms, self.candidate_offset_ms) {
            (Some(inv), Some(cand)) => Some(inv as i64 - cand as i64),
            _ => None,
        }
    }

    /// Converts a sample count into duration in milliseconds.
    pub fn samples_to_ms(samples: u64, sample_rate: u32) -> u64 {
        if sample_rate == 0 {
            return 0;
        }
        (samples * 1000) / sample_rate as u64
    }

    /// Calculates absolute timeline (start_ms, end_ms) for a chunk within the interview timeline.
    pub fn calculate_chunk_timeline(
        &self,
        track: TrackType,
        sample_offset_within_track: u64,
        chunk_samples: u64,
        sample_rate: u32,
    ) -> (u64, u64) {
        let track_offset = self.track_start_offset_ms(track).unwrap_or(0);
        let start_time_ms = track_offset + Self::samples_to_ms(sample_offset_within_track, sample_rate);
        let end_time_ms = track_offset + Self::samples_to_ms(sample_offset_within_track + chunk_samples, sample_rate);
        (start_time_ms, end_time_ms)
    }

    /// Measures the drift (in milliseconds) between the wall-clock elapsed time
    /// and the theoretical time represented by the audio samples.
    /// Positive drift means audio clock runs faster than wall clock;
    /// Negative drift means audio clock lags behind wall clock.
    pub fn calculate_drift_ms(elapsed_wall_ms: u64, samples: u64, sample_rate: u32) -> i64 {
        let sample_time_ms = Self::samples_to_ms(samples, sample_rate) as i64;
        sample_time_ms - elapsed_wall_ms as i64
    }

    /// Measures skew between two audio tracks (in milliseconds).
    pub fn calculate_inter_track_skew_ms(
        samples_a: u64,
        sample_rate_a: u32,
        samples_b: u64,
        sample_rate_b: u32,
    ) -> i64 {
        let ms_a = Self::samples_to_ms(samples_a, sample_rate_a) as i64;
        let ms_b = Self::samples_to_ms(samples_b, sample_rate_b) as i64;
        ms_a - ms_b
    }

    /// Measures skew between interviewer and candidate taking their start offsets into account.
    pub fn calculate_timeline_skew_ms(
        &self,
        samples_interviewer: u64,
        rate_interviewer: u32,
        samples_candidate: u64,
        rate_candidate: u32,
    ) -> i64 {
        let inv_offset = self.interviewer_offset_ms.unwrap_or(0) as i64;
        let cand_offset = self.candidate_offset_ms.unwrap_or(0) as i64;
        let inv_ms = inv_offset + Self::samples_to_ms(samples_interviewer, rate_interviewer) as i64;
        let cand_ms = cand_offset + Self::samples_to_ms(samples_candidate, rate_candidate) as i64;
        inv_ms - cand_ms
    }
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct DriftMetrics {
    pub wall_clock_elapsed_ms: u64,
    pub interviewer_samples: u64,
    pub interviewer_drift_ms: i64,
    pub candidate_samples: u64,
    pub candidate_drift_ms: i64,
    pub inter_track_skew_ms: i64,
}

