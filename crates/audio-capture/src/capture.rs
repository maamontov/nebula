use anyhow::{bail, Context, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, Host, Stream};
use ringbuf::traits::{Consumer, Producer, Split};
use ringbuf::HeapRb;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use crate::clock::{MonotonicInterviewClock, SharedInterviewClock};
use crate::resampler::{f32_to_pcm_s16le, StatefulAudioConverter};
use crate::spool::AudioSpoolManager;
use crate::types::{AudioFormat, AudioGap, CaptureStats, TrackType};

#[derive(Debug, Clone)]
pub struct DeviceInfo {
    pub name: String,
    pub is_default: bool,
}

pub fn list_input_devices(host: &Host) -> Result<Vec<DeviceInfo>> {
    let mut devices = Vec::new();
    let default_device = host.default_input_device().and_then(|d| d.name().ok());
    if let Ok(input_devices) = host.input_devices() {
        for dev in input_devices {
            if let Ok(name) = dev.name() {
                let is_default = default_device.as_deref() == Some(&name);
                devices.push(DeviceInfo { name, is_default });
            }
        }
    }
    Ok(devices)
}

pub fn list_output_devices(host: &Host) -> Result<Vec<DeviceInfo>> {
    let mut devices = Vec::new();
    let default_device = host.default_output_device().and_then(|d| d.name().ok());
    if let Ok(output_devices) = host.output_devices() {
        for dev in output_devices {
            if let Ok(name) = dev.name() {
                let is_default = default_device.as_deref() == Some(&name);
                devices.push(DeviceInfo { name, is_default });
            }
        }
    }
    Ok(devices)
}

#[derive(Debug, Clone)]
pub struct CaptureWorkerConfig {
    pub interview_id: String,
    pub track_id: TrackType,
    pub sample_rate: u32,
    pub channels: u16,
    pub chunk_duration_ms: u64,
    pub start_offset_ms: u64,
}

#[derive(Debug, Default)]
pub struct AudioLevelMetrics {
    rms: AtomicU64,
    peak: AtomicU64,
}

impl AudioLevelMetrics {
    pub fn update(&self, rms: f32, peak: f32) {
        self.rms.store(rms.to_bits() as u64, Ordering::Relaxed);
        self.peak.store(peak.to_bits() as u64, Ordering::Relaxed);
    }

    pub fn get(&self) -> (f32, f32) {
        (
            f32::from_bits(self.rms.load(Ordering::Relaxed) as u32),
            f32::from_bits(self.peak.load(Ordering::Relaxed) as u32),
        )
    }
}

/// Drains audio samples from lock-free consumer, downsamples with state preservation,
/// writes chunks to spool atomically, flushes trailing partial chunks on stop,
/// and seals the track manifest with exact duration and sample counts.
pub fn run_capture_worker<C: Consumer<Item = f32>>(
    consumer: C,
    spool_manager: Arc<AudioSpoolManager>,
    clock: impl Into<SharedInterviewClock>,
    config: CaptureWorkerConfig,
    is_running: Arc<AtomicBool>,
    dropped_samples: Arc<AtomicU64>,
) -> Result<CaptureStats> {
    run_capture_worker_extended(
        consumer,
        spool_manager,
        clock,
        config,
        is_running,
        dropped_samples,
        None,
    )
}

