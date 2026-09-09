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
