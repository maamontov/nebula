use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use tauri::Manager;

const BACKEND_HOST: &str = "127.0.0.1";
const BACKEND_PORT: u16 = 8000;

#[derive(Default)]
pub struct PythonServices {
    children: Mutex<Vec<Child>>,
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
    pub fn start(&self, app: &tauri::AppHandle) -> Result<(), String> {
        if backend_is_reachable() {
            return Err(format!(
                "Порт {BACKEND_PORT} уже занят. Остановите другой экземпляр Nebula/backend перед запуском приложения."
            ));
        }

        let dirs = RuntimeDirs::from_app(app)?;
        let binary = resolve_sidecar_binary(app)?;
        let env = runtime_environment(&dirs);

        let mut api = spawn_service(&binary, "api", &env, &dirs.logs.join("backend.log"))?;
        if let Err(error) = wait_for_backend(&mut api) {
            terminate_child(&mut api);
            return Err(error);
        }

        let worker = match spawn_service(&binary, "worker", &env, &dirs.logs.join("worker.log")) {
            Ok(worker) => worker,
            Err(error) => {
                terminate_child(&mut api);
                return Err(format!("Не удалось запустить embedded worker: {error}"));
            }
        };

        let mut children = self
            .children
            .lock()
            .map_err(|_| "Не удалось получить lock процессов embedded backend".to_string())?;
        children.push(api);
        children.push(worker);
        Ok(())
    }

    pub fn stop(&self) {
        let Ok(mut children) = self.children.lock() else {
            return;
        };
        for child in &mut *children {
            terminate_child(child);
        }
        children.clear();
    }
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

fn wait_for_backend(child: &mut Child) -> Result<(), String> {
    let address: SocketAddr = format!("{BACKEND_HOST}:{BACKEND_PORT}")
        .parse()
        .map_err(|error| format!("Некорректный адрес embedded backend: {error}"))?;

    for _ in 0..100 {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("Не удалось проверить embedded backend: {error}"))?
        {
            return Err(format!(
                "Embedded FastAPI завершился до готовности (status: {status}). Проверьте data/logs/backend.log."
            ));
        }

        if let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(150)) {
            stream
                .set_read_timeout(Some(Duration::from_millis(250)))
                .ok();
            stream
                .set_write_timeout(Some(Duration::from_millis(250)))
                .ok();
            let request = format!(
                "GET /healthz HTTP/1.1\r\nHost: {BACKEND_HOST}:{BACKEND_PORT}\r\nConnection: close\r\n\r\n"
            );
            if stream.write_all(request.as_bytes()).is_ok() {
                let mut response = String::new();
                if stream.read_to_string(&mut response).is_ok()
                    && response.starts_with("HTTP/1.1 200")
                {
                    return Ok(());
                }
            }
        }
        thread::sleep(Duration::from_millis(100));
    }

    Err(
        "Embedded FastAPI не стал доступен за 10 секунд. Проверьте data/logs/backend.log."
            .to_string(),
    )
}

fn backend_is_reachable() -> bool {
    let address: SocketAddr = match format!("{BACKEND_HOST}:{BACKEND_PORT}").parse() {
        Ok(address) => address,
        Err(_) => return false,
    };
    TcpStream::connect_timeout(&address, Duration::from_millis(100)).is_ok()
}

fn resolve_sidecar_binary(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let target = std::env::var("NEBULA_PYTHON_TARGET").unwrap_or_else(|_| current_target());
    let suffix = if cfg!(windows) { ".exe" } else { "" };
    let filename = format!("nebula-python-{target}{suffix}");
    let bundled_filename = format!("nebula-python{suffix}");

    let current_exe = std::env::current_exe()
        .map_err(|error| format!("Не удалось определить executable Tauri: {error}"))?;
    let manifest_binary = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("binaries")
        .join(&filename);
    let resource_dir = app.path().resource_dir().ok();

    let mut candidates = vec![
        current_exe.with_file_name(&filename),
        current_exe.with_file_name(&bundled_filename),
        manifest_binary,
    ];
    if let Some(resource_dir) = resource_dir {
        candidates.push(resource_dir.join(&filename));
        candidates.push(resource_dir.join(&bundled_filename));
    }

    candidates
        .into_iter()
        .find(|path| path.is_file())
        .ok_or_else(|| {
            format!(
                "Embedded Python sidecar не найден. Ожидался {}. Выполните npm run build:sidecar.",
                filename
            )
        })
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
