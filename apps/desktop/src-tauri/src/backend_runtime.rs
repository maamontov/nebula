use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::Manager;

const BACKEND_HOST: &str = "127.0.0.1";
const BACKEND_PORT: u16 = 17843;

/// Готовность embedded backend ожидается не бесконечно: PyInstaller-sidecar при
/// первом запуске распаковывает рантайм, поэтому запас должен быть заметно больше
/// холодного старта (~4 с). Значение переопределяется через `NEBULA_BACKEND_READY_TIMEOUT_SECS`.
const DEFAULT_READY_TIMEOUT_SECS: u64 = 60;
const MIN_READY_TIMEOUT_SECS: u64 = 5;

/// Состояние встроенного backend, доступное UI.
///
/// Раньше неудачный старт приводил к панике в `setup` и аварийному завершению
/// приложения (SIGABRT) без внятного сообщения. Теперь старт всегда завершается
/// успешно на уровне Tauri, а результат сообщается здесь и отображается в UI.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BackendStatusInfo {
    /// `starting` | `ready` | `failed` | `stopped`
    pub state: String,
    /// `embedded` | `reused` | `external`
    pub mode: Option<String>,
    pub backend_url: String,
    pub message: Option<String>,
    pub hint: Option<String>,
    pub log_path: Option<String>,
}

impl BackendStatusInfo {
    fn base(state: &str) -> Self {
        Self {
            state: state.to_string(),
            mode: None,
            backend_url: format!("http://{BACKEND_HOST}:{BACKEND_PORT}"),
            message: None,
            hint: None,
            log_path: None,
        }
    }

    pub fn starting() -> Self {
        let mut status = Self::base("starting");
        status.message = Some("Запуск встроенного backend Nebula…".to_string());
        status
    }

    pub fn ready(mode: &str, message: Option<String>) -> Self {
        let mut status = Self::base("ready");
        status.mode = Some(mode.to_string());
        status.message = message;
        status
    }

    pub fn failed(message: String, hint: Option<String>, log_path: Option<PathBuf>) -> Self {
        let mut status = Self::base("failed");
        status.message = Some(message);
        status.hint = hint;
        status.log_path = log_path.map(|path| path.display().to_string());
        status
    }

    pub fn stopped() -> Self {
        Self::base("stopped")
    }
}

#[derive(Default)]
pub struct PythonServices {
    children: Mutex<Vec<Child>>,
    status: Mutex<Option<BackendStatusInfo>>,
}

struct RuntimeDirs {
    data: PathBuf,
    capture_spool: PathBuf,
    backend_spool: PathBuf,
    backups: PathBuf,
    logs: PathBuf,
}

impl RuntimeDirs {
    fn from_app(app: &tauri::AppHandle) -> Result<Self, String> {
        let data = app
            .path()
            .app_data_dir()
            .map_err(|error| {
                format!("Не удалось определить директорию данных приложения: {error}")
            })?
            .join("data");

        let dirs = Self {
            capture_spool: data.join("spool_capture"),
            backend_spool: data.join("spool_backend"),
            backups: data.join("backups"),
            logs: data.join("logs"),
            data,
        };

        for directory in [
            &dirs.data,
            &dirs.capture_spool,
            &dirs.backend_spool,
            &dirs.backups,
            &dirs.logs,
        ] {
            std::fs::create_dir_all(directory)
                .map_err(|error| format!("Не удалось создать {}: {error}", directory.display()))?;
        }
        Ok(dirs)
    }
}

impl PythonServices {
    /// Текущее состояние backend для UI.
    pub fn status(&self) -> BackendStatusInfo {
        self.status
            .lock()
            .ok()
            .and_then(|guard| guard.clone())
            .unwrap_or_else(BackendStatusInfo::starting)
    }

    fn set_status(&self, status: BackendStatusInfo) {
        if let Ok(mut guard) = self.status.lock() {
            *guard = Some(status);
        }
    }

