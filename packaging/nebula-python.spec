# PyInstaller spec for the single embedded Python runtime used by Tauri.
#
# Режим onedir (EXE + COLLECT), а не onefile: onefile распаковывает ~16 МБ рантайма
# в свежий _MEI-каталог при каждом запуске, из-за чего старт backend занимал секунды
# и не укладывался в бюджет готовности на нагруженной машине. onedir распаковки не
# требует вовсе, поэтому старт предсказуемо быстрый.

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
    [],
    exclude_binaries=True,
    name="nebula-python",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="nebula-python",
)
