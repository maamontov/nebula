use audio_capture::MonotonicInterviewClock;

#[test]
fn test_clock_samples_to_ms() {
    assert_eq!(MonotonicInterviewClock::samples_to_ms(16000, 16000), 1000);
    assert_eq!(MonotonicInterviewClock::samples_to_ms(48000, 48000), 1000);
    assert_eq!(MonotonicInterviewClock::samples_to_ms(32000, 16000), 2000);
    assert_eq!(MonotonicInterviewClock::samples_to_ms(0, 16000), 0);
}

#[test]
fn test_clock_drift_and_skew() {
    // 16000 samples @ 16kHz = 1000ms audio time
    // If wall clock was 1050ms, drift is 1000 - 1050 = -50ms (audio lags)
    let drift = MonotonicInterviewClock::calculate_drift_ms(1050, 16000, 16000);
    assert_eq!(drift, -50);

    // Track A: 16000 samples @ 16kHz = 1000ms
    // Track B: 48000 samples @ 48kHz = 1000ms
    let skew = MonotonicInterviewClock::calculate_inter_track_skew_ms(16000, 16000, 48000, 48000);
    assert_eq!(skew, 0);

    // Track A has 16800 samples (1050ms), Track B has 48000 (1000ms)
    let skew_diff =
        MonotonicInterviewClock::calculate_inter_track_skew_ms(16800, 16000, 48000, 48000);
    assert_eq!(skew_diff, 50);
}

#[test]
fn test_clock_pause_resume() {
    let mut clock = MonotonicInterviewClock::new(1);
    assert_eq!(clock.epoch(), 1);
    assert!(!clock.is_paused());

    std::thread::sleep(std::time::Duration::from_millis(20));
    let elapsed_before_pause = clock.pause();
    assert!(clock.is_paused());
    assert!(elapsed_before_pause >= 15);

    std::thread::sleep(std::time::Duration::from_millis(30));
    // During pause, elapsed_ms should stay frozen
    assert_eq!(clock.elapsed_ms(), elapsed_before_pause);

    let new_epoch = clock.resume();
    assert_eq!(new_epoch, 2);
    assert_eq!(clock.epoch(), 2);
    assert!(!clock.is_paused());

    std::thread::sleep(std::time::Duration::from_millis(20));
    assert!(clock.elapsed_ms() > elapsed_before_pause);
}
