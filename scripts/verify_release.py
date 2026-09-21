"""Проверяет, что собранный DMG реально запускается после передачи получателю.

Ловит два регресса, которые для получателя выглядят одинаково — «приложение
не запускается»:

1. Потерянные exec-биты. Архивы (например, zip из Telegram) не сохраняют
   Unix-режимы, и после распаковки запускать оказывается нечего.
2. Битая подпись бандла. Без `Contents/_CodeSignature/CodeResources` macOS
   сообщает, что приложение повреждено, и правый клик → «Открыть» не помогает.

Запуск: `uv run python scripts/verify_release.py`
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DMG_DIR = ROOT / "target" / "release" / "bundle" / "dmg"
DEFAULT_APP = ROOT / "target" / "release" / "bundle" / "macos" / "Nebula.app"
SIDECAR_GLOB = "Contents/Resources/sidecar/*/nebula-python"

MACH_O_MAGICS = {
    b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 (little-endian)
    b"\xce\xfa\xed\xfe",  # MH_MAGIC (little-endian)
    b"\xca\xfe\xba\xbe",  # FAT_MAGIC (big-endian)
    b"\xbe\xba\xfe\xca",  # FAT_MAGIC (little-endian)
}


class VerificationError(RuntimeError):
    """Проверка релиза не прошла."""


def is_mach_o(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACH_O_MAGICS
    except OSError:
        return False


def check_permissions(app: Path) -> list[str]:
    """Каждый Mach-O внутри бандла должен быть исполняемым.

    Проверка самодостаточна (не требует исходной сборки) и точно описывает
    инвариант, который ломают архиваторы: без exec-бита macOS не может
    запустить ни приложение, ни его сайдкар.
    """
    problems: list[str] = []
    mach_o_count = 0
    for path in sorted(app.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        executable = bool(path.stat().st_mode & 0o100)
        if is_mach_o(path):
            mach_o_count += 1
            if not executable:
                problems.append(f"нет exec-бита: {path.relative_to(app)}")
        elif executable:
            problems.append(f"лишний exec-бит: {path.relative_to(app)}")

    if mach_o_count == 0:
        problems.append("в бандле не найдено ни одного Mach-O файла — сборка пустая")
    return problems


def check_signature(app: Path) -> list[str]:
    """Подпись бандла должна проверяться, иначе macOS сочтёт приложение повреждённым."""
    if not (app / "Contents" / "_CodeSignature" / "CodeResources").is_file():
        return ["отсутствует Contents/_CodeSignature/CodeResources — подпись бандла не создана"]

    result = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return [f"codesign --verify не прошёл: {detail[0] if detail else 'без деталей'}"]
    return []


def check_quarantine(app: Path) -> list[str]:
    """Предупреждает, если бандл помечен карантином: запуск будет заблокирован."""
    result = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(app)],
                            capture_output=True, text=True)
    if result.returncode == 0:
        return [
            "бандл помечен карантином — macOS заблокирует запуск. "
            f"Снять: xattr -cr {app}"
        ]
    return []


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def smoke_launch(app: Path, timeout: float = 60.0) -> list[str]:
    """Запускает сайдкар из бандла и ждёт ответа `/healthz`."""
    sidecar = next(iter(sorted(app.glob(SIDECAR_GLOB))), None)
    if sidecar is None:
        return [f"в бандле не найден сайдкар по маске {SIDECAR_GLOB}"]

    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(
            os.environ,
            NEBULA_DB_PATH=str(Path(tmp) / "verify.db"),
            NEBULA_BACKUP_DIR=str(Path(tmp) / "backups"),
            NEBULA_PARENT_PIPE="0",
        )
        log_path = Path(tmp) / "sidecar.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [str(sidecar), "api", "--port", str(port)],
                env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    return [f"сайдкар завершился с кодом {process.returncode}:\n"
                            f"{log_path.read_text()[-2000:]}"]
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/healthz", timeout=1
                    ) as response:
                        if response.status == 200:
                            return []
                except (urllib.error.URLError, TimeoutError, OSError):
                    time.sleep(0.25)
            return [f"сайдкар не ответил на /healthz за {timeout:.0f} с:\n"
                    f"{log_path.read_text()[-2000:]}"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


@contextmanager
def mounted(dmg: Path):
    """Монтирует DMG в временную точку и гарантированно размонтирует её."""
    mountpoint = Path(tempfile.mkdtemp(prefix="nebula-verify-"))
    try:
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly",
             "-mountpoint", str(mountpoint), str(dmg)],
            check=True, capture_output=True, text=True,
        )
        yield mountpoint
    finally:
        subprocess.run(["hdiutil", "detach", str(mountpoint), "-quiet"],
                       capture_output=True, text=True)
        shutil.rmtree(mountpoint, ignore_errors=True)


def latest_dmg() -> Path:
    candidates = sorted(DEFAULT_DMG_DIR.glob("*.dmg"))
    if not candidates:
        raise VerificationError(f"в {DEFAULT_DMG_DIR} нет ни одного .dmg — сначала соберите бандл")
    return candidates[-1]


def app_inside(root: Path) -> Path:
    apps = sorted(root.glob("*.app"))
    if not apps:
        raise VerificationError(f"в {root} не найден .app")
    return apps[0]


def report(title: str, problems: list[str]) -> bool:
    if problems:
        print(f"✗ {title}")
        for problem in problems:
            print(f"    {problem}")
        return False
    print(f"✓ {title}")
    return True


def verify(app: Path, launch: bool) -> bool:
    ok = report("exec-биты сохранены", check_permissions(app))
    ok &= report("подпись бандла валидна", check_signature(app))
    ok &= report("карантина нет", check_quarantine(app))
    if launch:
        ok &= report("сайдкар отвечает на /healthz", smoke_launch(app))
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--dmg", type=Path, help="проверить DMG (по умолчанию свежий из target/)")
    source.add_argument("--app", type=Path, help="проверить .app напрямую, без монтирования")
    parser.add_argument("--no-launch", action="store_true", help="пропустить запуск сайдкара")
    args = parser.parse_args(argv)

    if args.app:
        print(f"Проверяю {args.app}")
        return 0 if verify(args.app, launch=not args.no_launch) else 1

    try:
        dmg = args.dmg or latest_dmg()
    except VerificationError as error:
        print(f"✗ {error}")
        return 1

    print(f"Проверяю {dmg}")
    with mounted(dmg) as mountpoint:
        app = app_inside(mountpoint)
        print(f"Смонтирован: {app}")
        passed = verify(app, launch=not args.no_launch)

    print("DMG готов к передаче." if passed else "DMG требует доработки.")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
