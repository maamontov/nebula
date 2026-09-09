use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TrackType {
    Interviewer,
    Candidate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AudioFormat {
    PcmS16Le,
    PcmF32Le,
    Opus,
    Wav,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AudioChunkMetadata {
    pub interview_id: String,
    pub track_id: TrackType,
    pub capture_epoch: u32,
    pub sequence: u64,
    pub start_time_ms: u64,
    pub end_time_ms: u64,
    pub sample_rate: u32,
    pub channels: u16,
    pub sample_count: u64,
    pub format: AudioFormat,
    pub checksum_sha256: String,
    pub size_bytes: usize,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AudioGap {
    pub track_id: TrackType,
    pub start_time_ms: u64,
    pub end_time_ms: u64,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TrackManifest {
    pub interview_id: String,
    pub track_id: TrackType,
    pub capture_epoch: u32,
    pub total_chunks: u64,
    pub total_duration_ms: u64,
    pub is_sealed: bool,
    #[serde(default)]
    pub gaps: Vec<AudioGap>,
}
