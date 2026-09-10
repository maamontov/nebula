use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use sha2::{Digest, Sha256};
use anyhow::{Context, Result, bail};

use crate::types::{AudioChunkMetadata, AudioFormat, TrackManifest, TrackType};

pub struct AudioSpoolManager {
    base_dir: PathBuf,
}

impl AudioSpoolManager {
    pub fn new<P: AsRef<Path>>(base_dir: P) -> Self {
        Self {
            base_dir: base_dir.as_ref().to_path_buf(),
        }
    }

    pub fn get_track_dir(&self, interview_id: &str, track_id: TrackType) -> PathBuf {
        let track_name = match track_id {
            TrackType::Interviewer => "interviewer",
            TrackType::Candidate => "candidate",
        };
        self.base_dir.join(interview_id).join(track_name)
    }

    pub fn init_track_dir(&self, interview_id: &str, track_id: TrackType) -> Result<PathBuf> {
        let dir = self.get_track_dir(interview_id, track_id);
        fs::create_dir_all(&dir)
            .with_context(|| format!("Failed to create spool track dir: {:?}", dir))?;
        Ok(dir)
    }

    /// Atomically writes an audio chunk and its metadata to the spool directory.
    pub fn write_chunk(
        &self,
        interview_id: &str,
        track_id: TrackType,
        capture_epoch: u32,
        sequence: u64,
        start_time_ms: u64,
        end_time_ms: u64,
        sample_rate: u32,
        channels: u16,
        sample_count: u64,
        format: AudioFormat,
        data: &[u8],
    ) -> Result<AudioChunkMetadata> {
        let track_dir = self.init_track_dir(interview_id, track_id)?;

        // Compute SHA-256
        let mut hasher = Sha256::new();
        hasher.update(data);
        let checksum = hex::encode(hasher.finalize());

        let metadata = AudioChunkMetadata {
            interview_id: interview_id.to_string(),
            track_id,
            capture_epoch,
            sequence,
            start_time_ms,
            end_time_ms,
            sample_rate,
            channels,
            sample_count,
            format,
            checksum_sha256: checksum,
            size_bytes: data.len(),
        };

        let chunk_file_name = format!("{:08}.chunk", sequence);
        let tmp_file_name = format!("{:08}.tmp", sequence);
        let meta_file_name = format!("{:08}.meta.json", sequence);

        let tmp_path = track_dir.join(&tmp_file_name);
        let chunk_path = track_dir.join(&chunk_file_name);
        let meta_path = track_dir.join(&meta_file_name);

        // 1. Write chunk to temp file atomically
        {
            let mut file = File::create(&tmp_path)
                .with_context(|| format!("Failed to create tmp chunk file: {:?}", tmp_path))?;
            file.write_all(data)
                .with_context(|| format!("Failed to write chunk payload to {:?}", tmp_path))?;
            file.sync_all()?;
        }

        // 2. Rename tmp -> final chunk file (atomic on POSIX/Windows)
        fs::rename(&tmp_path, &chunk_path)
            .with_context(|| format!("Failed to rename {:?} to {:?}", tmp_path, chunk_path))?;

        // 3. Write metadata sidecar JSON
        let meta_json = serde_json::to_string_pretty(&metadata)?;
        fs::write(&meta_path, meta_json)
            .with_context(|| format!("Failed to write metadata to {:?}", meta_path))?;

        Ok(metadata)
    }

    /// Seals the track manifest once recording ends.
    pub fn seal_manifest(
        &self,
        interview_id: &str,
        track_id: TrackType,
        capture_epoch: u32,
        total_chunks: u64,
        total_duration_ms: u64,
    ) -> Result<TrackManifest> {
        self.seal_manifest_extended(
            interview_id,
            track_id,
            capture_epoch,
            total_chunks,
            total_duration_ms,
            0,
            0,
            Vec::new(),
        )
    }

    /// Seals the track manifest with full sample counts, overflow metrics, and gaps.
    pub fn seal_manifest_extended(
        &self,
        interview_id: &str,
        track_id: TrackType,
        capture_epoch: u32,
        total_chunks: u64,
        total_duration_ms: u64,
        total_samples: u64,
        dropped_samples: u64,
        gaps: Vec<crate::types::AudioGap>,
    ) -> Result<TrackManifest> {
        let track_dir = self.get_track_dir(interview_id, track_id);
        let manifest = TrackManifest {
            interview_id: interview_id.to_string(),
            track_id,
            capture_epoch,
            total_chunks,
            total_duration_ms,
            is_sealed: true,
            gaps,
            total_samples,
            dropped_samples,
        };

        let manifest_path = track_dir.join("manifest.json");
        let json_str = serde_json::to_string_pretty(&manifest)?;
        fs::write(&manifest_path, json_str)?;

        Ok(manifest)
    }

