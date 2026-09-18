use audio_capture::{
    capture::{run_capture_worker, CaptureWorkerConfig},
    clock::MonotonicInterviewClock,
    spool::AudioSpoolManager,
    TrackType,
};
use ringbuf::traits::{Producer, Split};
use ringbuf::HeapRb;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tempfile::tempdir;

#[test]
fn test_stop_before_full_chunk_flushes_tail_and_exact_duration() {
    let tmp = tempdir().unwrap();
    let spool = Arc::new(AudioSpoolManager::new(tmp.path()));
    let clock = MonotonicInterviewClock::new(1);

    // Setup ringbuffer
    let rb = HeapRb::<f32>::new(16000 * 2);
    let (mut producer, consumer) = rb.split();

    let is_running = Arc::new(AtomicBool::new(true));
    let dropped_samples = Arc::new(std::sync::atomic::AtomicU64::new(0));

    let config = CaptureWorkerConfig {
        interview_id: "test-lifecycle-1".into(),
        track_id: TrackType::Interviewer,
        sample_rate: 16000,
        channels: 1,
        chunk_duration_ms: 1000, // 16000 samples per chunk
        start_offset_ms: 0,
    };

    // Spawn worker
    let is_running_clone = is_running.clone();
    let dropped_clone = dropped_samples.clone();
    let spool_clone = spool.clone();
    let clock_clone = clock.clone();

    let worker_handle = std::thread::spawn(move || {
        run_capture_worker(
            consumer,
            spool_clone,
            clock_clone,
            config,
            is_running_clone,
            dropped_clone,
        )
    });

    // Push only 5,600 samples (350 ms at 16kHz) - less than a full 1000ms chunk!
    for i in 0..5600 {
        let val = (i as f32 % 100.0) / 100.0;
        producer.try_push(val).unwrap();
    }

    // Stop recording
    is_running.store(false, Ordering::SeqCst);
    let stats = worker_handle
        .join()
        .unwrap()
        .expect("Worker should finish successfully");

    // Assertions
    assert_eq!(
        stats.total_chunks, 1,
        "Must flush remaining tail as 1 chunk"
    );
    assert_eq!(stats.total_samples, 5600, "Must count exact total samples");
    assert_eq!(
        stats.total_duration_ms, 350,
        "Total duration must be 350ms, NOT 1000ms!"
    );
    assert_eq!(stats.dropped_samples, 0);

    // Verify spool
    let report = spool
        .verify_track_spool("test-lifecycle-1", TrackType::Interviewer)
        .unwrap();
    assert!(report.is_valid);
    assert_eq!(report.total_chunks_found, 1);
    assert_eq!(report.verified_chunks, 1);
    assert!(report.is_sealed);
}

#[test]
fn test_stop_after_partial_second_chunk() {
    let tmp = tempdir().unwrap();
    let spool = Arc::new(AudioSpoolManager::new(tmp.path()));
    let clock = MonotonicInterviewClock::new(1);

    // Setup ringbuffer
    let rb = HeapRb::<f32>::new(48000 * 2);
    let (mut producer, consumer) = rb.split();

    let is_running = Arc::new(AtomicBool::new(true));
    let dropped_samples = Arc::new(std::sync::atomic::AtomicU64::new(0));

    let config = CaptureWorkerConfig {
        interview_id: "test-lifecycle-2".into(),
        track_id: TrackType::Candidate,
        sample_rate: 16000,
        channels: 1,
        chunk_duration_ms: 1000, // 16,000 samples per chunk
        start_offset_ms: 20,
    };

    let is_running_clone = is_running.clone();
    let dropped_clone = dropped_samples.clone();
    let spool_clone = spool.clone();
    let clock_clone = clock.clone();

    let worker_handle = std::thread::spawn(move || {
        run_capture_worker(
            consumer,
            spool_clone,
            clock_clone,
            config,
            is_running_clone,
            dropped_clone,
        )
    });

    // Push 24,000 samples (1.5 seconds = 1 full chunk of 16k + 1 partial chunk of 8k)
    for i in 0..24000 {
        producer.try_push((i as f32 % 50.0) / 50.0).unwrap();
    }

    // Stop recording
    is_running.store(false, Ordering::SeqCst);
    let stats = worker_handle
        .join()
        .unwrap()
        .expect("Worker should finish successfully");

    assert_eq!(stats.total_chunks, 2);
    assert_eq!(stats.total_samples, 24000);
    assert_eq!(
        stats.total_duration_ms, 1500,
        "Total duration must be 1500ms, NOT 2000ms!"
    );

    let report = spool
        .verify_track_spool("test-lifecycle-2", TrackType::Candidate)
        .unwrap();
    assert!(report.is_valid);
    assert_eq!(report.total_chunks_found, 2);
}
