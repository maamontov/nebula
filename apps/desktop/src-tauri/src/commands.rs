use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use serde::{Deserialize, Serialize};
use cpal::traits::{DeviceTrait, HostTrait};
use audio_capture::{
    start_device_capture, AudioSpoolManager, CaptureHandle, CaptureStats, MonotonicInterviewClock,
    TrackManifest, TrackType,
};
use crate::uploader::{SessionUploader, UploadProgress};

pub struct ActiveSession {
    pub session_id: String,
    pub interviewer_handle: Option<CaptureHandle>,
    pub candidate_handle: Option<CaptureHandle>,
    pub clock: MonotonicInterviewClock,
    pub spool_dir: PathBuf,
    pub is_paused: Arc<AtomicBool>,
    pub uploader: Option<SessionUploader>,
    pub uploader_progress: Option<Arc<tokio::sync::Mutex<UploadProgress>>>,
}

pub struct AppState {
    pub active_session: Arc<Mutex<Option<ActiveSession>>>,
    pub is_recording: Arc<AtomicBool>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            active_session: Arc::new(Mutex::new(None)),
            is_recording: Arc::new(AtomicBool::new(false)),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeviceInfo {
    pub id: String,
    pub name: String,
    pub is_default: bool,
    pub channels: u16,
    pub sample_rate: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioLevels {
    pub interviewer_rms: f32,
    pub interviewer_peak: f32,
    pub candidate_rms: f32,
    pub candidate_peak: f32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionCaptureResult {
    pub status: String,
    pub session_id: String,
    pub total_chunks: u64,
    pub total_samples_interviewer: u64,
    pub total_samples_candidate: u64,
    pub total_duration_ms: u64,
    pub drift_ms: i64,
    pub skew_ms: i64,
    pub dropped_samples: u64,
    pub manifests: Vec<TrackManifest>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PauseResult {
    pub status: String,
    pub session_id: String,
    pub elapsed_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResumeResult {
    pub status: String,
    pub session_id: String,
    pub epoch: u32,
    pub elapsed_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActiveSessionInfo {
    pub is_recording: bool,
    pub is_paused: bool,
    pub session_id: Option<String>,
    pub elapsed_ms: u64,
    pub epoch: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SystemConfigInfo {
    pub capture_spool_dir: String,
    pub backend_url: String,
}

pub fn resolve_capture_spool_dir(spool_dir_arg: &str) -> PathBuf {
    if !spool_dir_arg.trim().is_empty() && spool_dir_arg != "./spool" {
        return PathBuf::from(spool_dir_arg);
    }
    if let Ok(val) = std::env::var("NEBULA_CAPTURE_SPOOL_DIR") {
        if !val.trim().is_empty() {
            return PathBuf::from(val);
        }
    }
    if let Ok(val) = std::env::var("NEBULA_DATA_DIR") {
        if !val.trim().is_empty() {
            return PathBuf::from(val).join("spool_capture");
        }
    }
    let repo_data = PathBuf::from("../../../data/spool_capture");
    if repo_data.parent().map(|p| p.exists()).unwrap_or(false) {
        return repo_data;
    }
    PathBuf::from("./data/spool_capture")
}

pub fn get_backend_url() -> String {
    std::env::var("NEBULA_BACKEND_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".to_string())
}

#[tauri::command]
pub fn get_system_config() -> Result<SystemConfigInfo, String> {
    let spool_dir = resolve_capture_spool_dir("");
    Ok(SystemConfigInfo {
        capture_spool_dir: spool_dir.to_string_lossy().to_string(),
        backend_url: get_backend_url(),
    })
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

    if state.is_recording.load(Ordering::SeqCst) {
        return Err("Запись уже активна. Остановите текущую сессию перед началом новой.".into());
    }

    if interviewer_dev_id == candidate_dev_id && !interviewer_dev_id.is_empty() {
        return Err("Для двухканального сценария требуется настроить отдельный вход кандидата (например, Loopback / BlackHole). Выбор одного и того же микрофона для обоих каналов запрещён.".into());
    }

    let host = cpal::default_host();
    let mut dev_inv: Option<cpal::Device> = None;
    let mut dev_cand: Option<cpal::Device> = None;

    if let Ok(devices) = host.input_devices() {
        for dev in devices {
            if let Ok(name) = dev.name() {
                if name == interviewer_dev_id {
                    dev_inv = Some(dev);
                } else if name == candidate_dev_id {
                    dev_cand = Some(dev);
                }
            }
        }
    }

    // No silent fallback to default input
    let dev_inv = dev_inv.ok_or_else(|| {
        format!("Устройство аудиовхода интервьюера '{}' не найдено или недоступно. Выберите устройство повторно.", interviewer_dev_id)
    })?;
    let dev_cand = dev_cand.ok_or_else(|| {
        format!("Устройство аудиовхода кандидата '{}' не найдено или недоступно. Выберите устройство повторно.", candidate_dev_id)
    })?;

    let spool_path = resolve_capture_spool_dir(&spool_dir);
    std::fs::create_dir_all(&spool_path)
        .map_err(|e| format!("Не удалось создать директорию capture spool {:?}: {}", spool_path, e))?;

    let spool_mgr = Arc::new(AudioSpoolManager::new(&spool_path));
    let mut clock = MonotonicInterviewClock::new(1);

    clock.mark_track_start(TrackType::Interviewer);

    // 1. Start interviewer track
    let inv_handle = start_device_capture(
        &dev_inv,
        session_id.clone(),
        TrackType::Interviewer,
        spool_mgr.clone(),
        clock.clone(),
        1000,
    ).map_err(|e| format!("Failed to open interviewer capture stream: {}", e))?;

    // 2. Start candidate track. If failed, cleanly stop interviewer track immediately!
    clock.mark_track_start(TrackType::Candidate);
    let cand_handle = match start_device_capture(
        &dev_cand,
        session_id.clone(),
        TrackType::Candidate,
        spool_mgr.clone(),
        clock.clone(),
        1000,
    ) {
        Ok(h) => h,
        Err(e) => {
            let _ = inv_handle.stop();
            state.is_recording.store(false, Ordering::SeqCst);
            return Err(format!("Failed to open candidate capture stream (interviewer stream cleanly aborted): {}", e));
        }
    };

    // 3. Start durable chunk uploader
    let mut uploader = SessionUploader::new(session_id.clone(), spool_path.clone(), get_backend_url());
    let uploader_progress = uploader.progress_handle();
    uploader.start();

    let session = ActiveSession {
        session_id: session_id.clone(),
        interviewer_handle: Some(inv_handle),
        candidate_handle: Some(cand_handle),
        clock,
        spool_dir: spool_path,
        is_paused: Arc::new(AtomicBool::new(false)),
        uploader: Some(uploader),
        uploader_progress: Some(uploader_progress),
    };

    if let Ok(mut guard) = state.active_session.lock() {
        *guard = Some(session);
    }
    state.is_recording.store(true, Ordering::SeqCst);

    println!("[Nebula Tauri] 2-Track Capture and Uploader started successfully for session: {}", session_id);
    Ok("Capture started successfully".into())
}

#[tauri::command]
pub async fn stop_capture(state: tauri::State<'_, AppState>) -> Result<SessionCaptureResult, String> {
    state.is_recording.store(false, Ordering::SeqCst);

    let session = {
        let mut guard = state.active_session.lock().map_err(|_| "Failed to lock active session state")?;
        guard.take()
    };

    let mut session = match session {
        Some(s) => s,
        None => {
            return Ok(SessionCaptureResult {
                status: "stopped".into(),
                session_id: "".into(),
                total_chunks: 0,
                total_samples_interviewer: 0,
                total_samples_candidate: 0,
                total_duration_ms: 0,
                drift_ms: 0,
                skew_ms: 0,
                dropped_samples: 0,
                manifests: Vec::new(),
            });
        }
    };

    println!("[Nebula Tauri] Stopping capture for session: {}", session.session_id);

    let mut stats_inv: Option<CaptureStats> = None;
    let mut stats_cand: Option<CaptureStats> = None;

    if let Some(inv_handle) = session.interviewer_handle.take() {
        stats_inv = Some(inv_handle.stop().map_err(|e| format!("Interviewer stop failed: {}", e))?);
    }
    if let Some(cand_handle) = session.candidate_handle.take() {
        stats_cand = Some(cand_handle.stop().map_err(|e| format!("Candidate stop failed: {}", e))?);
    }

    // Stop uploader with final drain
    if let Some(mut uploader) = session.uploader.take() {
        uploader.stop().await;
    }

    let inv = stats_inv.unwrap_or_else(|| CaptureStats {
        interview_id: session.session_id.clone(),
        track_id: TrackType::Interviewer,
        total_chunks: 0,
        total_samples: 0,
        total_duration_ms: 0,
        dropped_samples: 0,
        is_sealed: true,
        gaps: Vec::new(),
    });

    let cand = stats_cand.unwrap_or_else(|| CaptureStats {
        interview_id: session.session_id.clone(),
        track_id: TrackType::Candidate,
        total_chunks: 0,
        total_samples: 0,
        total_duration_ms: 0,
        dropped_samples: 0,
        is_sealed: true,
        gaps: Vec::new(),
    });

    let total_chunks = inv.total_chunks + cand.total_chunks;
    let total_duration_ms = inv.total_duration_ms.max(cand.total_duration_ms);
    let dropped_samples = inv.dropped_samples + cand.dropped_samples;

    let skew_ms = session.clock.calculate_timeline_skew_ms(
        inv.total_samples,
        16000,
        cand.total_samples,
        16000,
    );

    let drift_ms = MonotonicInterviewClock::calculate_drift_ms(
        total_duration_ms,
        inv.total_samples,
        16000,
    );

    // Load actual sealed manifests from spool
    let spool_mgr = AudioSpoolManager::new(&session.spool_dir);
    let mut manifests = Vec::new();
    if let Ok(m) = spool_mgr.load_manifest(&session.session_id, TrackType::Interviewer) {
        manifests.push(m);
    }
    if let Ok(m) = spool_mgr.load_manifest(&session.session_id, TrackType::Candidate) {
        manifests.push(m);
    }

    Ok(SessionCaptureResult {
        status: "stopped".into(),
        session_id: session.session_id,
        total_chunks,
        total_samples_interviewer: inv.total_samples,
        total_samples_candidate: cand.total_samples,
        total_duration_ms,
        drift_ms,
        skew_ms,
        dropped_samples,
        manifests,
    })
}

#[tauri::command]
pub fn get_audio_levels(state: tauri::State<AppState>) -> Result<AudioLevels, String> {
    let guard = state.active_session.lock().map_err(|_| "Failed to lock active session state")?;
    if let Some(ref session) = *guard {
        let (interviewer_rms, interviewer_peak) = session
            .interviewer_handle
            .as_ref()
            .map(|h| h.audio_levels())
            .unwrap_or((0.0, 0.0));
        let (candidate_rms, candidate_peak) = session
            .candidate_handle
            .as_ref()
            .map(|h| h.audio_levels())
            .unwrap_or((0.0, 0.0));
        return Ok(AudioLevels {
            interviewer_rms,
            interviewer_peak,
            candidate_rms,
            candidate_peak,
        });
    }

    Ok(AudioLevels {
        interviewer_rms: 0.0,
        interviewer_peak: 0.0,
        candidate_rms: 0.0,
        candidate_peak: 0.0,
    })
}

#[tauri::command]
pub async fn get_upload_progress(
    session_id: String,
    state: tauri::State<'_, AppState>,
) -> Result<UploadProgress, String> {
    let progress_arc = {
        let guard = state.active_session.lock().map_err(|_| "Failed to lock active session state")?;
        if let Some(ref s) = *guard {
            if s.session_id == session_id {
                s.uploader_progress.clone()
            } else {
                None
            }
        } else {
            None
        }
    };

    if let Some(arc) = progress_arc {
        let p = arc.lock().await;
        return Ok(p.clone());
    }

    // If not actively recording, count files on disk in capture spool
    let spool_dir = resolve_capture_spool_dir("");
    let session_dir = spool_dir.join(&session_id);
    let mut total_discovered = 0u64;
    let mut total_acked = 0u64;
    let mut total_failed = 0u64;

    for track in &["candidate", "interviewer"] {
        let td = session_dir.join(track);
        if let Ok(entries) = std::fs::read_dir(td) {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                if name.ends_with(".chunk") {
                    total_discovered += 1;
                } else if name.ends_with(".ack") {
                    total_acked += 1;
                } else if name.ends_with(".err") {
                    total_failed += 1;
                }
            }
        }
    }

    Ok(UploadProgress {
        session_id,
        total_discovered,
        total_acked,
        total_failed,
        in_flight: 0,
        is_active: false,
        last_error: None,
    })
}

#[tauri::command]
pub fn get_active_session(state: tauri::State<AppState>) -> Result<ActiveSessionInfo, String> {
    let is_rec = state.is_recording.load(Ordering::SeqCst);
    let guard = state.active_session.lock().map_err(|_| "Failed to lock active session state")?;
    if let Some(ref session) = *guard {
        let is_paused = session.is_paused.load(Ordering::SeqCst);
        let elapsed_ms = session.clock.elapsed_ms();
        let epoch = session.clock.epoch();
        return Ok(ActiveSessionInfo {
            is_recording: is_rec,
            is_paused,
            session_id: Some(session.session_id.clone()),
            elapsed_ms,
            epoch,
        });
    }

    Ok(ActiveSessionInfo {
        is_recording: false,
        is_paused: false,
        session_id: None,
        elapsed_ms: 0,
        epoch: 0,
    })
}

#[tauri::command]
pub fn verify_spool(spool_dir: String, interview_id: String, track: String) -> Result<bool, String> {
    let track_type = if track == "candidate" {
        TrackType::Candidate
    } else {
        TrackType::Interviewer
    };

    let spool_path = resolve_capture_spool_dir(&spool_dir);
    let spool = AudioSpoolManager::new(&spool_path);
    let report = spool.verify_track_spool(&interview_id, track_type).map_err(|e| e.to_string())?;
    Ok(report.is_valid)
}

#[tauri::command]
pub fn pause_capture(state: tauri::State<AppState>) -> Result<PauseResult, String> {
    let mut guard = state.active_session.lock().map_err(|_| "Failed to lock active session state")?;
    let session = guard.as_mut().ok_or_else(|| "No active recording session to pause".to_string())?;

    if session.is_paused.load(Ordering::SeqCst) {
        return Ok(PauseResult {
            status: "already_paused".into(),
            session_id: session.session_id.clone(),
            elapsed_ms: session.clock.elapsed_ms(),
        });
    }

    if let Some(ref inv) = session.interviewer_handle {
        inv.pause().map_err(|e| format!("Failed to pause interviewer stream: {}", e))?;
    }
    if let Some(ref cand) = session.candidate_handle {
        cand.pause().map_err(|e| format!("Failed to pause candidate stream: {}", e))?;
    }

    let elapsed_ms = session.clock.pause();
    session.is_paused.store(true, Ordering::SeqCst);

    println!("[Nebula Tauri] Session {} paused at {} ms", session.session_id, elapsed_ms);

    Ok(PauseResult {
        status: "paused".into(),
        session_id: session.session_id.clone(),
        elapsed_ms,
    })
}

#[tauri::command]
pub fn resume_capture(state: tauri::State<AppState>) -> Result<ResumeResult, String> {
    let mut guard = state.active_session.lock().map_err(|_| "Failed to lock active session state")?;
    let session = guard.as_mut().ok_or_else(|| "No active recording session to resume".to_string())?;

    if !session.is_paused.load(Ordering::SeqCst) {
        return Ok(ResumeResult {
            status: "not_paused".into(),
            session_id: session.session_id.clone(),
            epoch: session.clock.epoch(),
            elapsed_ms: session.clock.elapsed_ms(),
        });
    }

    let epoch = session.clock.resume();

    if let Some(ref inv) = session.interviewer_handle {
        inv.resume().map_err(|e| format!("Failed to resume interviewer stream: {}", e))?;
    }
    if let Some(ref cand) = session.candidate_handle {
        cand.resume().map_err(|e| format!("Failed to resume candidate stream: {}", e))?;
    }

    session.is_paused.store(false, Ordering::SeqCst);
    let elapsed_ms = session.clock.elapsed_ms();

    println!("[Nebula Tauri] Session {} resumed at epoch {}, elapsed {} ms", session.session_id, epoch, elapsed_ms);

    Ok(ResumeResult {
        status: "resumed".into(),
        session_id: session.session_id.clone(),
        epoch,
        elapsed_ms,
    })
}