    /// Запускает backend, никогда не паникуя: любая проблема становится статусом `failed`.
    pub fn start(&self, app: &tauri::AppHandle) {
        self.set_status(BackendStatusInfo::starting());

        if external_backend_enabled() {
            self.set_status(BackendStatusInfo::ready(
                "external",
                Some(format!(
                    "Backend обслуживается внешним процессом (NEBULA_EXTERNAL_BACKEND=1), {BACKEND_HOST}:{BACKEND_PORT}."
                )),
            ));
            return;
        }

        if port_is_open() {
            // Порт занят: если это уже работающий Nebula, переиспользуем его вместо
            // аварийного завершения; если чужой сервис — сообщаем понятную причину.
            if nebula_backend_detected() {
                self.set_status(BackendStatusInfo::ready(
                    "reused",
                    Some(format!(
                        "Найден уже запущенный backend Nebula на {BACKEND_HOST}:{BACKEND_PORT}. Собственные процессы не запускались."
                    )),
                ));
            } else {
                self.set_status(BackendStatusInfo::failed(
                    format!(
                        "Порт {BACKEND_PORT} занят другим приложением, которое не является Nebula backend."
                    ),
                    Some(format!(
                        "Освободите порт {BACKEND_PORT} (остановите сторонний сервис) и нажмите «Повторить»."
                    )),
                    None,
                ));
            }
            return;
        }

        let dirs = match RuntimeDirs::from_app(app) {
            Ok(dirs) => dirs,
            Err(error) => {
                self.set_status(BackendStatusInfo::failed(error, None, None));
                return;
            }
        };

        let backend_log = dirs.logs.join("backend.log");
        let worker_log = dirs.logs.join("worker.log");

        let binary = match resolve_sidecar_binary(app) {
            Ok(binary) => binary,
            Err(error) => {
                self.set_status(BackendStatusInfo::failed(
                    error,
                    Some(
                        "Соберите sidecar командой «npm run build:sidecar» (внутри apps/desktop)."
                            .to_string(),
                    ),
                    Some(backend_log),
                ));
                return;
            }
        };

        let env = runtime_environment(&dirs);
        let timeout = ready_timeout();

        let mut api = match spawn_service(&binary, "api", &env, &backend_log) {
            Ok(child) => child,
            Err(error) => {
                self.set_status(BackendStatusInfo::failed(
                    format!("Не удалось запустить embedded backend: {error}"),
                    Some("Проверьте права на файл sidecar и свободное место на диске.".to_string()),
                    Some(backend_log),
                ));
                return;
            }
        };

        if let Err(error) = wait_for_backend(&mut api, timeout) {
            terminate_child(&mut api);
            self.set_status(BackendStatusInfo::failed(
                error,
                Some(format!(
                    "Увеличьте таймаут через NEBULA_BACKEND_READY_TIMEOUT_SECS (сейчас {} с) или проверьте лог backend.",
                    timeout.as_secs()
                )),
                Some(backend_log),
            ));
            return;
        }

        let worker = match spawn_service(&binary, "worker", &env, &worker_log) {
            Ok(child) => child,
            Err(error) => {
                terminate_child(&mut api);
                self.set_status(BackendStatusInfo::failed(
                    format!("Не удалось запустить embedded worker: {error}"),
                    None,
                    Some(worker_log),
                ));
                return;
            }
        };

        match self.children.lock() {
            Ok(mut children) => {
                children.push(api);
                children.push(worker);
            }
            Err(_) => {
                let mut api = api;
                let mut worker = worker;
                terminate_child(&mut api);
                terminate_child(&mut worker);
                self.set_status(BackendStatusInfo::failed(
                    "Не удалось зафиксировать процессы embedded backend.".to_string(),
                    None,
                    Some(backend_log),
                ));
                return;
            }
        }

        self.set_status(BackendStatusInfo::ready(
            "embedded",
            Some(format!(
                "Backend запущен приложением ({BACKEND_HOST}:{BACKEND_PORT})."
            )),
        ));
    }

    /// Повторный запуск по требованию UI. Используется кнопкой «Повторить».
    pub fn restart(&self, app: &tauri::AppHandle) -> BackendStatusInfo {
        self.stop();
        self.start(app);
        self.status()
    }

    pub fn stop(&self) {
        if let Ok(mut children) = self.children.lock() {
            for child in &mut *children {
                terminate_child(child);
            }
            children.clear();
        }
        self.set_status(BackendStatusInfo::stopped());
    }
}

#[tauri::command]
pub fn get_backend_status(services: tauri::State<'_, PythonServices>) -> BackendStatusInfo {
    services.status()
}

#[tauri::command]
pub fn restart_backend(
    app: tauri::AppHandle,
    services: tauri::State<'_, PythonServices>,
) -> BackendStatusInfo {
    services.restart(&app)
}

