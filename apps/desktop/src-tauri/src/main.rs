#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;

use commands::{
    get_audio_levels, list_audio_devices, start_capture, stop_capture, verify_spool, AppState,
};

fn main() {
    tauri::Builder::default()
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            list_audio_devices,
            start_capture,
            stop_capture,
            get_audio_levels,
            verify_spool
        ])
        .run(tauri::generate_context!())
        .expect("error while running nebula desktop application");
}
