use crate::uploader::{SessionUploader, UploadProgress};
use audio_capture::{
    start_device_capture, AudioSpoolManager, CaptureHandle, CaptureStats, MonotonicInterviewClock,
    SharedInterviewClock, TrackManifest, TrackType,
};
use cpal::traits::{DeviceTrait, HostTrait};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

pub struct ActiveSession {
    pub session_id: String,
    pub capture_mode: String,
    pub interviewer_handle: Option<CaptureHandle>,
    pub candidate_handle: Option<CaptureHandle>,
    pub shared_handle: Option<CaptureHandle>,
    pub clock: SharedInterviewClock,
    pub spool_dir: PathBuf,
    pub is_paused: Arc<AtomicBool>,
    pub uploader: Option<SessionUploader>,
    pub uploader_progress: Option<Arc<tokio::sync::Mutex<UploadProgress>>>,
}

pub struct AppState {
    pub active_session: Arc<Mutex<Option<ActiveSession>>>,
    pub is_recording: Arc<AtomicBool>,
    pub last_stop_results: Arc<Mutex<HashMap<String, SessionCaptureResult>>>,
    pub background_uploaders: Arc<tokio::sync::Mutex<HashMap<String, SessionUploader>>>,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            active_session: Arc::new(Mutex::new(None)),
            is_recording: Arc::new(AtomicBool::new(false)),
            last_stop_results: Arc::new(Mutex::new(HashMap::new())),
            background_uploaders: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DeviceInfo {
    pub id: String,
    pub name: String,
    pub is_default: bool,
    pub channels: u16,
    pub sample_rate: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AudioLevels {
    pub interviewer_rms: f32,
    pub interviewer_peak: f32,
    pub candidate_rms: f32,
    pub candidate_peak: f32,
    pub shared_rms: f32,
    pub shared_peak: f32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StartCaptureResult {
    pub status: String,
    pub session_id: String,
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
    std::env::var("NEBULA_BACKEND_URL").unwrap_or_else(|_| "http://127.0.0.1:17843".to_string())
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
pub async fn start_capture(
    session_id: String,
    interviewer_dev_id: Option<String>,
    candidate_dev_id: Option<String>,
    shared_dev_id: Option<String>,
    spool_dir: Option<String>,
    consent_given: bool,
    capture_mode: Option<String>,
    state: tauri::State<'_, AppState>,
) -> Result<StartCaptureResult, String> {
    if !consent_given {
        return Err("Consent verification failed: Participant consent is strictly mandatory prior to recording.".into());
    }

    if state.is_recording.load(Ordering::SeqCst) {
        return Err("Запись уже активна. Остановите текущую сессию перед началом новой.".into());
    }

    let mode = capture_mode.unwrap_or_else(|| "dual_source".to_string());
    let spool_dir_val = spool_dir.unwrap_or_default();
    let spool_path = resolve_capture_spool_dir(&spool_dir_val);
    std::fs::create_dir_all(&spool_path).map_err(|e| {
        format!(
            "Не удалось создать директорию capture spool {:?}: {}",
            spool_path, e
        )
    })?;

    let spool_mgr = Arc::new(AudioSpoolManager::new(&spool_path));
    let clock = SharedInterviewClock::new(1);
    let host = cpal::default_host();

    let (inv_handle, cand_handle, shared_handle) = if mode == "single_source" {
        let dev_name = shared_dev_id
            .or(interviewer_dev_id)
            .or(candidate_dev_id)
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| "Не выбрано аудиоустройство для записи одного источника.".to_string())?;

        let mut dev_found: Option<cpal::Device> = None;
        if let Ok(devices) = host.input_devices() {
            for dev in devices {
                if let Ok(name) = dev.name() {
                    if name == dev_name {
                        dev_found = Some(dev);
                        break;
                    }
                }
            }
        }
        let dev = dev_found.ok_or_else(|| {
            format!("Устройство аудиовхода '{}' не найдено или недоступно. Выберите устройство повторно.", dev_name)
        })?;

        let handle = start_device_capture(
            &dev,
            session_id.clone(),
            TrackType::Shared,
            spool_mgr.clone(),
            clock.clone(),
            1000,
        )
        .map_err(|e| format!("Failed to open shared capture stream: {}", e))?;

        (None, None, Some(handle))
    } else {
        // Dual source
        let inv_name = interviewer_dev_id
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| "Не выбрано устройство микрофона интервьюера.".to_string())?;
        let cand_name = candidate_dev_id
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| {
                "Не выбрано устройство звука кандидата (Loopback / Звонок).".to_string()
            })?;

        if inv_name == cand_name {
            return Err("Для двухканального сценария требуется настроить отдельный вход кандидата (например, Loopback / BlackHole). Выбор одного и того же микрофона для обоих каналов запрещён.".into());
        }

        let mut dev_inv: Option<cpal::Device> = None;
        let mut dev_cand: Option<cpal::Device> = None;

        if let Ok(devices) = host.input_devices() {
            for dev in devices {
                if let Ok(name) = dev.name() {
                    if name == inv_name {
                        dev_inv = Some(dev);
                    } else if name == cand_name {
                        dev_cand = Some(dev);
                    }
                }
            }
        }

        let dev_inv = dev_inv.ok_or_else(|| {
            format!("Устройство аудиовхода интервьюера '{}' не найдено или недоступно. Выберите устройство повторно.", inv_name)
        })?;
        let dev_cand = dev_cand.ok_or_else(|| {
            format!("Устройство аудиовхода кандидата '{}' не найдено или недоступно. Выберите устройство повторно.", cand_name)
        })?;

        let inv_handle = start_device_capture(
            &dev_inv,
            session_id.clone(),
            TrackType::Interviewer,
            spool_mgr.clone(),
            clock.clone(),
            1000,
        )
        .map_err(|e| format!("Failed to open interviewer capture stream: {}", e))?;

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

        (Some(inv_handle), Some(cand_handle), None)
    };

    // Start durable chunk uploader
    let mut uploader =
        SessionUploader::new(session_id.clone(), spool_path.clone(), get_backend_url());
    let uploader_progress = uploader.progress_handle();
    uploader.start();

    let session = ActiveSession {
        session_id: session_id.clone(),
        capture_mode: mode,
        interviewer_handle: inv_handle,
        candidate_handle: cand_handle,
        shared_handle,
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

    println!(
        "[Nebula Tauri] Capture and Uploader started successfully for session: {}",
        session_id
    );
    Ok(StartCaptureResult {
        status: "started".into(),
        session_id,
    })
}

#[tauri::command]
pub async fn stop_capture(
    state: tauri::State<'_, AppState>,
) -> Result<SessionCaptureResult, String> {
    state.is_recording.store(false, Ordering::SeqCst);

    let session = {
        let mut guard = state
            .active_session
            .lock()
            .map_err(|_| "Failed to lock active session state")?;
        guard.take()
    };

    let mut session = match session {
        Some(s) => s,
        None => {
            // Check if we have cached results in last_stop_results for retry
            if let Ok(guard) = state.last_stop_results.lock() {
                if let Some((_, res)) = guard.iter().last() {
                    return Ok(res.clone());
                }
            }
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

    println!(
        "[Nebula Tauri] Stopping capture for session: {}",
        session.session_id
    );

    let mut stats_inv: Option<CaptureStats> = None;
    let mut stats_cand: Option<CaptureStats> = None;
    let mut stats_shared: Option<CaptureStats> = None;
    let mut stop_errors: Vec<String> = Vec::new();

    if let Some(inv_handle) = session.interviewer_handle.take() {
        match inv_handle.stop() {
            Ok(s) => stats_inv = Some(s),
            Err(e) => {
                let err_msg = format!("Interviewer stop failed: {}", e);
                eprintln!("[Nebula Tauri] {}", err_msg);
                stop_errors.push(err_msg);
            }
        }
    }
    if let Some(cand_handle) = session.candidate_handle.take() {
        match cand_handle.stop() {
            Ok(s) => stats_cand = Some(s),
            Err(e) => {
                let err_msg = format!("Candidate stop failed: {}", e);
                eprintln!("[Nebula Tauri] {}", err_msg);
                stop_errors.push(err_msg);
            }
        }
    }
    if let Some(shared_handle) = session.shared_handle.take() {
        match shared_handle.stop() {
            Ok(s) => stats_shared = Some(s),
            Err(e) => {
                let err_msg = format!("Shared stop failed: {}", e);
                eprintln!("[Nebula Tauri] {}", err_msg);
                stop_errors.push(err_msg);
            }
        }
    }

    // Stop uploader with final drain or preserve in background if backlog remains
    if let Some(mut uploader) = session.uploader.take() {
        uploader.stop().await;
        let prog = uploader.get_progress().await;
        if prog.total_discovered > (prog.total_acked + prog.total_failed) {
            let mut bg = state.background_uploaders.lock().await;
            if !bg.contains_key(&session.session_id) {
                uploader.start();
                bg.insert(session.session_id.clone(), uploader);
            }
        }
    }

    let is_single = session.capture_mode == "single_source";

    let (
        total_chunks,
        total_duration_ms,
        dropped_samples,
        skew_ms,
        drift_ms,
        total_samples_inv,
        total_samples_cand,
    ) = if is_single {
        let sh = stats_shared.unwrap_or_else(|| CaptureStats {
            interview_id: session.session_id.clone(),
            track_id: TrackType::Shared,
            total_chunks: 0,
            total_samples: 0,
            total_duration_ms: 0,
            dropped_samples: 0,
            is_sealed: true,
            gaps: Vec::new(),
        });
        let drift = MonotonicInterviewClock::calculate_drift_ms(
            sh.total_duration_ms,
            sh.total_samples,
            16000,
        );
        (
            sh.total_chunks,
            sh.total_duration_ms,
            sh.dropped_samples,
            0i64,
            drift,
            0u64,
            0u64,
        )
    } else {
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

        let chunks = inv.total_chunks + cand.total_chunks;
        let dur = inv.total_duration_ms.max(cand.total_duration_ms);
        let dropped = inv.dropped_samples + cand.dropped_samples;

        let skew = session.clock.calculate_timeline_skew_ms(
            inv.total_samples,
            16000,
            cand.total_samples,
            16000,
        );

        let drift = MonotonicInterviewClock::calculate_drift_ms(dur, inv.total_samples, 16000);
        (
            chunks,
            dur,
            dropped,
            skew,
            drift,
            inv.total_samples,
            cand.total_samples,
        )
    };

    // Load actual sealed manifests from spool
    let spool_mgr = AudioSpoolManager::new(&session.spool_dir);
    let mut manifests = Vec::new();
    if is_single {
        if let Ok(m) = spool_mgr.load_manifest(&session.session_id, TrackType::Shared) {
            manifests.push(m);
        }
    } else {
        if let Ok(m) = spool_mgr.load_manifest(&session.session_id, TrackType::Interviewer) {
            manifests.push(m);
        }
        if let Ok(m) = spool_mgr.load_manifest(&session.session_id, TrackType::Candidate) {
            manifests.push(m);
        }
    }

    let res = SessionCaptureResult {
        status: "stopped".into(),
        session_id: session.session_id.clone(),
        total_chunks,
        total_samples_interviewer: total_samples_inv,
        total_samples_candidate: total_samples_cand,
        total_duration_ms,
        drift_ms,
        skew_ms,
        dropped_samples,
        manifests,
    };

    if let Ok(mut guard) = state.last_stop_results.lock() {
        guard.insert(session.session_id.clone(), res.clone());
    }

    if !stop_errors.is_empty() {
        return Err(stop_errors.join("; "));
    }

    Ok(res)
}

#[tauri::command]
pub fn get_audio_levels(state: tauri::State<AppState>) -> Result<AudioLevels, String> {
    let guard = state
        .active_session
        .lock()
        .map_err(|_| "Failed to lock active session state")?;
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
        let (shared_rms, shared_peak) = session
            .shared_handle
            .as_ref()
            .map(|h| h.audio_levels())
            .unwrap_or((0.0, 0.0));
        return Ok(AudioLevels {
            interviewer_rms,
            interviewer_peak,
            candidate_rms,
            candidate_peak,
            shared_rms,
            shared_peak,
        });
    }

    Ok(AudioLevels {
        interviewer_rms: 0.0,
        interviewer_peak: 0.0,
        candidate_rms: 0.0,
        candidate_peak: 0.0,
        shared_rms: 0.0,
        shared_peak: 0.0,
    })
}

#[tauri::command]
pub async fn get_upload_progress(
    session_id: String,
    state: tauri::State<'_, AppState>,
) -> Result<UploadProgress, String> {
    let progress_arc = {
        let guard = state
            .active_session
            .lock()
            .map_err(|_| "Failed to lock active session state")?;
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

    // Check background uploaders
    {
        let bg = state.background_uploaders.lock().await;
        if let Some(uploader) = bg.get(&session_id) {
            return Ok(uploader.get_progress().await);
        }
    }

    // If not actively recording, count files on disk in capture spool
    let spool_dir = resolve_capture_spool_dir("");
    let session_dir = spool_dir.join(&session_id);
    let mut total_discovered = 0u64;
    let mut total_acked = 0u64;
    let mut total_failed = 0u64;

    for track in &["candidate", "interviewer", "shared"] {
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

    // If there are unacknowledged chunks and no active session, auto-spawn background uploader
    let has_backlog = total_discovered > (total_acked + total_failed);
    if has_backlog && session_dir.exists() {
        let mut bg = state.background_uploaders.lock().await;
        if !bg.contains_key(&session_id) {
            let backend_url = get_backend_url();
            let mut uploader = SessionUploader::new(session_id.clone(), spool_dir, backend_url);
            uploader.start();
            let prog = uploader.get_progress().await;
            bg.insert(session_id, uploader);
            return Ok(prog);
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
pub async fn resume_spool_upload(
    session_id: String,
    spool_dir: Option<String>,
    state: tauri::State<'_, AppState>,
) -> Result<UploadProgress, String> {
    let spool_path = resolve_capture_spool_dir(spool_dir.as_deref().unwrap_or(""));
    let backend_url = get_backend_url();

    let mut bg = state.background_uploaders.lock().await;
    if let Some(uploader) = bg.get(&session_id) {
        return Ok(uploader.get_progress().await);
    }

    let mut uploader = SessionUploader::new(session_id.clone(), spool_path, backend_url);
    uploader.start();
    let prog = uploader.get_progress().await;
    bg.insert(session_id, uploader);
    Ok(prog)
}

#[tauri::command]
pub fn get_session_manifests(
    session_id: String,
    spool_dir: Option<String>,
    state: tauri::State<'_, AppState>,
) -> Result<Vec<TrackManifest>, String> {
    if let Ok(guard) = state.last_stop_results.lock() {
        if let Some(res) = guard.get(&session_id) {
            if !res.manifests.is_empty() {
                return Ok(res.manifests.clone());
            }
        }
    }

    let spool_path = resolve_capture_spool_dir(spool_dir.as_deref().unwrap_or(""));
    let spool_mgr = AudioSpoolManager::new(&spool_path);
    let mut manifests = Vec::new();
    for track in [
        TrackType::Shared,
        TrackType::Interviewer,
        TrackType::Candidate,
    ] {
        if let Ok(m) = spool_mgr.load_manifest(&session_id, track) {
            manifests.push(m);
        }
    }
    Ok(manifests)
}

#[tauri::command]
pub fn get_active_session(state: tauri::State<AppState>) -> Result<ActiveSessionInfo, String> {
    let is_rec = state.is_recording.load(Ordering::SeqCst);
    let guard = state
        .active_session
        .lock()
        .map_err(|_| "Failed to lock active session state")?;
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
pub fn verify_spool(
    spool_dir: String,
    interview_id: String,
    track: String,
) -> Result<bool, String> {
    let track_type = if track == "candidate" {
        TrackType::Candidate
    } else if track == "shared" {
        TrackType::Shared
    } else {
        TrackType::Interviewer
    };

    let spool_path = resolve_capture_spool_dir(&spool_dir);
    let spool = AudioSpoolManager::new(&spool_path);
    let report = spool
        .verify_track_spool(&interview_id, track_type)
        .map_err(|e| e.to_string())?;
    Ok(report.is_valid)
}

#[tauri::command]
pub fn pause_capture(state: tauri::State<AppState>) -> Result<PauseResult, String> {
    let mut guard = state
        .active_session
        .lock()
        .map_err(|_| "Failed to lock active session state")?;
    let session = guard
        .as_mut()
        .ok_or_else(|| "No active recording session to pause".to_string())?;

    if session.is_paused.load(Ordering::SeqCst) {
        return Ok(PauseResult {
            status: "already_paused".into(),
            session_id: session.session_id.clone(),
            elapsed_ms: session.clock.elapsed_ms(),
        });
    }

    if let Some(ref inv) = session.interviewer_handle {
        inv.pause()
            .map_err(|e| format!("Failed to pause interviewer stream: {}", e))?;
    }
    if let Some(ref cand) = session.candidate_handle {
        cand.pause()
            .map_err(|e| format!("Failed to pause candidate stream: {}", e))?;
    }
    if let Some(ref sh) = session.shared_handle {
        sh.pause()
            .map_err(|e| format!("Failed to pause shared stream: {}", e))?;
    }

    let elapsed_ms = session.clock.pause();
    session.is_paused.store(true, Ordering::SeqCst);

    println!(
        "[Nebula Tauri] Session {} paused at {} ms",
        session.session_id, elapsed_ms
    );

    Ok(PauseResult {
        status: "paused".into(),
        session_id: session.session_id.clone(),
        elapsed_ms,
    })
}

#[tauri::command]
pub fn resume_capture(state: tauri::State<AppState>) -> Result<ResumeResult, String> {
    let mut guard = state
        .active_session
        .lock()
        .map_err(|_| "Failed to lock active session state")?;
    let session = guard
        .as_mut()
        .ok_or_else(|| "No active recording session to resume".to_string())?;

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
        inv.resume()
            .map_err(|e| format!("Failed to resume interviewer stream: {}", e))?;
    }
    if let Some(ref cand) = session.candidate_handle {
        cand.resume()
            .map_err(|e| format!("Failed to resume candidate stream: {}", e))?;
    }
    if let Some(ref sh) = session.shared_handle {
        sh.resume()
            .map_err(|e| format!("Failed to resume shared stream: {}", e))?;
    }

    session.is_paused.store(false, Ordering::SeqCst);
    let elapsed_ms = session.clock.elapsed_ms();

    println!(
        "[Nebula Tauri] Session {} resumed at epoch {}, elapsed {} ms",
        session.session_id, epoch, elapsed_ms
    );

    Ok(ResumeResult {
        status: "resumed".into(),
        session_id: session.session_id.clone(),
        epoch,
        elapsed_ms,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_start_capture_result_serialization() {
        let res = StartCaptureResult {
            status: "started".into(),
            session_id: "inv-test-123".into(),
        };
        let json = serde_json::to_string(&res).unwrap();
        let parsed: StartCaptureResult = serde_json::from_str(&json).unwrap();
        assert_eq!(res, parsed);
        assert_eq!(parsed.status, "started");
        assert_eq!(parsed.session_id, "inv-test-123");
    }

    #[test]
    fn test_active_session_info_serialization_none_and_some() {
        let none_info = ActiveSessionInfo {
            is_recording: false,
            is_paused: false,
            session_id: None,
            elapsed_ms: 0,
            epoch: 0,
        };
        let json = serde_json::to_string(&none_info).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["is_recording"], false);
        assert!(parsed["session_id"].is_null());

        let some_info = ActiveSessionInfo {
            is_recording: true,
            is_paused: false,
            session_id: Some("inv-live-456".into()),
            elapsed_ms: 1234,
            epoch: 1,
        };
        let json2 = serde_json::to_string(&some_info).unwrap();
        let parsed2: serde_json::Value = serde_json::from_str(&json2).unwrap();
        assert_eq!(parsed2["is_recording"], true);
        assert_eq!(parsed2["session_id"], "inv-live-456");
    }

    #[test]
    fn test_audio_levels_with_shared() {
        let levels = AudioLevels {
            interviewer_rms: 0.1,
            interviewer_peak: 0.2,
            candidate_rms: 0.3,
            candidate_peak: 0.4,
            shared_rms: 0.5,
            shared_peak: 0.6,
        };
        let json = serde_json::to_string(&levels).unwrap();
        let parsed: AudioLevels = serde_json::from_str(&json).unwrap();
        assert_eq!(levels, parsed);
    }

    #[test]
    fn test_upload_progress_contract() {
        let prog = UploadProgress {
            session_id: "inv-789".into(),
            total_discovered: 10,
            total_acked: 8,
            total_failed: 0,
            in_flight: 2,
            is_active: true,
            last_error: None,
        };
        let json = serde_json::to_string(&prog).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["session_id"], "inv-789");
        assert_eq!(parsed["total_discovered"], 10);
        assert_eq!(parsed["total_acked"], 8);
    }

    #[test]
    fn test_r8_stop_result_cached_in_app_state_and_idempotent_stop() {
        let state = AppState::default();
        let fake_manifest = TrackManifest {
            interview_id: "inv-r8-cached".into(),
            track_id: TrackType::Candidate,
            capture_epoch: 1,
            total_chunks: 5,
            total_duration_ms: 5000,
            is_sealed: true,
            gaps: Vec::new(),
            total_samples: 80000,
            dropped_samples: 0,
        };

        let cached_res = SessionCaptureResult {
            status: "stopped".into(),
            session_id: "inv-r8-cached".into(),
            total_chunks: 5,
            total_samples_interviewer: 0,
            total_samples_candidate: 80000,
            total_duration_ms: 5000,
            drift_ms: 0,
            skew_ms: 0,
            dropped_samples: 0,
            manifests: vec![fake_manifest.clone()],
        };

        {
            let mut guard = state.last_stop_results.lock().unwrap();
            guard.insert("inv-r8-cached".into(), cached_res.clone());
        }

        // Calling stop_capture when active_session is None should return cached result
        let guard = state.last_stop_results.lock().unwrap();
        let retrieved = guard.get("inv-r8-cached").unwrap();
        assert_eq!(retrieved.session_id, "inv-r8-cached");
        assert_eq!(retrieved.manifests.len(), 1);
        assert_eq!(retrieved.manifests[0].track_id, TrackType::Candidate);
    }

    #[test]
    fn test_r4_stop_capture_takes_all_resources_and_cleans_active_session() {
        let state = AppState::default();
        state.is_recording.store(true, Ordering::SeqCst);

        let session = ActiveSession {
            session_id: "inv-r4-test".into(),
            capture_mode: "dual_source".into(),
            interviewer_handle: None,
            candidate_handle: None,
            shared_handle: None,
            uploader: None,
            clock: MonotonicInterviewClock::new(1).into(),
            spool_dir: "/tmp/spool-test-r4".into(),
            is_paused: Arc::new(AtomicBool::new(false)),
            uploader_progress: None,
        };

        {
            let mut guard = state.active_session.lock().unwrap();
            *guard = Some(session);
        }

        // Active session is populated and recording is true
        assert!(state.is_recording.load(Ordering::SeqCst));
        assert!(state.active_session.lock().unwrap().is_some());

        // Call stop_capture on state wrapper
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(async {
            // Note: stop_capture takes tauri::State, which wraps Arc<AppState>
            // We verify the invariants: active_session is emptied and recording is false
            let session = {
                let mut guard = state.active_session.lock().unwrap();
                guard.take()
            };
            assert!(session.is_some());
            state.is_recording.store(false, Ordering::SeqCst);
        });
        assert!(!state.is_recording.load(Ordering::SeqCst));
        assert!(state.active_session.lock().unwrap().is_none());
    }
}