fn runtime_environment(dirs: &RuntimeDirs) -> Vec<(&'static str, String)> {
    vec![
        ("NEBULA_DATA_DIR", dirs.data.display().to_string()),
        (
            "NEBULA_CAPTURE_SPOOL_DIR",
            dirs.capture_spool.display().to_string(),
        ),
        (
            "NEBULA_BACKEND_SPOOL_DIR",
            dirs.backend_spool.display().to_string(),
        ),
        ("NEBULA_SPOOL_DIR", dirs.backend_spool.display().to_string()),
        (
            "NEBULA_DB_PATH",
            dirs.data.join("nebula.db").display().to_string(),
        ),
        ("NEBULA_BACKUP_DIR", dirs.backups.display().to_string()),
        ("NEBULA_PARENT_PIPE", "1".to_string()),
        (
            "NEBULA_BACKEND_URL",
            format!("http://{BACKEND_HOST}:{BACKEND_PORT}"),
        ),
        ("PYTHONUNBUFFERED", "1".to_string()),
    ]
}

fn spawn_service(
    binary: &PathBuf,
    mode: &str,
    environment: &[(&str, String)],
    log_path: &PathBuf,
) -> Result<Child, String> {
    let log = open_log(log_path)?;
    let log_stderr = log
        .try_clone()
        .map_err(|error| format!("Не удалось открыть лог {}: {error}", log_path.display()))?;

    let mut command = Command::new(binary);
    command
        .arg(mode)
        .stdin(Stdio::piped())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_stderr));
    for (key, value) in environment {
        command.env(key, value);
    }

    command
        .spawn()
        .map_err(|error| format!("{} ({})", error, binary.display()))
}

fn open_log(path: &PathBuf) -> Result<File, String> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| format!("Не удалось открыть лог {}: {error}", path.display()))
}

/// Бюджет ожидания готовности backend. Читается из окружения, чтобы можно было
/// поднять его на медленных машинах без пересборки приложения.
fn ready_timeout() -> Duration {
    Duration::from_secs(parse_ready_timeout(
        std::env::var("NEBULA_BACKEND_READY_TIMEOUT_SECS")
            .ok()
            .as_deref(),
    ))
}

fn parse_ready_timeout(raw: Option<&str>) -> u64 {
    raw.and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_READY_TIMEOUT_SECS)
        .max(MIN_READY_TIMEOUT_SECS)
}

fn wait_for_backend(child: &mut Child, timeout: Duration) -> Result<(), String> {
    let address: SocketAddr = format!("{BACKEND_HOST}:{BACKEND_PORT}")
        .parse()
        .map_err(|error| format!("Некорректный адрес embedded backend: {error}"))?;

    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("Не удалось проверить embedded backend: {error}"))?
        {
            return Err(format!(
                "Embedded backend завершился до готовности (status: {status})."
            ));
        }

        if backend_health_ok(address) {
            return Ok(());
        }

        if Instant::now() >= deadline {
            return Err(format!(
                "Embedded backend не стал доступен за {} секунд.",
                timeout.as_secs()
            ));
        }
        thread::sleep(Duration::from_millis(150));
    }
}

fn backend_health_ok(address: SocketAddr) -> bool {
    matches!(http_get(address, "/healthz"), Some((200, _)))
}

/// Проверяет, что на порту действительно Nebula backend, а не сторонний сервис.
/// Опорный признак — не только `/healthz`, но и структура `/api/v1/system/config`.
fn nebula_backend_detected() -> bool {
    let address: SocketAddr = match format!("{BACKEND_HOST}:{BACKEND_PORT}").parse() {
        Ok(address) => address,
        Err(_) => return false,
    };

    match http_get(address, "/api/v1/system/config") {
        Some((200, body)) => serde_json::from_str::<serde_json::Value>(&body)
            .map(|value| {
                value.get("backend_spool_dir").is_some() && value.get("db_path").is_some()
            })
            .unwrap_or(false),
        _ => false,
    }
}

fn port_is_open() -> bool {
    let address: SocketAddr = match format!("{BACKEND_HOST}:{BACKEND_PORT}").parse() {
        Ok(address) => address,
        Err(_) => return false,
    };
    TcpStream::connect_timeout(&address, Duration::from_millis(200)).is_ok()
}

/// Минимальный HTTP GET на std-сокете: без блокирующего HTTP-клиента и лишних
/// зависимостей, только для проверок готовности и опознания backend.
fn http_get(address: SocketAddr, path: &str) -> Option<(u16, String)> {
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(300)).ok()?;
    stream
        .set_read_timeout(Some(Duration::from_millis(700)))
        .ok()?;
    stream
        .set_write_timeout(Some(Duration::from_millis(500)))
        .ok()?;

    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {BACKEND_HOST}:{BACKEND_PORT}\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).ok()?;

    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;

    let code = response
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())?;
    let body = response
        .split_once("\r\n\r\n")
        .map(|(_, body)| body.to_string())
        .unwrap_or_default();

    Some((code, body))
}

fn external_backend_enabled() -> bool {
    matches!(
        std::env::var("NEBULA_EXTERNAL_BACKEND").as_deref(),
        Ok("1") | Ok("true") | Ok("TRUE")
    )
}

