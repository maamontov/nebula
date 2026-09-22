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
    # symlinks=True обязателен: PyInstaller на macOS отдаёт Python.framework с
    # симлинками (Versions/Current -> 3.12, Python -> Versions/Current/Python,
    # _internal/Python -> Python.framework/Versions/3.12/Python). При копировании
    # по умолчанию (symlinks=False) каждый симлинк разворачивается в полную копию
    # 7.4 МБ dylib, и сайдкар раздувается с ~32 МБ до ~54 МБ без пользы.
    shutil.copytree(built_dir, destination, symlinks=True)
    if target.endswith("apple-darwin"):
        _flatten_python_framework(destination)
        _assert_no_symlinks(destination)

    staged_binary = destination / f"nebula-python{suffix}"
    if os.name != "nt":
        staged_binary.chmod(staged_binary.stat().st_mode | 0o111)

    _remove_legacy_onefile(target)

    print(f"Built embedded Python sidecar (onedir): {destination}")

def _flatten_python_framework(destination: Path) -> None:
    """Схлопывает Python.framework в единственный плоский dylib `_internal/Python`.

    PyInstaller раскладывает на macOS и `_internal/Python`, и полный
    `Python.framework`, связанные симлинками. Tauri-бандлер разворачивает
    симлинки в копии при копировании ресурсов, из-за чего в готовом `.app`
    оказывается 4 копии одного dylib (+22 МБ). Загрузчик PyInstaller грузит
    только `_internal/Python` (проверено через DYLD_PRINT_LIBRARIES), поэтому
    фреймворк не нужен. Удаление симлинков заодно лишает бандлер повода
    дублировать файлы.
    """
    internal = destination / "_internal"
    flat = internal / "Python"
    framework = internal / "Python.framework"

    if not framework.is_dir():
        return
    if not flat.is_file():
        raise RuntimeError(f"PyInstaller layout changed: {flat} is missing")

    if flat.is_symlink():
        # Копируем реальный файл на место симлинка, чтобы `_internal/Python`
        # остался обычным файлом после удаления фреймворка.
        materialized = flat.with_name("Python.materialized")
        shutil.copy2(flat.resolve(), materialized)
        flat.unlink()
        materialized.replace(flat)
        flat.chmod(0o755)

    shutil.rmtree(framework)

def _assert_no_symlinks(destination: Path) -> None:
    """Гарантирует отсутствие симлинков в staged-сайдкаре.

    Tauri-бандлер копирует ресурсы с разыменованием симлинков: каждый оставшийся
    симлинк превращается в полную копию файла и раздувает `.app`. Регресс не
    ломает приложение, поэтому без явной проверки прошёл бы незамеченным.
    """
    remaining = sorted(path for path in destination.rglob("*") if path.is_symlink())
    if remaining:
        listed = ", ".join(str(path.relative_to(destination)) for path in remaining[:5])
        raise RuntimeError(f"Sidecar still contains symlinks that the bundler would expand: {listed}")

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
