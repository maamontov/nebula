/// Calculates Root-Mean-Square (RMS) level of audio samples in range [0.0, 1.0].
pub fn calculate_rms_f32(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum_sq: f32 = samples.iter().map(|&s| s * s).sum();
    (sum_sq / samples.len() as f32).sqrt()
}

/// Calculates RMS level for PCM 16-bit signed integer samples.
pub fn calculate_rms_i16(samples: &[i16]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum_sq: f64 = samples.iter().map(|&s| {
        let norm = s as f64 / 32768.0;
        norm * norm
    }).sum();
    ((sum_sq / samples.len() as f64).sqrt()) as f32
}

/// Converts f32 normalized samples [-1.0, 1.0] to PCM 16-bit signed little-endian bytes.
pub fn f32_to_pcm_s16le(samples: &[f32]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(samples.len() * 2);
    for &sample in samples {
        // Clamp to [-1.0, 1.0] to prevent integer overflow
        let clamped = sample.clamp(-1.0, 1.0);
        let val_i16 = (clamped * 32767.0) as i16;
        bytes.extend_from_slice(&val_i16.to_le_bytes());
    }
    bytes
}

/// Converts multi-channel f32 audio into single-channel (mono) by averaging channels.
pub fn multi_channel_to_mono_f32(samples: &[f32], channels: u16) -> Vec<f32> {
    if channels <= 1 {
        return samples.to_vec();
    }
    let ch = channels as usize;
    let frame_count = samples.len() / ch;
    let mut mono = Vec::with_capacity(frame_count);

    for i in 0..frame_count {
        let mut sum = 0.0;
        for c in 0..ch {
            sum += samples[i * ch + c];
        }
        mono.push(sum / ch as f32);
    }
    mono
}

/// Downsamples mono f32 audio using linear interpolation.
pub fn resample_linear_f32(samples: &[f32], from_rate: u32, to_rate: u32) -> Vec<f32> {
    if from_rate == to_rate || samples.is_empty() {
        return samples.to_vec();
    }
    let ratio = from_rate as f64 / to_rate as f64;
    let out_len = ((samples.len() as f64) / ratio).floor() as usize;
    let mut out = Vec::with_capacity(out_len);

    for i in 0..out_len {
        let src_pos = i as f64 * ratio;
        let idx = src_pos.floor() as usize;
        let frac = (src_pos - idx as f64) as f32;

        if idx + 1 < samples.len() {
            let s1 = samples[idx];
            let s2 = samples[idx + 1];
            out.push(s1 + frac * (s2 - s1));
        } else if idx < samples.len() {
            out.push(samples[idx]);
        }
    }
    out
}

/// Stateful audio stream converter:
/// 1. Preserves inter-channel frame remainders across chunk boundaries (no lost channel frames).
/// 2. Downsamples mono audio to target rate using linear interpolation while maintaining
///    continuous phase across chunk boundaries (no phase jitter, no drift error accumulation).
#[derive(Debug, Clone)]
pub struct StatefulAudioConverter {
    channels: u16,
    from_rate: u32,
    to_rate: u32,
    channel_remainder: Vec<f32>,
    mono_history: Vec<f32>,
    phase: f64,
    ratio: f64,
}

impl StatefulAudioConverter {
    pub fn new(channels: u16, from_rate: u32, to_rate: u32) -> Self {
        let ch = if channels == 0 { 1 } else { channels };
        let ratio = from_rate as f64 / to_rate as f64;
        Self {
            channels: ch,
            from_rate,
            to_rate,
            channel_remainder: Vec::with_capacity(ch as usize),
            mono_history: Vec::with_capacity(4096),
            phase: 0.0,
            ratio,
        }
    }

    pub fn process_input(&mut self, input: &[f32]) -> Vec<f32> {
        if input.is_empty() {
            return Vec::new();
        }

        let ch = self.channels as usize;
        let mut mono_samples = Vec::new();

        if ch <= 1 {
            mono_samples.extend_from_slice(input);
        } else {
            let mut cursor = 0;
            // First complete any previous channel remainder
            if !self.channel_remainder.is_empty() {
                let needed = ch - self.channel_remainder.len();
                if input.len() >= needed {
                    self.channel_remainder.extend_from_slice(&input[..needed]);
                    cursor = needed;
                    let sum: f32 = self.channel_remainder.iter().sum();
                    mono_samples.push(sum / ch as f32);
                    self.channel_remainder.clear();
                } else {
                    self.channel_remainder.extend_from_slice(input);
                    return Vec::new();
                }
            }

            let remaining_input = &input[cursor..];
            let full_frames = remaining_input.len() / ch;
            let frame_samples_len = full_frames * ch;

            for frame in remaining_input[..frame_samples_len].chunks_exact(ch) {
                let sum: f32 = frame.iter().sum();
                mono_samples.push(sum / ch as f32);
            }

            if frame_samples_len < remaining_input.len() {
                self.channel_remainder.extend_from_slice(&remaining_input[frame_samples_len..]);
            }
        }

        if self.from_rate == self.to_rate {
            return mono_samples;
        }

        self.mono_history.extend(mono_samples);

        let step = self.ratio;
        let mut out = Vec::new();
        let mut pos = self.phase;

        while {
            let idx = pos.floor() as usize;
            idx + 1 < self.mono_history.len()
        } {
            let idx = pos.floor() as usize;
            let frac = (pos - idx as f64) as f32;
            let s0 = self.mono_history[idx];
            let s1 = self.mono_history[idx + 1];
            out.push(s0 + frac * (s1 - s0));
            pos += step;
        }

        let last_idx = pos.floor() as usize;
        if last_idx > 0 {
            let drain_count = last_idx.min(self.mono_history.len());
            self.mono_history.drain(0..drain_count);
            self.phase = pos - drain_count as f64;
        } else {
            self.phase = pos;
        }

        out
    }

    pub fn flush(&mut self) -> Vec<f32> {
        let mut out = Vec::new();

        // Flush trailing incomplete channel remainder
        if !self.channel_remainder.is_empty() {
            let sum: f32 = self.channel_remainder.iter().sum();
            let avg = sum / self.channel_remainder.len() as f32;
            self.channel_remainder.clear();
            if self.from_rate == self.to_rate {
                out.push(avg);
                return out;
            }
            self.mono_history.push(avg);
        }

        if self.from_rate == self.to_rate {
            let remaining = std::mem::take(&mut self.mono_history);
            out.extend(remaining);
            return out;
        }

        let step = self.ratio;
        let mut pos = self.phase;

        // Drain any pairs remaining
        while {
            let idx = pos.floor() as usize;
            idx + 1 < self.mono_history.len()
        } {
            let idx = pos.floor() as usize;
            let frac = (pos - idx as f64) as f32;
            let s0 = self.mono_history[idx];
            let s1 = self.mono_history[idx + 1];
            out.push(s0 + frac * (s1 - s0));
            pos += step;
        }

        // Emit final trailing sample if pos hasn't exceeded the last sample index
        let last_idx = pos.floor() as usize;
        if last_idx < self.mono_history.len() {
            out.push(self.mono_history[last_idx]);
        }

        self.mono_history.clear();
        self.phase = 0.0;
        out
    }
}

