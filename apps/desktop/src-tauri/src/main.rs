#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend_runtime;
mod commands;
mod uploader;

use backend_runtime::{get_backend_status, restart_backend, PythonServices};
use commands::{
    get_active_session, get_audio_levels, get_session_manifests, get_system_config,
    get_upload_progress, list_audio_devices, pause_capture, resume_capture, resume_spool_upload,
    start_capture, stop_capture, verify_spool, AppState,
};
use tauri::Manager;

fn main() {
    tauri::Builder::default()
        .manage(AppState::default())
        .manage(PythonServices::default())
        .setup(|app| {
            // Канонические каталоги данных нужны и самому процессу Tauri: команды
            // читают NEBULA_CAPTURE_SPOOL_DIR. Без этого путь резолвился
            // относительно cwd, а у запущенного из Finder приложения cwd = "/"
            // (только для чтения) — запись падала с EROFS (os error 30).
            if let Err(error) = backend_runtime::apply_runtime_environment(app.handle()) {
                eprintln!("Не удалось подготовить каталоги данных приложения: {error}");
            }
            // Ошибка старта backend не должна валить приложение: паника внутри
            // setup приводит к abort() через ObjC-границу и SIGABRT без диагностики.
            // Состояние публикуется в PythonServices и показывается в UI.
            app.state::<PythonServices>().start(app.handle());
            Ok(())
        })
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
            verify_spool,
            resume_spool_upload,
            get_session_manifests,
            get_backend_status,
            restart_backend
        ])
        .build(tauri::generate_context!())
        .expect("error while building nebula desktop application")
        .run(|app, event| {
            if matches!(
                event,
                tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
            ) {
                app.state::<PythonServices>().stop();
            }
        });
}
