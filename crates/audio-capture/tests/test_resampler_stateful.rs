use audio_capture::resampler::StatefulAudioConverter;
use std::f32::consts::PI;

#[test]
fn test_stateful_converter_chunk_invariance() {
    // 48kHz stereo to 16kHz mono (downsampling ratio 3:1)
    let from_rate = 48000u32;
    let to_rate = 16000u32;
    let channels = 2u16;

    // Generate 1 second of stereo 440Hz sine wave (96,000 floats total = 48,000 frames)
    let total_frames = 48000;
    let mut input = Vec::with_capacity(total_frames * 2);
    for i in 0..total_frames {
        let t = i as f32 / from_rate as f32;
        let left = (2.0 * PI * 440.0 * t).sin() * 0.8;
        let right = (2.0 * PI * 440.0 * t).sin() * 0.4;
        input.push(left);
        input.push(right);
    }

    // Run 1: Process all at once
    let mut conv1 = StatefulAudioConverter::new(channels, from_rate, to_rate);
    let mut out1 = conv1.process_input(&input);
    out1.extend(conv1.flush());

    // Run 2: Process in small fixed chunks of 128 floats (not necessarily frame-aligned)
    let mut conv2 = StatefulAudioConverter::new(channels, from_rate, to_rate);
    let mut out2 = Vec::new();
    for chunk in input.chunks(128) {
        out2.extend(conv2.process_input(chunk));
    }
    out2.extend(conv2.flush());

    // Run 3: Process in irregular odd-sized chunks (33, 77, 251...)
    let mut conv3 = StatefulAudioConverter::new(channels, from_rate, to_rate);
    let mut out3 = Vec::new();
    let chunk_sizes = [33, 77, 105, 251, 513, 999];
    let mut offset = 0;
    let mut size_idx = 0;
    while offset < input.len() {
        let sz = chunk_sizes[size_idx % chunk_sizes.len()];
        let end = (offset + sz).min(input.len());
        out3.extend(conv3.process_input(&input[offset..end]));
        offset = end;
        size_idx += 1;
    }
    out3.extend(conv3.flush());

    // Invariant 1: Exactly 16,000 samples for 1 second of 16kHz audio
    assert_eq!(
        out1.len(),
        16000,
        "Full block output must match exactly 16000 samples"
    );
    assert_eq!(
        out2.len(),
        16000,
        "Chunked output must match exactly 16000 samples"
    );
    assert_eq!(
        out3.len(),
        16000,
        "Irregular chunked output must match exactly 16000 samples"
    );

    // Invariant 2: Sample-by-sample difference between chunked and single block must be negligible (< 1e-4)
    for i in 0..16000 {
        let diff2 = (out1[i] - out2[i]).abs();
        let diff3 = (out1[i] - out3[i]).abs();
        assert!(
            diff2 < 1e-4,
            "Sample mismatch at index {}: out1={} out2={} diff={}",
            i,
            out1[i],
            out2[i],
            diff2
        );
        assert!(
            diff3 < 1e-4,
            "Sample mismatch at index {}: out1={} out3={} diff={}",
            i,
            out1[i],
            out3[i],
            diff3
        );
    }
}

#[test]
fn test_stateful_converter_inter_channel_frame_remainder() {
    // Stereo stream where odd number of samples arrive (breaks frame alignment)
    let channels = 2u16;
    let rate = 16000u32;
    let mut conv = StatefulAudioConverter::new(channels, rate, rate);

    // Feed 3 samples: Frame 0 (1.0, 0.0), Frame 1 Left (0.6)
    let chunk1 = vec![1.0f32, 0.0f32, 0.6f32];
    let out1 = conv.process_input(&chunk1);
    // Should output only Frame 0 (average = 0.5) because Frame 1 is incomplete
    assert_eq!(out1.len(), 1);
    assert!((out1[0] - 0.5).abs() < 1e-5);

    // Feed next 3 samples: Frame 1 Right (0.4), Frame 2 (0.8, 0.2)
    let chunk2 = vec![0.4f32, 0.8f32, 0.2f32];
    let out2 = conv.process_input(&chunk2);
    // Should output Frame 1 (0.6 + 0.4 / 2 = 0.5) and Frame 2 (0.8 + 0.2 / 2 = 0.5)
    assert_eq!(out2.len(), 2);
    assert!((out2[0] - 0.5).abs() < 1e-5);
    assert!((out2[1] - 0.5).abs() < 1e-5);

    // Flush should yield empty since all frames were complete
    let flush_out = conv.flush();
    assert!(flush_out.is_empty());
}

#[test]
fn test_stateful_converter_flush_odd_trailing_sample() {
    let channels = 2u16;
    let rate = 16000u32;
    let mut conv = StatefulAudioConverter::new(channels, rate, rate);

    // Feed 1 sample of stereo
    let chunk = vec![0.8f32];
    let out = conv.process_input(&chunk);
    assert!(out.is_empty());

    // Flush should gracefully handle incomplete trailing frame without panic
    let flushed = conv.flush();
    assert_eq!(flushed.len(), 1);
    assert!((flushed[0] - 0.8).abs() < 1e-5);
}
