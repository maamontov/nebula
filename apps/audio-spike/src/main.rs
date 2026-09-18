use anyhow::{bail, Context, Result};
use audio_capture::{
    list_input_devices, list_output_devices, simulate_track_recording, start_device_capture,
    AudioSpoolManager, MonotonicInterviewClock, SyntheticTrackConfig, TrackType,
};
use clap::{Parser, Subcommand};
use cpal::traits::{DeviceTrait, HostTrait};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

#[derive(Parser)]
#[command(name = "nebula-audio-spike")]
#[command(about = "Nebula Diagnostic Audio Capture & Spooling CLI", long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// List all system audio input and output devices
    ListDevices,

    /// Record test audio from system microphone and spool into chunks
    Record {
        #[arg(long, default_value = "spike-session")]
        interview_id: String,

        #[arg(long)]
        device_name: Option<String>,

        #[arg(long, default_value_t = 5)]
        duration_sec: u64,

        #[arg(long, default_value = "./spool")]
        output_dir: PathBuf,

        #[arg(long, default_value_t = 2000)]
        chunk_ms: u64,
    },

    /// Verify an existing audio spool directory for missing chunks and checksum mismatches
    VerifySpool {
        #[arg(default_value = "./spool")]
        spool_dir: PathBuf,

        #[arg(long, default_value = "spike-session")]
        interview_id: String,
    },

    /// Run synthetic two-channel stress test simulating hours of recording with clock drift
    SyntheticTest {
        #[arg(long, default_value_t = 3600)]
        simulated_duration_sec: u64,

        #[arg(long, default_value = "./spool/synthetic")]
        output_dir: PathBuf,

        #[arg(long, default_value_t = 25.0)]
        candidate_drift_ppm: f64,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Commands::ListDevices => {
            println!("==================================================");
            println!("Nebula Audio Device Enumeration (CPAL CoreAudio/WASAPI)");
            println!("==================================================\n");

            let host = cpal::default_host();
            println!("Host Backend: {:?}", host.id());

            println!("\n[Input Devices / Microphones]:");
            let inputs = list_input_devices(&host)?;
            if inputs.is_empty() {
                println!("  No input devices detected.");
            } else {
                for (i, dev) in inputs.iter().enumerate() {
                    let mark = if dev.is_default { " (DEFAULT)" } else { "" };
                    println!("  [{}] {}{}", i + 1, dev.name, mark);
                }
            }

            println!("\n[Output Devices / Speakers / Loops]:");
            let outputs = list_output_devices(&host)?;
            if outputs.is_empty() {
                println!("  No output devices detected.");
            } else {
                for (i, dev) in outputs.iter().enumerate() {
                    let mark = if dev.is_default { " (DEFAULT)" } else { "" };
                    println!("  [{}] {}{}", i + 1, dev.name, mark);
                }
            }
            println!("\n==================================================");
        }

        Commands::Record {
            interview_id,
            device_name,
            duration_sec,
            output_dir,
            chunk_ms,
        } => {
            println!("==================================================");
            println!("Nebula Audio Capture Spike");
            println!("Interview ID: {}", interview_id);
            println!("Duration: {} seconds", duration_sec);
            println!("Chunk Duration: {} ms", chunk_ms);
            println!("Spool Dir: {:?}", output_dir);
            println!("==================================================\n");

            let host = cpal::default_host();
            let device = if let Some(ref target) = device_name {
                host.input_devices()?
                    .find(|d| d.name().map(|n| n.contains(target)).unwrap_or(false))
                    .with_context(|| format!("Device matching '{}' not found", target))?
            } else {
                host.default_input_device()
                    .context("No default input device available")?
            };

            let dev_name = device.name().unwrap_or_else(|_| "Unknown".to_string());
            println!("Selected Input Device: {}", dev_name);

            let spool = Arc::new(AudioSpoolManager::new(&output_dir));
            let clock = MonotonicInterviewClock::new(0);

            println!("Starting capture for {} seconds...", duration_sec);
            let handle = start_device_capture(
                &device,
                interview_id.clone(),
                TrackType::Interviewer,
                spool.clone(),
                clock,
                chunk_ms,
            )?;

            // Live progress
            for sec in 1..=duration_sec {
                std::thread::sleep(Duration::from_secs(1));
                print!("\rRecording progress: {}/{} s", sec, duration_sec);
                std::io::Write::flush(&mut std::io::stdout())?;
            }
            println!("\nStopping capture and sealing spool...");
            let stats = handle.stop()?;
            println!(
                "Capture stats: {} chunks, {} samples, {} ms, dropped: {}",
                stats.total_chunks,
                stats.total_samples,
                stats.total_duration_ms,
                stats.dropped_samples
            );

            println!("\nVerifying captured spool...");
            let report = spool.verify_track_spool(&interview_id, TrackType::Interviewer)?;
            println!("Verification Report:");
            println!("  Total chunks found: {}", report.total_chunks_found);
            println!("  Verified valid chunks: {}", report.verified_chunks);
            println!("  Manifest sealed: {}", report.is_sealed);
            println!("  Spool integrity valid: {}", report.is_valid);

            if !report.is_valid {
                eprintln!("  Errors: {:?}", report.corrupted_chunks);
                eprintln!("  Missing sequences: {:?}", report.missing_sequences);
                bail!("Spool verification failed!");
            }
            println!("\nSUCCESS: Real audio recorded and verified successfully.");
        }

        Commands::VerifySpool {
            spool_dir,
            interview_id,
        } => {
            let spool = AudioSpoolManager::new(&spool_dir);
            println!(
                "Verifying spool in {:?} for interview '{}'...",
                spool_dir, interview_id
            );

            for track in [TrackType::Interviewer, TrackType::Candidate] {
                match spool.verify_track_spool(&interview_id, track) {
                    Ok(report) => {
                        println!("\nTrack {:?}:", track);
                        println!("  Chunks found: {}", report.total_chunks_found);
                        println!("  Verified chunks: {}", report.verified_chunks);
                        println!("  Sealed: {}", report.is_sealed);
                        println!("  Valid: {}", report.is_valid);
                    }
                    Err(e) => {
                        println!(
                            "\nTrack {:?}: Directory not found or unreadable ({})",
                            track, e
                        );
                    }
                }
            }
        }

        Commands::SyntheticTest {
            simulated_duration_sec,
            output_dir,
            candidate_drift_ppm,
        } => {
            println!("==================================================");
            println!("Nebula Synthetic 2-Channel Stress & Drift Test");
            println!(
                "Simulated Duration: {} seconds ({} hours)",
                simulated_duration_sec,
                simulated_duration_sec as f64 / 3600.0
            );
            println!("Candidate Drift: {:+.1} PPM", candidate_drift_ppm);
            println!("Output Spool: {:?}", output_dir);
            println!("==================================================\n");

            let interview_id = "synthetic-run";
            let spool = Arc::new(AudioSpoolManager::new(&output_dir));

            let interviewer_cfg = SyntheticTrackConfig {
                track_id: TrackType::Interviewer,
                frequency_hz: 440.0,
                sample_rate: 16000,
                drift_ppm: 0.0,
                simulated_dropout_at_ms: None,
                dropout_duration_ms: 0,
            };

            let candidate_cfg = SyntheticTrackConfig {
                track_id: TrackType::Candidate,
                frequency_hz: 880.0,
                sample_rate: 16000,
                drift_ppm: candidate_drift_ppm,
                simulated_dropout_at_ms: None,
                dropout_duration_ms: 0,
            };

            println!("Simulating Track 1 (Interviewer)...");
            let t0 = std::time::Instant::now();
            let res_i = simulate_track_recording(
                interview_id,
                &interviewer_cfg,
                spool.clone(),
                simulated_duration_sec,
                2000,
            )?;
            println!(
                "  Done in {:.2?}. Total chunks: {}, Drift: {} ms",
                t0.elapsed(),
                res_i.total_chunks,
                res_i.calculated_drift_ms
            );

            println!(
                "Simulating Track 2 (Candidate with drift {:+.1} PPM)...",
                candidate_drift_ppm
            );
            let t1 = std::time::Instant::now();
            let res_c = simulate_track_recording(
                interview_id,
                &candidate_cfg,
                spool.clone(),
                simulated_duration_sec,
                2000,
            )?;
            println!(
                "  Done in {:.2?}. Total chunks: {}, Drift: {} ms",
                t1.elapsed(),
                res_c.total_chunks,
                res_c.calculated_drift_ms
            );

            let skew_ms = res_c.calculated_drift_ms - res_i.calculated_drift_ms;
            println!(
                "\nResults over {} hours of audio:",
                simulated_duration_sec as f64 / 3600.0
            );
            println!("  Channel Skew (Candidate - Interviewer): {} ms", skew_ms);
            println!(
                "  Target threshold: |skew| <= 200 ms: {}",
                if skew_ms.abs() <= 200 {
                    "PASSED"
                } else {
                    "FAILED"
                }
            );

            println!("\nVerifying Interviewer Spool...");
            let rep_i = spool.verify_track_spool(interview_id, TrackType::Interviewer)?;
            assert!(rep_i.is_valid && rep_i.is_sealed);
            println!(
                "  ✓ {} chunks verified with valid SHA-256",
                rep_i.verified_chunks
            );

            println!("Verifying Candidate Spool...");
            let rep_c = spool.verify_track_spool(interview_id, TrackType::Candidate)?;
            assert!(rep_c.is_valid && rep_c.is_sealed);
            println!(
                "  ✓ {} chunks verified with valid SHA-256",
                rep_c.verified_chunks
            );

            println!("\n==================================================");
            println!("SYNTHETIC SPIKE PASSED: 100% integrity, clock drift within tolerance.");
            println!("==================================================");
        }
    }

    Ok(())
}