pub fn run_capture_worker_extended<C: Consumer<Item = f32>>(
    mut consumer: C,
    spool_manager: Arc<AudioSpoolManager>,
    clock: impl Into<SharedInterviewClock>,
    config: CaptureWorkerConfig,
    is_running: Arc<AtomicBool>,
    dropped_samples: Arc<AtomicU64>,
    level_metrics: Option<Arc<AudioLevelMetrics>>,
) -> Result<CaptureStats> {
    let clock = clock.into();
    let target_sample_rate = 16000u32;
    let samples_per_chunk =
        ((target_sample_rate as u64 * config.chunk_duration_ms) / 1000) as usize;
    let mut converter =
        StatefulAudioConverter::new(config.channels, config.sample_rate, target_sample_rate);

    let mut chunk_accumulator = Vec::with_capacity(samples_per_chunk * 2);
    let mut chunk_sequence = 0u64;
    let mut total_target_samples = 0u64;
    let mut raw_samples_buf = Vec::with_capacity(4096);
    let mut was_paused = false;

    while is_running.load(Ordering::Relaxed) {
        if clock.is_paused() {
            if !was_paused {
                // Pause boundary reached: drain available samples and flush pending accumulator
                raw_samples_buf.clear();
                while let Some(sample) = consumer.try_pop() {
                    raw_samples_buf.push(sample);
                }
                if !raw_samples_buf.is_empty() {
                    let converted = converter.process_input(&raw_samples_buf);
                    chunk_accumulator.extend(converted);
                }
                while chunk_accumulator.len() >= samples_per_chunk {
                    let chunk_samples: Vec<f32> =
                        chunk_accumulator.drain(..samples_per_chunk).collect();
                    let pcm_s16le_bytes = f32_to_pcm_s16le(&chunk_samples);

                    let (start_time_ms, end_time_ms) = clock.calculate_chunk_timeline(
                        config.track_id,
                        total_target_samples,
                        chunk_samples.len() as u64,
                        target_sample_rate,
                    );

                    spool_manager.write_chunk(
                        &config.interview_id,
                        config.track_id,
                        clock.epoch(),
                        chunk_sequence,
                        start_time_ms,
                        end_time_ms,
                        target_sample_rate,
                        1,
                        chunk_samples.len() as u64,
                        AudioFormat::PcmS16Le,
                        &pcm_s16le_bytes,
                    )?;

                    total_target_samples += chunk_samples.len() as u64;
                    chunk_sequence += 1;
                }
                if !chunk_accumulator.is_empty() {
                    let tail_samples_len = chunk_accumulator.len() as u64;
                    let pcm_s16le_bytes = f32_to_pcm_s16le(&chunk_accumulator);

                    let (start_time_ms, end_time_ms) = clock.calculate_chunk_timeline(
                        config.track_id,
                        total_target_samples,
                        tail_samples_len,
                        target_sample_rate,
                    );

                    spool_manager.write_chunk(
                        &config.interview_id,
                        config.track_id,
                        clock.epoch(),
                        chunk_sequence,
                        start_time_ms,
                        end_time_ms,
                        target_sample_rate,
                        1,
                        tail_samples_len,
                        AudioFormat::PcmS16Le,
                        &pcm_s16le_bytes,
                    )?;

                    total_target_samples += tail_samples_len;
                    chunk_sequence += 1;
                    chunk_accumulator.clear();
                }
                was_paused = true;
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
            continue;
        } else {
            was_paused = false;
        }

        raw_samples_buf.clear();
        while let Some(sample) = consumer.try_pop() {
            raw_samples_buf.push(sample);
        }

        if !raw_samples_buf.is_empty() {
            if let Some(ref lm) = level_metrics {
                let rms = crate::resampler::calculate_rms_f32(&raw_samples_buf);
                let peak = crate::resampler::calculate_peak_f32(&raw_samples_buf);
                lm.update(rms, peak);
            }

            let converted = converter.process_input(&raw_samples_buf);
            chunk_accumulator.extend(converted);

            while chunk_accumulator.len() >= samples_per_chunk {
                let chunk_samples: Vec<f32> =
                    chunk_accumulator.drain(..samples_per_chunk).collect();
                let pcm_s16le_bytes = f32_to_pcm_s16le(&chunk_samples);

                let (start_time_ms, end_time_ms) = clock.calculate_chunk_timeline(
                    config.track_id,
                    total_target_samples,
                    chunk_samples.len() as u64,
                    target_sample_rate,
                );

                spool_manager
                    .write_chunk(
                        &config.interview_id,
                        config.track_id,
                        clock.epoch(),
                        chunk_sequence,
                        start_time_ms,
                        end_time_ms,
                        target_sample_rate,
                        1,
                        chunk_samples.len() as u64,
                        AudioFormat::PcmS16Le,
                        &pcm_s16le_bytes,
                    )
                    .with_context(|| {
                        format!("Failed to write chunk seq {} to spool", chunk_sequence)
                    })?;

                total_target_samples += chunk_samples.len() as u64;
                chunk_sequence += 1;
            }
        }

        std::thread::sleep(std::time::Duration::from_millis(15));
    }

    if let Some(ref lm) = level_metrics {
        lm.update(0.0, 0.0);
    }

    // 1. Drain all remaining samples in ring buffer
    raw_samples_buf.clear();
    while let Some(sample) = consumer.try_pop() {
        raw_samples_buf.push(sample);
    }
    if !raw_samples_buf.is_empty() {
        let converted = converter.process_input(&raw_samples_buf);
        chunk_accumulator.extend(converted);
    }

    // 2. Flush converter (stateful phase and incomplete frame remainder)
    let flushed = converter.flush();
    chunk_accumulator.extend(flushed);

    // 3. Flush any full chunks formed after final drain
    while chunk_accumulator.len() >= samples_per_chunk {
        let chunk_samples: Vec<f32> = chunk_accumulator.drain(..samples_per_chunk).collect();
        let pcm_s16le_bytes = f32_to_pcm_s16le(&chunk_samples);

        let (start_time_ms, end_time_ms) = clock.calculate_chunk_timeline(
            config.track_id,
            total_target_samples,
            chunk_samples.len() as u64,
            target_sample_rate,
        );

        spool_manager
            .write_chunk(
                &config.interview_id,
                config.track_id,
                clock.epoch(),
                chunk_sequence,
                start_time_ms,
                end_time_ms,
                target_sample_rate,
                1,
                chunk_samples.len() as u64,
                AudioFormat::PcmS16Le,
                &pcm_s16le_bytes,
            )
            .with_context(|| {
                format!(
                    "Failed to write drained chunk seq {} to spool",
                    chunk_sequence
                )
            })?;

        total_target_samples += chunk_samples.len() as u64;
        chunk_sequence += 1;
    }

    // 4. Flush trailing partial chunk (tail) if any samples remain
    if !chunk_accumulator.is_empty() {
        let tail_samples_len = chunk_accumulator.len() as u64;
        let pcm_s16le_bytes = f32_to_pcm_s16le(&chunk_accumulator);

        let (start_time_ms, end_time_ms) = clock.calculate_chunk_timeline(
            config.track_id,
            total_target_samples,
            tail_samples_len,
            target_sample_rate,
        );

        spool_manager
            .write_chunk(
                &config.interview_id,
                config.track_id,
                clock.epoch(),
                chunk_sequence,
                start_time_ms,
                end_time_ms,
                target_sample_rate,
                1,
                tail_samples_len,
                AudioFormat::PcmS16Le,
                &pcm_s16le_bytes,
            )
            .with_context(|| {
                format!(
                    "Failed to write final partial chunk seq {} to spool",
                    chunk_sequence
                )
            })?;

        total_target_samples += tail_samples_len;
        chunk_sequence += 1;
        chunk_accumulator.clear();
    }

    // 5. Calculate total duration based on exact sample count (NOT full chunk count * chunk_duration)
    let total_duration_ms =
        MonotonicInterviewClock::samples_to_ms(total_target_samples, target_sample_rate);
    let dropped = dropped_samples.load(Ordering::SeqCst);

    let mut gaps = Vec::new();
    if dropped > 0 {
        gaps.push(AudioGap {
            track_id: config.track_id,
            start_time_ms: 0,
            end_time_ms: total_duration_ms,
            reason: format!("Ring buffer overflow: dropped {} samples", dropped),
        });
    }

    spool_manager
        .seal_manifest_extended(
            &config.interview_id,
            config.track_id,
            clock.epoch(),
            chunk_sequence,
            total_duration_ms,
            total_target_samples,
            dropped,
            gaps.clone(),
        )
        .with_context(|| "Failed to seal track manifest")?;

    Ok(CaptureStats {
        interview_id: config.interview_id,
        track_id: config.track_id,
        total_chunks: chunk_sequence,
        total_samples: total_target_samples,
        total_duration_ms,
        dropped_samples: dropped,
        is_sealed: true,
        gaps,
    })
}

enum StreamControlMsg {
    Pause(std::sync::mpsc::Sender<Result<()>>),
    Resume(std::sync::mpsc::Sender<Result<()>>),
    Stop(std::sync::mpsc::Sender<()>),
}

pub struct CaptureHandle {
    stream_ctrl: Option<std::sync::mpsc::Sender<StreamControlMsg>>,
    stream_thread: Option<std::thread::JoinHandle<()>>,
    is_running: Arc<AtomicBool>,
    worker_thread: Option<std::thread::JoinHandle<Result<CaptureStats>>>,
    dropped_samples: Arc<AtomicU64>,
    stream_error: Arc<std::sync::Mutex<Option<String>>>,
    level_metrics: Arc<AudioLevelMetrics>,
}

impl CaptureHandle {
    pub fn audio_levels(&self) -> (f32, f32) {
        if !self.is_running.load(Ordering::Relaxed) {
            return (0.0, 0.0);
        }
        self.level_metrics.get()
    }

    pub fn pause(&self) -> Result<()> {
        if let Some(ref ctrl) = self.stream_ctrl {
            let (tx, rx) = std::sync::mpsc::channel();
            ctrl.send(StreamControlMsg::Pause(tx))
                .map_err(|_| anyhow::anyhow!("Stream thread disconnected"))?;
            rx.recv()
                .map_err(|_| anyhow::anyhow!("Stream thread dropped ack"))??;
        }
        Ok(())
    }

    pub fn resume(&self) -> Result<()> {
        if let Some(ref ctrl) = self.stream_ctrl {
            let (tx, rx) = std::sync::mpsc::channel();
            ctrl.send(StreamControlMsg::Resume(tx))
                .map_err(|_| anyhow::anyhow!("Stream thread disconnected"))?;
            rx.recv()
                .map_err(|_| anyhow::anyhow!("Stream thread dropped ack"))??;
        }
        Ok(())
    }

    pub fn stop(mut self) -> Result<CaptureStats> {
        // 1. Halt callback from incoming stream
        if let Some(ctrl) = self.stream_ctrl.take() {
            let (tx, rx) = std::sync::mpsc::channel();
            let _ = ctrl.send(StreamControlMsg::Stop(tx));
            let _ = rx.recv();
        }
        if let Some(th) = self.stream_thread.take() {
            let _ = th.join();
        }

        // 2. Signal worker to drain and finalize
        self.is_running.store(false, Ordering::SeqCst);

        // 3. Join worker and propagate any I/O / worker errors
        let stats = match self.worker_thread.take() {
            Some(th) => th
                .join()
                .map_err(|_| anyhow::anyhow!("Capture worker thread panicked"))??,
            None => bail!("Capture worker was already joined"),
        };

        // 4. Verify stream error
        if let Ok(mut guard) = self.stream_error.lock() {
            if let Some(err) = guard.take() {
                bail!("Audio input stream error: {}", err);
            }
        }

        Ok(stats)
    }

    pub fn dropped_samples(&self) -> u64 {
        self.dropped_samples.load(Ordering::Relaxed)
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
    clock: impl Into<SharedInterviewClock>,
    chunk_duration_ms: u64,
) -> Result<CaptureHandle> {
    let clock = clock.into();
    let default_config = device
        .default_input_config()
        .context("Failed to get default input config for device")?;

    let sample_rate = default_config.sample_rate().0;
    let channels = default_config.channels();
    let sample_format = default_config.sample_format();

    // Ring buffer size: 2 seconds of multi-channel audio
    let buffer_capacity = (sample_rate as usize) * (channels as usize) * 2;
    let rb = HeapRb::<f32>::new(buffer_capacity.max(4096));
    let (mut producer, consumer) = rb.split();

    let is_running = Arc::new(AtomicBool::new(true));
    let is_running_clone = is_running.clone();

    let dropped_samples = Arc::new(AtomicU64::new(0));
    let dropped_samples_cb = dropped_samples.clone();

    let stream_error = Arc::new(std::sync::Mutex::new(None));
    let stream_err_cb = stream_error.clone();

    let err_fn = move |err| {
        eprintln!("Audio input stream error: {:?}", err);
        if let Ok(mut guard) = stream_err_cb.lock() {
            *guard = Some(format!("{:?}", err));
        }
    };

    let device_clone = device.clone();
    let config_clone = default_config.clone();

    let first_sample_f32 = Arc::new(AtomicBool::new(false));
    let first_sample_f32_cb = first_sample_f32.clone();
    let clock_f32_cb = clock.clone();

    let first_sample_i16 = Arc::new(AtomicBool::new(false));
    let first_sample_i16_cb = first_sample_i16.clone();
    let clock_i16_cb = clock.clone();

    let (init_tx, init_rx) = std::sync::mpsc::channel();
    let (ctrl_tx, ctrl_rx) = std::sync::mpsc::channel::<StreamControlMsg>();

    let stream_thread = std::thread::spawn(move || {
        let stream_res: Result<Stream> = match sample_format {
            cpal::SampleFormat::F32 => device_clone
                .build_input_stream(
                    &config_clone.into(),
                    move |data: &[f32], _: &_| {
                        if !data.is_empty() && !first_sample_f32_cb.swap(true, Ordering::SeqCst) {
                            if clock_f32_cb.track_start_offset_ms(track_id).is_none() {
                                clock_f32_cb.mark_track_start(track_id);
                            }
                        }
                        for &sample in data {
                            if producer.try_push(sample).is_err() {
                                dropped_samples_cb.fetch_add(1, Ordering::Relaxed);
                            }
                        }
                    },
                    err_fn,
                    None,
                )
                .map_err(|e| anyhow::anyhow!("Failed to build f32 input stream: {}", e)),
            cpal::SampleFormat::I16 => device_clone
                .build_input_stream(
                    &config_clone.into(),
                    move |data: &[i16], _: &_| {
                        if !data.is_empty() && !first_sample_i16_cb.swap(true, Ordering::SeqCst) {
                            if clock_i16_cb.track_start_offset_ms(track_id).is_none() {
                                clock_i16_cb.mark_track_start(track_id);
                            }
                        }
                        for &sample in data {
                            let f_sample = sample as f32 / 32768.0;
                            if producer.try_push(f_sample).is_err() {
                                dropped_samples_cb.fetch_add(1, Ordering::Relaxed);
                            }
                        }
                    },
                    err_fn,
                    None,
                )
                .map_err(|e| anyhow::anyhow!("Failed to build i16 input stream: {}", e)),
            _ => Err(anyhow::anyhow!(
                "Unsupported sample format: {:?}",
                sample_format
            )),
        };

        let stream = match stream_res {
            Ok(s) => s,
            Err(e) => {
                let _ = init_tx.send(Err(e));
                return;
            }
        };

        if let Err(e) = stream.play() {
            let _ = init_tx.send(Err(anyhow::anyhow!("Failed to play audio stream: {}", e)));
            return;
        }

        let _ = init_tx.send(Ok(()));

        while let Ok(msg) = ctrl_rx.recv() {
            match msg {
                StreamControlMsg::Pause(ack) => {
                    let res = stream
                        .pause()
                        .map_err(|e| anyhow::anyhow!("Failed to pause stream: {}", e));
                    let _ = ack.send(res);
                }
                StreamControlMsg::Resume(ack) => {
                    let res = stream
                        .play()
                        .map_err(|e| anyhow::anyhow!("Failed to play stream: {}", e));
                    let _ = ack.send(res);
                }
                StreamControlMsg::Stop(ack) => {
                    let _ = stream.pause();
                    drop(stream);
                    let _ = ack.send(());
                    break;
                }
            }
        }
    });

    init_rx.recv().map_err(|_| {
        anyhow::anyhow!("Audio stream initialization thread terminated unexpectedly")
    })??;

    let start_offset_ms = clock.track_start_offset_ms(track_id).unwrap_or(0);
    let config = CaptureWorkerConfig {
        interview_id,
        track_id,
        sample_rate,
        channels,
        chunk_duration_ms,
        start_offset_ms,
    };

    let level_metrics = Arc::new(AudioLevelMetrics::default());
    let level_metrics_worker = level_metrics.clone();
    let dropped_worker = dropped_samples.clone();
    let worker_thread = std::thread::spawn(move || {
        run_capture_worker_extended(
            consumer,
            spool_manager,
            clock,
            config,
            is_running_clone,
            dropped_worker,
            Some(level_metrics_worker),
        )
    });

    Ok(CaptureHandle {
        stream_ctrl: Some(ctrl_tx),
        stream_thread: Some(stream_thread),
        is_running,
        worker_thread: Some(worker_thread),
        dropped_samples,
        stream_error,
        level_metrics,
    })
}
