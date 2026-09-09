use std::fs;
use tempfile::tempdir;
use audio_capture::{AudioFormat, AudioSpoolManager, TrackType};

#[test]
fn test_atomic_spool_write_and_verification() {
    let tmp = tempdir().unwrap();
    let spool = AudioSpoolManager::new(tmp.path());

    let payload1 = vec![0x11, 0x22, 0x33, 0x44];
    let payload2 = vec![0x55, 0x66, 0x77, 0x88];

    // Write chunk 0
    let meta0 = spool.write_chunk(
        "test-inv",
        TrackType::Interviewer,
        0,
        0,
        0,
        1000,
        16000,
        1,
        16000,
        AudioFormat::PcmS16Le,
        &payload1,
    ).unwrap();

    assert_eq!(meta0.sequence, 0);
    assert_eq!(meta0.size_bytes, 4);

    // Write chunk 1
    spool.write_chunk(
        "test-inv",
        TrackType::Interviewer,
        0,
        1,
        1000,
        2000,
        16000,
        1,
        16000,
        AudioFormat::PcmS16Le,
        &payload2,
    ).unwrap();

    // Seal manifest
    spool.seal_manifest("test-inv", TrackType::Interviewer, 0, 2, 2000).unwrap();

    // Verify spool
    let report = spool.verify_track_spool("test-inv", TrackType::Interviewer).unwrap();
    assert!(report.is_valid);
    assert_eq!(report.total_chunks_found, 2);
    assert_eq!(report.verified_chunks, 2);
    assert!(report.is_sealed);
    assert!(report.missing_sequences.is_empty());
    assert!(report.corrupted_chunks.is_empty());
}

#[test]
fn test_corrupted_chunk_detected() {
    let tmp = tempdir().unwrap();
    let spool = AudioSpoolManager::new(tmp.path());

    let payload = vec![0xAA, 0xBB, 0xCC, 0xDD];
    spool.write_chunk(
        "test-inv",
        TrackType::Candidate,
        0,
        0,
        0,
        1000,
        16000,
        1,
        16000,
        AudioFormat::PcmS16Le,
        &payload,
    ).unwrap();

    // Corrupt the chunk file
    let chunk_path = spool.get_track_dir("test-inv", TrackType::Candidate).join("00000000.chunk");
    fs::write(&chunk_path, b"CORRUPTED_BYTES").unwrap();

    let report = spool.verify_track_spool("test-inv", TrackType::Candidate).unwrap();
    assert!(!report.is_valid);
    assert_eq!(report.verified_chunks, 0);
    assert_eq!(report.corrupted_chunks.len(), 1);
    assert!(report.corrupted_chunks[0].1.contains("Hash mismatch"));
}

#[test]
fn test_missing_sequence_gap_detected() {
    let tmp = tempdir().unwrap();
    let spool = AudioSpoolManager::new(tmp.path());

    let payload = vec![1, 2, 3];
    // Write chunk 0 and chunk 2, skip chunk 1
    spool.write_chunk("test-inv", TrackType::Interviewer, 0, 0, 0, 1000, 16000, 1, 16000, AudioFormat::PcmS16Le, &payload).unwrap();
    spool.write_chunk("test-inv", TrackType::Interviewer, 0, 2, 2000, 3000, 16000, 1, 16000, AudioFormat::PcmS16Le, &payload).unwrap();

    let report = spool.verify_track_spool("test-inv", TrackType::Interviewer).unwrap();
    assert!(!report.is_valid);
    assert_eq!(report.missing_sequences, vec![1]);
}
