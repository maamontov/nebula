use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use audio_capture::AudioChunkMetadata;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UploadProgress {
    pub session_id: String,
    pub total_discovered: u64,
    pub total_acked: u64,
    pub total_failed: u64,
    pub in_flight: u64,
    pub is_active: bool,
    pub last_error: Option<String>,
}

#[derive(Serialize)]
struct ChunkUploadPayload {
    metadata: AudioChunkMetadata,
    payload_hex: String,
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct ChunkUploadResponse {
    status: String,
    interview_id: String,
    track_id: String,
    sequence: u64,
}

pub struct SessionUploader {
    session_id: String,
    spool_dir: PathBuf,
    backend_url: String,
    is_running: Arc<AtomicBool>,
    handle: Option<tauri::async_runtime::JoinHandle<()>>,
    progress: Arc<Mutex<UploadProgress>>,
}

impl SessionUploader {
    pub fn new(session_id: String, spool_dir: PathBuf, backend_url: String) -> Self {
        let progress = Arc::new(Mutex::new(UploadProgress {
            session_id: session_id.clone(),
            total_discovered: 0,
            total_acked: 0,
            total_failed: 0,
            in_flight: 0,
            is_active: false,
            last_error: None,
        }));

        Self {
            session_id,
            spool_dir,
            backend_url,
            is_running: Arc::new(AtomicBool::new(false)),
            handle: None,
            progress,
        }
    }

    pub fn progress_handle(&self) -> Arc<Mutex<UploadProgress>> {
        self.progress.clone()
    }

    pub fn start(&mut self) {
        if self.is_running.swap(true, Ordering::SeqCst) {
            return; // Already running
        }

        let session_id = self.session_id.clone();
        let spool_dir = self.spool_dir.clone();
        let backend_url = self.backend_url.clone();
        let is_running = self.is_running.clone();
        let progress = self.progress.clone();

        let task = async move {
            {
                let mut p = progress.lock().await;
                p.is_active = true;
            }

            let client = reqwest::Client::builder()
                .timeout(Duration::from_secs(10))
                .build()
                .unwrap_or_else(|_| reqwest::Client::new());

            let mut backoff_ms = 200u64;

            while is_running.load(Ordering::Relaxed) {
                let sent_any = Self::poll_and_upload_tracks(
                    &session_id,
                    &spool_dir,
                    &backend_url,
                    &client,
                    &progress,
                    &is_running,
                ).await;

                if let Some(true) = sent_any {
                    // Reset backoff on progress
                    backoff_ms = 200;
                    tokio::time::sleep(Duration::from_millis(50)).await;
                } else if let Some(false) = sent_any {
                    // Idle - wait a bit
                    tokio::time::sleep(Duration::from_millis(200)).await;
                } else {
                    // Error encountered or fatal stop
                    if !is_running.load(Ordering::Relaxed) {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(backoff_ms)).await;
                    backoff_ms = (backoff_ms * 2).min(3000);
                }
            }

            // Final drain pass upon stop
            let _ = Self::poll_and_upload_tracks(
                &session_id,
                &spool_dir,
                &backend_url,
                &client,
                &progress,
                &is_running,
            ).await;

            let mut p = progress.lock().await;
            p.is_active = false;
        };

        let handle = tauri::async_runtime::spawn(task);
        self.handle = Some(handle);
    }

