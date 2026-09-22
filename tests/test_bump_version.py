from pathlib import Path
from shutil import copyfile

import pytest

from scripts.bump_version import (
    JSON_TARGETS,
    LOCK_TARGETS,
    TOML_TARGETS,
    bump_project,
    current_version,
    next_version,
)

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = set(TOML_TARGETS) | set(JSON_TARGETS) | set(LOCK_TARGETS) | {"README.md"}


@pytest.mark.parametrize(
    ("current", "bump", "expected"),
    [
        ("1.2.3", "patch", "1.2.4"),
        ("1.2.3", "minor", "1.3.0"),
        ("1.2.3", "major", "2.0.0"),
    ],
)
def test_next_version(current: str, bump: str, expected: str) -> None:
    assert next_version(current, bump) == expected


def test_bump_project_updates_every_version_file(tmp_path: Path) -> None:
    for relative_path in VERSION_FILES:
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        copyfile(ROOT / relative_path, destination)

    old = current_version(tmp_path)
    expected = next_version(old, "patch")

    assert bump_project(tmp_path, "patch") == expected
    assert current_version(tmp_path) == expected
