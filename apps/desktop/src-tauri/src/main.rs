#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod uploader;

use commands::{
    get_active_session, get_audio_levels, get_system_config, get_upload_progress,
    list_audio_devices, pause_capture, resume_capture, start_capture, stop_capture,
    verify_spool, AppState,
};

fn main() {
    tauri::Builder::default()
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            list_audio_devices,
            start_capture,
            stop_capture,
            pause_capture,
            resume_capture,
            get_audio_levels,
            get_upload_progress,
            get_active_session,
            get_system_config,
            verify_spool
        ])
        .run(tauri::generate_context!())
        .expect("error while running nebula desktop application");
}
