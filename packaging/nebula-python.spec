# PyInstaller spec for the single embedded Python runtime used by Tauri.

from PyInstaller.utils.hooks import collect_submodules
from pathlib import Path


ROOT = Path(SPECPATH).parent
hiddenimports = collect_submodules("backend") + collect_submodules("contracts")

analysis = Analysis(
    [str(ROOT / "backend" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "backend" / "db" / "schema.sql"), "backend/db")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "evals"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="nebula-python",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
