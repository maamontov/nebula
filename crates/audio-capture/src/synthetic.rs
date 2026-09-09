use std::f32::consts::PI;
use std::sync::Arc;
use anyhow::Result;

use crate::clock::MonotonicInterviewClock;
use crate::resampler::f32_to_pcm_s16le;
use crate::spool::AudioSpoolManager;
use crate::types::{AudioFormat, TrackType};

#[derive(Debug, Clone)]
pub struct SyntheticTrackConfig {
    pub track_id: TrackType,
    pub frequency_hz: f32,
    pub sample_rate: u32,
    pub drift_ppm: f64, // Parts-per-million drift, e.g. +50.0 means clock runs 50 microseconds faster per sec
    pub simulated_dropout_at_ms: Option<u64>,
    pub dropout_duration_ms: u64,
}

pub struct SimulationResult {
    pub total_chunks: u64,
    pub total_samples: u64,
    pub simulated_duration_ms: u64,
    pub calculated_drift_ms: i64,
}

/// Fast-forward simulation of an audio track generating chunks to spool.
/// Simulates hours of recording in milliseconds for automated CI/benchmarks.
pub fn simulate_track_recording(
    interview_id: &str,
    config: &SyntheticTrackConfig,
    spool: Arc<AudioSpoolManager>,
    duration_sec: u64,
    chunk_duration_ms: u64,
) -> Result<SimulationResult> {
    let total_duration_ms = duration_sec * 1000;
    let target_sample_rate = 16000u32;
    let base_samples_per_chunk = ((target_sample_rate as u64 * chunk_duration_ms) / 1000) as usize;

    let total_chunks = total_duration_ms / chunk_duration_ms;
    let mut current_sample_idx = 0u64;
    let mut total_samples = 0u64;

    for seq in 0..total_chunks {
        let start_time_ms = seq * chunk_duration_ms;
        let end_time_ms = start_time_ms + chunk_duration_ms;

        // Check for simulated dropout
        if let Some(drop_start) = config.simulated_dropout_at_ms {
            if start_time_ms >= drop_start && start_time_ms < drop_start + config.dropout_duration_ms {
                // Drop this chunk completely to simulate network or driver failure
                continue;
            }
        }

        // Apply drift: slightly adjust the number of samples in this chunk
        let drift_factor = 1.0 + (config.drift_ppm / 1_000_000.0);
        let chunk_sample_count = ((base_samples_per_chunk as f64) * drift_factor).round() as usize;

        let mut samples = Vec::with_capacity(chunk_sample_count);
        for _ in 0..chunk_sample_count {
            let t = current_sample_idx as f32 / target_sample_rate as f32;
            let val = (2.0 * PI * config.frequency_hz * t).sin() * 0.5;
            samples.push(val);
            current_sample_idx += 1;
        }

        let pcm_bytes = f32_to_pcm_s16le(&samples);
        total_samples += chunk_sample_count as u64;

        spool.write_chunk(
            interview_id,
            config.track_id,
            0, // epoch
            seq,
            start_time_ms,
            end_time_ms,
            target_sample_rate,
            1, // mono
            chunk_sample_count as u64,
            AudioFormat::PcmS16Le,
            &pcm_bytes,
        )?;
    }

    spool.seal_manifest(
        interview_id,
        config.track_id,
        0,
        total_chunks,
        total_duration_ms,
    )?;

    let calculated_drift_ms = MonotonicInterviewClock::calculate_drift_ms(
        total_duration_ms,
        total_samples,
        target_sample_rate,
    );

    Ok(SimulationResult {
        total_chunks,
        total_samples,
        simulated_duration_ms: total_duration_ms,
        calculated_drift_ms,
    })
}
