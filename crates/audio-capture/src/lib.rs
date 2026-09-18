pub mod capture;
pub mod clock;
pub mod resampler;
pub mod spool;
pub mod synthetic;
pub mod types;

pub use capture::{
    list_input_devices, list_output_devices, run_capture_worker, run_capture_worker_extended,
    start_device_capture, AudioLevelMetrics, CaptureHandle, CaptureWorkerConfig, DeviceInfo,
};
pub use clock::{DriftMetrics, MonotonicInterviewClock, SharedInterviewClock};
pub use resampler::{
    calculate_peak_f32, calculate_rms_f32, calculate_rms_i16, f32_to_pcm_s16le,
    multi_channel_to_mono_f32, resample_linear_f32, StatefulAudioConverter,
};
pub use spool::{AudioSpoolManager, VerificationReport};
pub use types::{
    AudioChunkMetadata, AudioFormat, AudioGap, CaptureStats, TrackManifest, TrackType,
};

pub use synthetic::{simulate_track_recording, SimulationResult, SyntheticTrackConfig};
