use audio_capture::{
    capture::{run_capture_worker, CaptureWorkerConfig},
    clock::MonotonicInterviewClock,
    spool::AudioSpoolManager,
    TrackType,
};
use ringbuf::traits::{Producer, Split};
use ringbuf::HeapRb;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use tempfile::tempdir;

#[test]
fn test_ringbuffer_overflow_surfaced_in_stats_and_manifest() {
    let tmp = tempdir().unwrap();
    let spool = Arc::new(AudioSpoolManager::new(tmp.path()));
    let clock = MonotonicInterviewClock::new(1);

    // Small ring buffer to trigger overflow easily
    let rb = HeapRb::<f32>::new(512);
    let (mut producer, consumer) = rb.split();

    let is_running = Arc::new(AtomicBool::new(true));
    let dropped_samples = Arc::new(AtomicU64::new(0));

    let config = CaptureWorkerConfig {
        interview_id: "test-overflow-1".into(),
        track_id: TrackType::Interviewer,
        sample_rate: 16000,
        channels: 1,
        chunk_duration_ms: 1000,
        start_offset_ms: 0,
    };

    // Simulate producer pushing more samples than ringbuffer capacity
    let mut dropped_count = 0u64;
    for _ in 0..2000 {
        if producer.try_push(0.1).is_err() {
            dropped_count += 1;
        }
    }
    assert!(dropped_count > 0, "Producer must have experienced overflow");
    dropped_samples.store(dropped_count, Ordering::SeqCst);

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

    // Let worker drain and stop
    std::thread::sleep(std::time::Duration::from_millis(50));
    is_running.store(false, Ordering::SeqCst);

    let stats = worker_handle
        .join()
        .unwrap()
        .expect("Worker should complete with overflow recorded");
    assert_eq!(stats.dropped_samples, dropped_count);
    assert!(
        !stats.gaps.is_empty(),
        "Manifest must record AudioGap for overflow"
    );
    assert!(stats.gaps[0].reason.contains("Ring buffer overflow"));
}

#[test]
fn test_disk_failure_causes_worker_error() {
    // A file cannot be used as a parent directory on any supported platform.
    let temp_dir = tempfile::tempdir().unwrap();
    let blocking_file = temp_dir.path().join("not-a-directory");
    std::fs::write(&blocking_file, b"blocked").unwrap();
    let invalid_path = blocking_file.join("invalid_spool");
    let spool = Arc::new(AudioSpoolManager::new(&invalid_path));
    let clock = MonotonicInterviewClock::new(1);

    let rb = HeapRb::<f32>::new(4096);
    let (mut producer, consumer) = rb.split();

    let is_running = Arc::new(AtomicBool::new(true));
    let dropped_samples = Arc::new(AtomicU64::new(0));

    let config = CaptureWorkerConfig {
        interview_id: "test-fail-disk".into(),
        track_id: TrackType::Interviewer,
        sample_rate: 16000,
        channels: 1,
        chunk_duration_ms: 1000,
        start_offset_ms: 0,
    };

    let is_running_clone = is_running.clone();
    let dropped_clone = dropped_samples.clone();

    let worker_handle = std::thread::spawn(move || {
        run_capture_worker(
            consumer,
            spool,
            clock,
            config,
            is_running_clone,
            dropped_clone,
        )
    });

    // Push enough samples for at least one chunk (16,000 samples)
    for _ in 0..16000 {
        let _ = producer.try_push(0.5);
    }

    // Give worker time to attempt write and fail
    std::thread::sleep(std::time::Duration::from_millis(50));
    is_running.store(false, Ordering::SeqCst);

    let result = worker_handle.join().unwrap();
    assert!(
        result.is_err(),
        "Worker must return Err on disk failure, not silent success!"
    );
}
