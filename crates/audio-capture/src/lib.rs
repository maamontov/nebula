pub mod clock;
pub mod resampler;
pub mod spool;
pub mod types;
pub mod capture;
pub mod synthetic;

pub use clock::{DriftMetrics, MonotonicInterviewClock};
pub use resampler::{
    calculate_rms_f32, calculate_rms_i16, f32_to_pcm_s16le,
    multi_channel_to_mono_f32, resample_linear_f32,
};
pub use spool::{AudioSpoolManager, VerificationReport};
pub use types::{AudioChunkMetadata, AudioFormat, AudioGap, TrackManifest, TrackType};
pub use capture::{list_input_devices, list_output_devices, start_device_capture, DeviceInfo};
pub use synthetic::{simulate_track_recording, SyntheticTrackConfig, SimulationResult};