    /// Loads a sealed track manifest from disk.
    pub fn load_manifest(&self, interview_id: &str, track_id: TrackType) -> Result<TrackManifest> {
        let manifest_path = self.get_track_dir(interview_id, track_id).join("manifest.json");
        if !manifest_path.exists() {
            bail!("Manifest does not exist at {:?}", manifest_path);
        }
        let data = fs::read_to_string(&manifest_path)?;
        Ok(serde_json::from_str(&data)?)
    }


    /// Verifies all chunks in a track directory against their metadata and checksums.
    pub fn verify_track_spool(
        &self,
        interview_id: &str,
        track_id: TrackType,
    ) -> Result<VerificationReport> {
        let track_dir = self.get_track_dir(interview_id, track_id);
        if !track_dir.exists() {
            bail!("Track directory does not exist: {:?}", track_dir);
        }

        let manifest_path = track_dir.join("manifest.json");
        let manifest: Option<TrackManifest> = if manifest_path.exists() {
            let data = fs::read_to_string(&manifest_path)?;
            Some(serde_json::from_str(&data)?)
        } else {
            None
        };

        let mut verified_chunks = 0u64;
        let mut corrupted_chunks = Vec::new();
        let mut missing_sequences = Vec::new();

        // Scan directory for .chunk files
        let mut sequences = Vec::new();
        for entry in fs::read_dir(&track_dir)? {
            let entry = entry?;
            let path = entry.path();
            if let Some(ext) = path.extension() {
                if ext == "chunk" {
                    if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                        if let Ok(seq) = stem.parse::<u64>() {
                            sequences.push(seq);
                        }
                    }
                }
            }
        }
        sequences.sort_unstable();

        // Check for sequence gaps
        if !sequences.is_empty() {
            let mut expected_seq = 0u64;
            for &seq in &sequences {
                while expected_seq < seq {
                    missing_sequences.push(expected_seq);
                    expected_seq += 1;
                }
                expected_seq = seq + 1;
            }
        }

        // Verify hashes
        for &seq in &sequences {
            let chunk_path = track_dir.join(format!("{:08}.chunk", seq));
            let meta_path = track_dir.join(format!("{:08}.meta.json", seq));

            if !meta_path.exists() {
                corrupted_chunks.push((seq, "Missing .meta.json file".to_string()));
                continue;
            }

            let meta_str = fs::read_to_string(&meta_path)?;
            let meta: AudioChunkMetadata = match serde_json::from_str(&meta_str) {
                Ok(m) => m,
                Err(e) => {
                    corrupted_chunks.push((seq, format!("Invalid JSON metadata: {}", e)));
                    continue;
                }
            };

            let mut chunk_bytes = Vec::new();
            File::open(&chunk_path)?.read_to_end(&mut chunk_bytes)?;

            let mut hasher = Sha256::new();
            hasher.update(&chunk_bytes);
            let calculated_hash = hex::encode(hasher.finalize());

            if calculated_hash != meta.checksum_sha256 {
                corrupted_chunks.push((
                    seq,
                    format!(
                        "Hash mismatch! Meta: {}, Real: {}",
                        meta.checksum_sha256, calculated_hash
                    ),
                ));
            } else {
                verified_chunks += 1;
            }
        }

        let is_valid = corrupted_chunks.is_empty() && missing_sequences.is_empty();

        Ok(VerificationReport {
            track_id,
            total_chunks_found: sequences.len() as u64,
            verified_chunks,
            is_sealed: manifest.map(|m| m.is_sealed).unwrap_or(false),
            is_valid,
            missing_sequences,
            corrupted_chunks,
        })
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct VerificationReport {
    pub track_id: TrackType,
    pub total_chunks_found: u64,
    pub verified_chunks: u64,
    pub is_sealed: bool,
    pub is_valid: bool,
    pub missing_sequences: Vec<u64>,
    pub corrupted_chunks: Vec<(u64, String)>,
}
