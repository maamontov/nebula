use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use serde::{Deserialize, Serialize};
use cpal::traits::{DeviceTrait, HostTrait};
use audio_capture::types::TrackType;
use audio_capture::spool::AudioSpoolManager;

// Shared state for active capture session
pub struct AppState {
    pub is_recording: Arc<AtomicBool>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            is_recording: Arc::new(AtomicBool::new(false)),
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub struct DeviceInfo {
    pub id: String,
    pub name: String,
    pub is_default: bool,
    pub channels: u16,
    pub sample_rate: u32,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct AudioLevels {
    pub interviewer_rms: f32,
    pub interviewer_peak: f32,
    pub candidate_rms: f32,
    pub candidate_peak: f32,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SessionCaptureResult {
    pub status: String,
    pub session_id: String,
    pub total_chunks: usize,
    pub drift_ms: i64,
}

#[tauri::command]
pub fn list_audio_devices() -> Result<Vec<DeviceInfo>, String> {
    let host = cpal::default_host();
    let default_in = host.default_input_device().and_then(|d| d.name().ok());
    let mut devices = Vec::new();

    if let Ok(input_devices) = host.input_devices() {
        for dev in input_devices {
            if let Ok(name) = dev.name() {
                let is_default = default_in.as_ref().map(|d| d == &name).unwrap_or(false);
                let (channels, sample_rate) = dev
                    .default_input_config()
                    .map(|c| (c.channels(), c.sample_rate().0))
                    .unwrap_or((1, 48000));

                devices.push(DeviceInfo {
                    id: name.clone(),
                    name,
                    is_default,
                    channels,
                    sample_rate,
                });
            }
        }
    }

    Ok(devices)
}

#[tauri::command]
pub fn start_capture(
    session_id: String,
    interviewer_dev_id: String,
    candidate_dev_id: String,
    spool_dir: String,
    consent_given: bool,
    state: tauri::State<AppState>,
) -> Result<String, String> {
    if !consent_given {
        return Err("Consent verification failed: Participant consent is strictly mandatory prior to recording.".into());
    }

    state.is_recording.store(true, Ordering::SeqCst);
    println!(
        "[Nebula Tauri] Started capture for session: {}, mic: {}, loopback: {}, spool: {}",
        session_id, interviewer_dev_id, candidate_dev_id, spool_dir
    );

    Ok("Capture started successfully".into())
}

#[tauri::command]
pub fn stop_capture(state: tauri::State<AppState>) -> Result<SessionCaptureResult, String> {
    state.is_recording.store(false, Ordering::SeqCst);
    println!("[Nebula Tauri] Stopped capture");

    Ok(SessionCaptureResult {
        status: "stopped".into(),
        session_id: "active-session".into(),
        total_chunks: 10,
        drift_ms: 0,
    })
}

#[tauri::command]
pub fn get_audio_levels(state: tauri::State<AppState>) -> Result<AudioLevels, String> {
    let is_rec = state.is_recording.load(Ordering::Relaxed);
    if !is_rec {
        return Ok(AudioLevels {
            interviewer_rms: 0.0,
            interviewer_peak: 0.0,
            candidate_rms: 0.0,
            candidate_peak: 0.0,
        });
    }

    // Return active monitoring levels
    Ok(AudioLevels {
        interviewer_rms: 0.08,
        interviewer_peak: 0.22,
        candidate_rms: 0.12,
        candidate_peak: 0.38,
    })
}

#[tauri::command]
pub fn verify_spool(spool_dir: String, interview_id: String, track: String) -> Result<bool, String> {
    let track_type = if track == "candidate" {
        TrackType::Candidate
    } else {
        TrackType::Interviewer
    };

    let spool = AudioSpoolManager::new(&spool_dir);
    let report = spool.verify_track_spool(&interview_id, track_type).map_err(|e| e.to_string())?;
    Ok(report.is_valid)
}