    /// Scans candidate and interviewer tracks and uploads pending unacknowledged chunks.
    /// Returns:
    /// Some(true) if at least one chunk was successfully uploaded
    /// Some(false) if queue is completely up to date
    /// None if an error occurred or interview was deleted (404)
    async fn poll_and_upload_tracks(
        session_id: &str,
        spool_dir: &Path,
        backend_url: &str,
        client: &reqwest::Client,
        progress: &Arc<Mutex<UploadProgress>>,
        is_running: &Arc<AtomicBool>,
    ) -> Option<bool> {
        let session_path = spool_dir.join(session_id);
        if !session_path.exists() {
            return Some(false);
        }

        let tracks = ["candidate", "interviewer", "shared"];
        let mut any_uploaded = false;
        let mut total_discovered = 0u64;
        let mut total_acked = 0u64;
        let mut total_failed = 0u64;

        for track_name in tracks {
            let track_dir = session_path.join(track_name);
            if !track_dir.exists() {
                continue;
            }

            let entries = match std::fs::read_dir(&track_dir) {
                Ok(e) => e,
                Err(_) => continue,
            };

            let mut meta_files = Vec::new();
            for entry in entries.flatten() {
                let path = entry.path();
                if let Some(ext) = path.extension() {
                    if ext == "json" && path.to_string_lossy().ends_with(".meta.json") {
                        meta_files.push(path);
                    }
                }
            }
            // Sort by sequence order
            meta_files.sort();

            total_discovered += meta_files.len() as u64;

            for meta_path in meta_files {
                let file_stem = meta_path.file_name().and_then(|s| s.to_str()).unwrap_or("");
                let seq_prefix = file_stem.trim_end_matches(".meta.json");

                let chunk_path = track_dir.join(format!("{}.chunk", seq_prefix));
                let tmp_path = track_dir.join(format!("{}.tmp", seq_prefix));
                let ack_path = track_dir.join(format!("{}.ack", seq_prefix));
                let err_path = track_dir.join(format!("{}.err", seq_prefix));

                // If chunk is still being written (.tmp exists) or already acked or failed, skip
                if tmp_path.exists() || !chunk_path.exists() {
                    continue;
                }
                if ack_path.exists() {
                    total_acked += 1;
                    continue;
                }
                if err_path.exists() {
                    total_failed += 1;
                    continue;
                }

                // Read metadata
                let meta_str = match std::fs::read_to_string(&meta_path) {
                    Ok(s) => s,
                    Err(e) => {
                        eprintln!("[Uploader] Error reading {:?}: {}", meta_path, e);
                        continue;
                    }
                };

                let metadata: AudioChunkMetadata = match serde_json::from_str(&meta_str) {
                    Ok(m) => m,
                    Err(e) => {
                        eprintln!("[Uploader] JSON parse error in {:?}: {}", meta_path, e);
                        continue;
                    }
                };

                // Read chunk bytes
                let chunk_bytes = match std::fs::read(&chunk_path) {
                    Ok(b) => b,
                    Err(e) => {
                        eprintln!("[Uploader] Error reading {:?}: {}", chunk_path, e);
                        continue;
                    }
                };

                // In-flight tracking
                {
                    let mut p = progress.lock().await;
                    p.in_flight += 1;
                }

                let payload = ChunkUploadPayload {
                    metadata: metadata.clone(),
                    payload_hex: hex::encode(&chunk_bytes),
                };

                let url = format!("{}/api/v1/interviews/{}/audio/chunks", backend_url, session_id);
                let res = client.post(&url).json(&payload).send().await;

                {
                    let mut p = progress.lock().await;
                    p.in_flight = p.in_flight.saturating_sub(1);
                }

                match res {
                    Ok(resp) => {
                        let status = resp.status();
                        if status.is_success() {
                            // Write .ack file atomically
                            let ack_data = serde_json::json!({
                                "sequence": metadata.sequence,
                                "track_id": track_name,
                                "acked_at": chrono_like_now(),
                                "checksum": metadata.checksum_sha256,
                            });
                            let ack_tmp = track_dir.join(format!("{}.ack.tmp", seq_prefix));
                            if let Ok(_) = std::fs::write(&ack_tmp, ack_data.to_string()) {
                                let _ = std::fs::rename(&ack_tmp, &ack_path);
                            }
                            total_acked += 1;
                            any_uploaded = true;
                        } else if status == reqwest::StatusCode::NOT_FOUND {
                            // Interview was deleted! Stop uploader permanently for this session
                            eprintln!("[Uploader] Interview {} not found (404). Terminating uploader.", session_id);
                            is_running.store(false, Ordering::SeqCst);
                            let mut p = progress.lock().await;
                            p.last_error = Some("Интервью удалено на сервере (404)".into());
                            return None;
                        } else if status == reqwest::StatusCode::CONFLICT {
                            let err_text = resp.text().await.unwrap_or_default();
                            eprintln!("[Uploader] Conflict 409 on chunk {}: {}", metadata.sequence, err_text);
                            let _ = std::fs::write(&err_path, &err_text);
                            total_failed += 1;
                            let mut p = progress.lock().await;
                            p.last_error = Some(format!("Конфликт чанка: {}", err_text));
                        } else {
                            let err_msg = format!("Server error HTTP {}: seq {}", status, metadata.sequence);
                            let mut p = progress.lock().await;
                            p.last_error = Some(err_msg);
                            return None;
                        }
                    }
                    Err(err) => {
                        let mut p = progress.lock().await;
                        p.last_error = Some(format!("Сетевая ошибка доставки: {}", err));
                        return None;
                    }
                }
            }
        }

        {
            let mut p = progress.lock().await;
            p.total_discovered = total_discovered;
            p.total_acked = total_acked;
            p.total_failed = total_failed;
        }

        Some(any_uploaded)
    }

    #[allow(dead_code)]
    pub async fn get_progress(&self) -> UploadProgress {
        self.progress.lock().await.clone()
    }

    pub async fn stop(&mut self) {
        self.is_running.store(false, Ordering::SeqCst);
        if let Some(handle) = self.handle.take() {
            let _ = tokio::time::timeout(Duration::from_secs(3), handle).await;
        }
    }
}

fn chrono_like_now() -> String {
    let now = std::time::SystemTime::now();
    format!("{:?}", now)
}
