use std::sync::Arc;
use tempfile::tempdir;

use audio_capture::{AudioSpoolManager, SharedInterviewClock, TrackType};

#[test]
fn test_r10_zero_sample_track_seals_clean_manifest_with_directory_creation() {
    let dir = tempdir().unwrap();
    let spool = Arc::new(AudioSpoolManager::new(dir.path().to_path_buf()));
    let interview_id = "inv-empty-track";

    // Track dir does NOT exist yet
    let track_dir = spool.get_track_dir(interview_id, TrackType::Candidate);
    assert!(!track_dir.exists());

    // Stop before first callback / zero samples
    let manifest = spool
        .seal_manifest_extended(interview_id, TrackType::Candidate, 1, 0, 0, 0, 0, vec![])
        .expect("seal_manifest_extended must succeed even for zero samples");

    // Directory must now exist
    assert!(track_dir.exists());
    assert!(manifest.is_sealed);
    assert_eq!(manifest.total_chunks, 0);
    assert_eq!(manifest.total_samples, 0);
    assert_eq!(manifest.total_duration_ms, 0);

    // Manifest must be loadable and verified
    let loaded = spool
        .load_manifest(interview_id, TrackType::Candidate)
        .expect("manifest must load");
    assert_eq!(loaded.total_chunks, 0);
    assert_eq!(loaded.total_samples, 0);

    let report = spool
        .verify_track_spool(interview_id, TrackType::Candidate)
        .expect("verification should pass");
    assert!(report.is_valid);
    assert_eq!(report.total_chunks_found, 0);
}

#[test]
fn test_r11_shared_clock_pause_resume_synchronizes_epoch_across_workers() {
    let clock = SharedInterviewClock::new(1);
    assert_eq!(clock.epoch(), 1);

    // Clone clock as if passed into worker
    let worker_clock = clock.clone();
    assert_eq!(worker_clock.epoch(), 1);

    // Simulate session pause & resume in Tauri command
    clock.pause();
    assert!(clock.is_paused());
    assert!(worker_clock.is_paused());

    let new_epoch = clock.resume();
    assert_eq!(new_epoch, 2);
    assert!(!clock.is_paused());

    // Worker's view of epoch MUST immediately be updated!
    assert_eq!(worker_clock.epoch(), 2);
    assert!(!worker_clock.is_paused());
}

#[test]
fn test_r12_track_start_offset_not_faked_before_samples() {
    let clock = SharedInterviewClock::new(1);

    // Before any callback, track offset is None
    assert_eq!(clock.track_start_offset_ms(TrackType::Interviewer), None);
    assert_eq!(clock.track_start_offset_ms(TrackType::Candidate), None);

    // First device callback arrives at t = 50ms
    clock.set_track_start_offset_ms(TrackType::Interviewer, 50);

    // Second device callback arrives at t = 120ms (delayed startup)
    clock.set_track_start_offset_ms(TrackType::Candidate, 120);

    assert_eq!(
        clock.track_start_offset_ms(TrackType::Interviewer),
        Some(50)
    );
    assert_eq!(clock.track_start_offset_ms(TrackType::Candidate), Some(120));

    // Calculate chunk timelines reflect real sample start, not arbitrary pre-open timestamp
    let (start_inv, end_inv) =
        clock.calculate_chunk_timeline(TrackType::Interviewer, 0, 16000, 16000);
    assert_eq!(start_inv, 50);
    assert_eq!(end_inv, 1050);

    let (start_cand, end_cand) =
        clock.calculate_chunk_timeline(TrackType::Candidate, 0, 16000, 16000);
    assert_eq!(start_cand, 120);
    assert_eq!(end_cand, 1120);
}
