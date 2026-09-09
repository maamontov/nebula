use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use anyhow::{Context, Result, bail};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, Host, Stream};
use ringbuf::traits::{Consumer, Producer, Split};
use ringbuf::HeapRb;

use crate::clock::MonotonicInterviewClock;
use crate::resampler::{f32_to_pcm_s16le, multi_channel_to_mono_f32, resample_linear_f32};
use crate::spool::AudioSpoolManager;
use crate::types::{AudioFormat, TrackType};

#[derive(Debug, Clone)]
pub struct DeviceInfo {
    pub name: String,
    pub is_default: bool,
}

pub fn list_input_devices(host: &Host) -> Result<Vec<DeviceInfo>> {
    let default_name = host.default_input_device().and_then(|d| d.name().ok());
    let mut devices = Vec::new();
    for dev in host.input_devices()? {
        if let Ok(name) = dev.name() {
            let is_default = default_name.as_ref().map(|d| d == &name).unwrap_or(false);
            devices.push(DeviceInfo { name, is_default });
        }
    }
    Ok(devices)
}

pub fn list_output_devices(host: &Host) -> Result<Vec<DeviceInfo>> {
    let default_name = host.default_output_device().and_then(|d| d.name().ok());
    let mut devices = Vec::new();
    for dev in host.output_devices()? {
        if let Ok(name) = dev.name() {
            let is_default = default_name.as_ref().map(|d| d == &name).unwrap_or(false);
            devices.push(DeviceInfo { name, is_default });
        }
    }
    Ok(devices)
}

pub struct CaptureHandle {
    _stream: Stream,
    is_running: Arc<AtomicBool>,
    worker_thread: Option<std::thread::JoinHandle<()>>,
}

impl CaptureHandle {
    pub fn stop(mut self) {
        self.is_running.store(false, Ordering::SeqCst);
        if let Some(th) = self.worker_thread.take() {
            let _ = th.join();
        }
    }
}

/// Spawns a dedicated audio capture pipeline for a single track.
/// Realtime callback feeds a lock-free ring buffer;
/// Worker thread drains buffer, downsamples to 16kHz mono, and spools chunks.
pub fn start_device_capture(
    device: &Device,
    interview_id: String,
    track_id: TrackType,
    spool_manager: Arc<AudioSpoolManager>,
    clock: MonotonicInterviewClock,
    chunk_duration_ms: u64,
) -> Result<CaptureHandle> {
    let default_config = device.default_input_config()
        .context("Failed to get default input config for device")?;

    let sample_rate = default_config.sample_rate().0;
    let channels = default_config.channels();
    let sample_format = default_config.sample_format();

    // Ring buffer size: 2 seconds of multi-channel audio
    let buffer_capacity = (sample_rate as usize) * (channels as usize) * 2;
    let rb = HeapRb::<f32>::new(buffer_capacity);
    let (mut producer, mut consumer) = rb.split();

    let is_running = Arc::new(AtomicBool::new(true));
    let is_running_clone = is_running.clone();

    // Build CPAL Stream
    let err_fn = move |err| {
        eprintln!("Audio input stream error: {:?}", err);
    };

    let stream = match sample_format {
        cpal::SampleFormat::F32 => {
            device.build_input_stream(
                &default_config.into(),
                move |data: &[f32], _: &_| {
                    for &sample in data {
                        let _ = producer.try_push(sample);
                    }
                },
                err_fn,
                None,
            )?
        }
        cpal::SampleFormat::I16 => {
            device.build_input_stream(
                &default_config.into(),
                move |data: &[i16], _: &_| {
                    for &sample in data {
                        let f_sample = sample as f32 / 32768.0;
                        let _ = producer.try_push(f_sample);
                    }
                },
                err_fn,
                None,
            )?
        }
        _ => bail!("Unsupported sample format: {:?}", sample_format),
    };

    stream.play().context("Failed to play audio stream")?;

    // Background worker thread to drain ringbuffer and spool chunks
    let target_sample_rate = 16000u32;
    let samples_per_chunk = ((target_sample_rate as u64 * chunk_duration_ms) / 1000) as usize;

    let worker_thread = std::thread::spawn(move || {
        let mut raw_samples_buf = Vec::with_capacity(4096);
        let mut mono_16k_accumulator = Vec::with_capacity(samples_per_chunk * 2);
        let mut chunk_sequence = 0u64;

        while is_running_clone.load(Ordering::Relaxed) {
            // Drain ringbuffer into temporary slice
            raw_samples_buf.clear();
            while let Some(sample) = consumer.try_pop() {
                raw_samples_buf.push(sample);
            }

            if !raw_samples_buf.is_empty() {
                // Convert to mono
                let mono_f32 = multi_channel_to_mono_f32(&raw_samples_buf, channels);
                // Resample to 16kHz
                let resampled_16k = resample_linear_f32(&mono_f32, sample_rate, target_sample_rate);
                mono_16k_accumulator.extend(resampled_16k);

                // Check if we accumulated enough for a chunk
                while mono_16k_accumulator.len() >= samples_per_chunk {
                    let chunk_samples: Vec<f32> = mono_16k_accumulator.drain(..samples_per_chunk).collect();
                    let pcm_s16le_bytes = f32_to_pcm_s16le(&chunk_samples);

                    let start_time_ms = chunk_sequence * chunk_duration_ms;
                    let end_time_ms = start_time_ms + chunk_duration_ms;

                    let _ = spool_manager.write_chunk(
                        &interview_id,
                        track_id,
                        clock.epoch(),
                        chunk_sequence,
                        start_time_ms,
                        end_time_ms,
                        target_sample_rate,
                        1, // mono
                        chunk_samples.len() as u64,
                        AudioFormat::PcmS16Le,
                        &pcm_s16le_bytes,
                    );

                    chunk_sequence += 1;
                }
            }

            std::thread::sleep(std::time::Duration::from_millis(20));
        }

        // Flush remaining samples if any
        if !mono_16k_accumulator.is_empty() {
            let pcm_s16le_bytes = f32_to_pcm_s16le(&mono_16k_accumulator);
            let duration_ms = (mono_16k_accumulator.len() as u64 * 1000) / target_sample_rate as u64;
            let start_time_ms = chunk_sequence * chunk_duration_ms;
            let end_time_ms = start_time_ms + duration_ms;

            let _ = spool_manager.write_chunk(
                &interview_id,
                track_id,
                clock.epoch(),
                chunk_sequence,
                start_time_ms,
                end_time_ms,
                target_sample_rate,
                1,
                mono_16k_accumulator.len() as u64,
                AudioFormat::PcmS16Le,
                &pcm_s16le_bytes,
            );
            chunk_sequence += 1;
        }

        let total_duration_ms = chunk_sequence * chunk_duration_ms;
        let _ = spool_manager.seal_manifest(
            &interview_id,
            track_id,
            clock.epoch(),
            chunk_sequence,
            total_duration_ms,
        );
    });

    Ok(CaptureHandle {
        _stream: stream,
        is_running,
        worker_thread: Some(worker_thread),
    })
}
