"""Regression: staged sidecar must not contain symlinks the Tauri bundler would expand.

PyInstaller on macOS ships `Python.framework` next to a flat `_internal/Python`,
all tied together with symlinks. The Tauri bundler copies resources with
symlink dereferencing, so every symlink became a full 7.4 MB copy of the same
dylib and `Nebula.app` grew from ~46 MB to ~68 MB.
"""
import os
from pathlib import Path

import pytest

from scripts.build_backend_sidecar import _assert_no_symlinks, _flatten_python_framework

pytestmark = pytest.mark.skipif(os.name == "nt", reason="framework layout is macOS-only")

DYLD_BYTES = b"\xcf\xfa\xed\xfe" + b"python-dylib-payload" * 64


def _make_framework_layout(root: Path) -> Path:
    """Воспроизводит раскладку onedir-сборки PyInstaller на macOS."""
    internal = root / "_internal"
    version_dir = internal / "Python.framework" / "Versions" / "3.12"
    version_dir.mkdir(parents=True)
    (version_dir / "Python").write_bytes(DYLD_BYTES)
    (version_dir / "Resources").mkdir()
    (version_dir / "Resources" / "Info.plist").write_text("<plist/>")

    (internal / "Python.framework" / "Versions" / "Current").symlink_to("3.12")
    (internal / "Python.framework" / "Python").symlink_to("Versions/Current/Python")
    (internal / "Python.framework" / "Resources").symlink_to("Versions/Current/Resources")
    (internal / "Python").symlink_to("Python.framework/Versions/3.12/Python")

    (root / "nebula-python").write_bytes(b"bootloader")
    return internal / "Python"


def test_flatten_python_framework_keeps_single_real_dylib(tmp_path):
    flat = _make_framework_layout(tmp_path)
    assert flat.is_symlink()

    _flatten_python_framework(tmp_path)

    assert flat.is_file() and not flat.is_symlink()
    assert flat.read_bytes() == DYLD_BYTES
    assert not (tmp_path / "_internal" / "Python.framework").exists()
    assert [p for p in tmp_path.rglob("*") if p.is_symlink()] == []
    # Один dylib вместо четырёх путей, ведущих к одним и тем же байтам.
    assert len(list(tmp_path.rglob("Python"))) == 1


def test_flatten_python_framework_is_noop_without_framework(tmp_path):
    internal = tmp_path / "_internal"
    internal.mkdir()
    (internal / "Python").write_bytes(DYLD_BYTES)

    _flatten_python_framework(tmp_path)

    assert (internal / "Python").read_bytes() == DYLD_BYTES


def test_flatten_python_framework_rejects_unexpected_layout(tmp_path):
    (tmp_path / "_internal" / "Python.framework").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="layout changed"):
        _flatten_python_framework(tmp_path)


def test_assert_no_symlinks_accepts_flat_tree(tmp_path):
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal" / "Python").write_bytes(DYLD_BYTES)

    _assert_no_symlinks(tmp_path)


def test_assert_no_symlinks_rejects_surviving_symlink(tmp_path):
    (tmp_path / "real.bin").write_bytes(b"payload")
    (tmp_path / "link.bin").symlink_to("real.bin")

    with pytest.raises(RuntimeError, match="symlinks"):
        _assert_no_symlinks(tmp_path)