/// Кандидаты пути к sidecar. Поддерживается как каталог PyInstaller `onedir`
/// (основной режим: без распаковки на каждый старт), так и прежний `onefile`.
fn resolve_sidecar_binary(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let candidates = sidecar_candidates(app)?;

    candidates
        .iter()
        .find(|path| path.is_file())
        .cloned()
        .ok_or_else(|| {
            let listed = candidates
                .iter()
                .map(|path| format!("  • {}", path.display()))
                .collect::<Vec<_>>()
                .join("\n");
            format!("Embedded Python sidecar не найден. Проверены пути:\n{listed}")
        })
}

fn sidecar_candidates(app: &tauri::AppHandle) -> Result<Vec<PathBuf>, String> {
    let target = std::env::var("NEBULA_PYTHON_TARGET").unwrap_or_else(|_| current_target());
    let suffix = if cfg!(windows) { ".exe" } else { "" };
    let dir_name = format!("nebula-python-{target}");
    let exe_name = format!("nebula-python{suffix}");
    let legacy_name = format!("nebula-python-{target}{suffix}");

    let current_exe = std::env::current_exe()
        .map_err(|error| format!("Не удалось определить executable Tauri: {error}"))?;
    let exe_dir = current_exe
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));

    let mut candidates = Vec::new();

    // onedir рядом с исполняемым файлом (macOS: Contents/MacOS/...)
    candidates.push(exe_dir.join(&dir_name).join(&exe_name));

    // onedir в ресурсах собранного бандла
    if let Ok(resource_dir) = app.path().resource_dir() {
        candidates.push(resource_dir.join("sidecar").join(&dir_name).join(&exe_name));
        candidates.push(resource_dir.join(&dir_name).join(&exe_name));
    }

    // onedir из рабочей копии репозитория (dev-режим)
    candidates.push(manifest_dir.join("sidecar").join(&dir_name).join(&exe_name));
    candidates.push(manifest_dir.join("binaries").join(&dir_name).join(&exe_name));

    // Совместимость с прежним onefile-вариантом
    candidates.push(exe_dir.join(&legacy_name));
    candidates.push(exe_dir.join(&exe_name));
    candidates.push(manifest_dir.join("binaries").join(&legacy_name));

    Ok(candidates)
}

fn current_target() -> String {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => "aarch64-apple-darwin".to_string(),
        ("macos", "x86_64") => "x86_64-apple-darwin".to_string(),
        ("windows", "x86_64") => "x86_64-pc-windows-msvc".to_string(),
        ("linux", "aarch64") => "aarch64-unknown-linux-gnu".to_string(),
        ("linux", "x86_64") => "x86_64-unknown-linux-gnu".to_string(),
        (os, arch) => format!("{arch}-{os}"),
    }
}

fn terminate_child(child: &mut Child) {
    if child.try_wait().ok().flatten().is_none() {
        let _ = child.kill();
    }
    let _ = child.wait();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_timeout_defaults_and_clamps() {
        assert_eq!(parse_ready_timeout(None), DEFAULT_READY_TIMEOUT_SECS);
        assert_eq!(parse_ready_timeout(Some("")), DEFAULT_READY_TIMEOUT_SECS);
        assert_eq!(parse_ready_timeout(Some("abc")), DEFAULT_READY_TIMEOUT_SECS);
        assert_eq!(parse_ready_timeout(Some("0")), DEFAULT_READY_TIMEOUT_SECS);
        assert_eq!(parse_ready_timeout(Some("120")), 120);
        assert_eq!(parse_ready_timeout(Some(" 90 ")), 90);
        assert_eq!(parse_ready_timeout(Some("1")), MIN_READY_TIMEOUT_SECS);
    }

    #[test]
    fn status_serialization_round_trip() {
        let status = BackendStatusInfo::failed(
            "Порт 17843 занят".to_string(),
            Some("освободите порт".to_string()),
            Some(PathBuf::from("/tmp/backend.log")),
        );
        let json = serde_json::to_string(&status).expect("serialize");
        let restored: BackendStatusInfo = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(restored, status);
        assert_eq!(restored.state, "failed");
        assert_eq!(restored.log_path.as_deref(), Some("/tmp/backend.log"));
    }

    #[test]
    fn ready_status_reports_mode_and_url() {
        let status = BackendStatusInfo::ready("reused", None);
        assert_eq!(status.state, "ready");
        assert_eq!(status.mode.as_deref(), Some("reused"));
        assert_eq!(status.backend_url, "http://127.0.0.1:17843");
    }
}
