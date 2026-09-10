use audio_capture::{MonotonicInterviewClock, TrackType};

#[test]
fn test_clock_track_start_offset_and_skew() {
    let mut clock = MonotonicInterviewClock::new(1);

    // Mark interviewer starting at offset 0
    clock.set_track_start_offset_ms(TrackType::Interviewer, 0);
    // Mark candidate starting 45ms later due to OS device initialization
    clock.set_track_start_offset_ms(TrackType::Candidate, 45);

    assert_eq!(clock.track_start_offset_ms(TrackType::Interviewer), Some(0));
    assert_eq!(clock.track_start_offset_ms(TrackType::Candidate), Some(45));
    assert_eq!(clock.initial_skew_ms(), Some(-45)); // Candidate started 45ms behind

    // Interviewer produced 16,000 samples @ 16kHz (1000ms audio)
    // Absolute timeline for Interviewer chunk 0: [0ms, 1000ms]
    let (inv_start, inv_end) = clock.calculate_chunk_timeline(TrackType::Interviewer, 0, 16000, 16000);
    assert_eq!(inv_start, 0);
    assert_eq!(inv_end, 1000);

    // Candidate produced 16,000 samples @ 16kHz (1000ms audio)
    // Absolute timeline for Candidate chunk 0: [45ms, 1045ms]
    let (cand_start, cand_end) = clock.calculate_chunk_timeline(TrackType::Candidate, 0, 16000, 16000);
    assert_eq!(cand_start, 45);
    assert_eq!(cand_end, 1045);

    // Absolute inter-track skew considering start offset + sample count:
    // Interviewer: 0 + 1000ms = 1000ms
    // Candidate: 45 + 1000ms = 1045ms
    // Skew: 1000 - 1045 = -45ms
    let skew = clock.calculate_timeline_skew_ms(16000, 16000, 16000, 16000);
    assert_eq!(skew, -45);
}
