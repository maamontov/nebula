use std::sync::Arc;
use tempfile::tempdir;
use audio_capture::{
    simulate_track_recording, AudioSpoolManager,
    SyntheticTrackConfig, TrackType,
};

#[test]
fn test_simulated_one_hour_recording() {
    let tmp = tempdir().unwrap();
    let spool = Arc::new(AudioSpoolManager::new(tmp.path()));

    let interviewer_config = SyntheticTrackConfig {
        track_id: TrackType::Interviewer,
        frequency_hz: 440.0,
        sample_rate: 16000,
        drift_ppm: 0.0,
        simulated_dropout_at_ms: None,
        dropout_duration_ms: 0,
    };

    let candidate_config = SyntheticTrackConfig {
        track_id: TrackType::Candidate,
        frequency_hz: 880.0,
        sample_rate: 16000,
        drift_ppm: 25.0, // +25 ppm clock drift
        simulated_dropout_at_ms: None,
        dropout_duration_ms: 0,
    };

    // Simulate 3600 seconds (1 hour) with 2-second chunks (1800 chunks per track)
    let res_i = simulate_track_recording("hour-sim", &interviewer_config, spool.clone(), 3600, 2000).unwrap();
    let res_c = simulate_track_recording("hour-sim", &candidate_config, spool.clone(), 3600, 2000).unwrap();

    assert_eq!(res_i.total_chunks, 1800);
    assert_eq!(res_c.total_chunks, 1800);

    // Verify spool integrity for interviewer
    let report_i = spool.verify_track_spool("hour-sim", TrackType::Interviewer).unwrap();
    assert!(report_i.is_valid);
    assert_eq!(report_i.verified_chunks, 1800);
    assert!(report_i.is_sealed);

    // Verify spool integrity for candidate
    let report_c = spool.verify_track_spool("hour-sim", TrackType::Candidate).unwrap();
    assert!(report_c.is_valid);
    assert_eq!(report_c.verified_chunks, 1800);
    assert!(report_c.is_sealed);

    // Measured drift over 1 hour with +25ppm should be ~90ms (within target threshold <= 200ms)
    println!("Candidate drift over 1 hour: {} ms", res_c.calculated_drift_ms);
    assert!(res_c.calculated_drift_ms.abs() < 200);
}
