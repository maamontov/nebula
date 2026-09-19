"""Build the Python API/worker executable expected by the Tauri bundle.

Собирает PyInstaller-дистрибутив в режиме onedir и раскладывает его в каталог,
который Tauri кладёт в ресурсы приложения (`apps/desktop/src-tauri/sidecar/`).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDECAR_ROOT = ROOT / "apps" / "desktop" / "src-tauri" / "sidecar"
PYINSTALLER_SPEC = ROOT / "packaging" / "nebula-python.spec"
# Прежний onefile-бинарник: больше не собирается, но мог остаться от старых сборок.
LEGACY_BINARIES_DIR = ROOT / "apps" / "desktop" / "src-tauri" / "binaries"

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

def build_sidecar() -> None:
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

    built_dir = ROOT / "dist" / "nebula-python"
    built_binary = built_dir / f"nebula-python{suffix}"
    if not built_binary.is_file():
        raise FileNotFoundError(f"PyInstaller did not produce {built_binary}")

    destination = SIDECAR_ROOT / f"nebula-python-{target}"
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(built_dir, destination)

    staged_binary = destination / f"nebula-python{suffix}"
    if os.name != "nt":
        staged_binary.chmod(staged_binary.stat().st_mode | 0o111)

    _remove_legacy_onefile(target)

    print(f"Built embedded Python sidecar (onedir): {destination}")

def _remove_legacy_onefile(target: str) -> None:
    """Удаляет onefile-бинарник прошлых сборок, чтобы не оставалось двух источников истины."""
    suffix = ".exe" if os.name == "nt" else ""
    legacy = LEGACY_BINARIES_DIR / f"nebula-python-{target}{suffix}"
    if legacy.is_file():
        legacy.unlink()
        print(f"Removed legacy onefile sidecar: {legacy}")

def main() -> None:
    build_sidecar()

if __name__ == "__main__":
    main()
