use audio_capture::{
    calculate_rms_f32, calculate_rms_i16, f32_to_pcm_s16le, multi_channel_to_mono_f32,
    resample_linear_f32,
};

#[test]
fn test_rms_calculation() {
    assert_eq!(calculate_rms_f32(&[]), 0.0);

    // DC signal of 0.5 -> RMS = 0.5
    let dc = vec![0.5f32; 100];
    assert!((calculate_rms_f32(&dc) - 0.5).abs() < 1e-4);

    let dc_i16 = vec![16384i16; 100]; // 16384 / 32768 = 0.5
    assert!((calculate_rms_i16(&dc_i16) - 0.5).abs() < 1e-4);
}

#[test]
fn test_f32_to_pcm_s16le_conversion() {
    let samples = vec![0.0f32, 1.0f32, -1.0f32];
    let bytes = f32_to_pcm_s16le(&samples);
    assert_eq!(bytes.len(), 6);

    // 0.0 -> 0
    let v0 = i16::from_le_bytes([bytes[0], bytes[1]]);
    assert_eq!(v0, 0);

    // 1.0 -> 32767
    let v1 = i16::from_le_bytes([bytes[2], bytes[3]]);
    assert_eq!(v1, 32767);

    // -1.0 -> -32767
    let v2 = i16::from_le_bytes([bytes[4], bytes[5]]);
    assert_eq!(v2, -32767);
}

#[test]
fn test_stereo_to_mono() {
    // Stereo: [L0, R0, L1, R1]
    let stereo = vec![1.0f32, 0.0f32, 0.4f32, 0.6f32];
    let mono = multi_channel_to_mono_f32(&stereo, 2);
    assert_eq!(mono.len(), 2);
    assert!((mono[0] - 0.5).abs() < 1e-5);
    assert!((mono[1] - 0.5).abs() < 1e-5);
}

#[test]
fn test_resample_linear() {
    // 48kHz down to 16kHz (ratio 3:1)
    let input = vec![1.0f32, 1.0f32, 1.0f32, 2.0f32, 2.0f32, 2.0f32];
    let output = resample_linear_f32(&input, 48000, 16000);
    assert_eq!(output.len(), 2);
    assert!((output[0] - 1.0).abs() < 1e-4);
    assert!((output[1] - 2.0).abs() < 1e-4);
}
