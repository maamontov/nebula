use std::time::Instant;

#[derive(Debug, Clone)]
pub struct MonotonicInterviewClock {
    epoch: u32,
    start_instant: Instant,
}

impl MonotonicInterviewClock {
    pub fn new(epoch: u32) -> Self {
        Self {
            epoch,
            start_instant: Instant::now(),
        }
    }

    pub fn epoch(&self) -> u32 {
        self.epoch
    }

    pub fn elapsed_ms(&self) -> u64 {
        self.start_instant.elapsed().as_millis() as u64
    }

    /// Converts a sample count into duration in milliseconds.
    pub fn samples_to_ms(samples: u64, sample_rate: u32) -> u64 {
        if sample_rate == 0 {
            return 0;
        }
        (samples * 1000) / sample_rate as u64
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
