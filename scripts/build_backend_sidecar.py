"""Build the Python API/worker executable expected by the Tauri bundle."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDECAR_DIR = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"
PYINSTALLER_SPEC = ROOT / "packaging" / "nebula-python.spec"


def target_triple() -> str:
    configured = os.environ.get("TAURI_ENV_TARGET_TRIPLE") or os.environ.get("NEBULA_PYTHON_TARGET")
    if configured:
        return configured

    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin" and machine in {"arm64", "aarch64"}:
        return "aarch64-apple-darwin"
    if system == "darwin" and machine in {"x86_64", "amd64"}:
        return "x86_64-apple-darwin"
    if system == "windows" and machine in {"amd64", "x86_64"}:
        return "x86_64-pc-windows-msvc"
    if system == "linux" and machine in {"arm64", "aarch64"}:
        return "aarch64-unknown-linux-gnu"
    if system == "linux" and machine in {"amd64", "x86_64"}:
        return "x86_64-unknown-linux-gnu"
    raise RuntimeError(f"Unsupported host for embedded Python sidecar: {system}/{machine}")


def main() -> None:
    suffix = ".exe" if os.name == "nt" else ""
    target = target_triple()
    subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(ROOT),
            "pyinstaller",
            "--clean",
            "--noconfirm",
            str(PYINSTALLER_SPEC),
        ],
        cwd=ROOT,
        check=True,
    )

    built = ROOT / "dist" / f"nebula-python{suffix}"
    if not built.is_file():
        raise FileNotFoundError(f"PyInstaller did not produce {built}")

    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    destination = SIDECAR_DIR / f"nebula-python-{target}{suffix}"
    shutil.copy2(built, destination)
    if os.name != "nt":
        destination.chmod(destination.stat().st_mode | 0o111)
    print(f"Built embedded Python sidecar: {destination}")


if __name__ == "__main__":
    main()
