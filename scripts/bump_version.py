#!/usr/bin/env python3
"""Keep the project version synchronized across all package manifests and locks."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

TOML_TARGETS = {
    "pyproject.toml": "project",
    "crates/audio-capture/Cargo.toml": "package",
    "apps/audio-spike/Cargo.toml": "package",
    "apps/desktop/src-tauri/Cargo.toml": "package",
}

JSON_TARGETS = {
    "apps/desktop/package.json": (("version",),),
    "apps/desktop/package-lock.json": (("version",), ("packages", "", "version")),
    "apps/desktop/src-tauri/tauri.conf.json": (("version",),),
}

LOCK_TARGETS = {
    "uv.lock": ("nebula",),
    "Cargo.lock": ("audio-capture", "audio-spike", "nebula-desktop"),
}


def _json_value(document: dict, keys: tuple[str, ...]) -> str:
    value: object = document
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"missing JSON path: {'.'.join(keys)}")
        value = value[key]
    if not isinstance(value, str):
        raise ValueError(f"JSON path is not a string: {'.'.join(keys)}")
    return value


def _set_json_value(document: dict, keys: tuple[str, ...], version: str) -> None:
    value = document
    for key in keys[:-1]:
        child = value.get(key)
        if not isinstance(child, dict):
            raise ValueError(f"missing JSON path: {'.'.join(keys)}")
        value = child
    value[keys[-1]] = version


def _lock_version(text: str, package: str) -> str:
    pattern = re.compile(
        rf'^\[\[package\]\]\r?\nname = "{re.escape(package)}"\r?\nversion = "([^"]+)"',
        re.MULTILINE,
    )
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ValueError(f"expected one lock entry for {package!r}, found {len(matches)}")
    return matches[0]


def read_versions(root: Path) -> dict[str, str]:
    versions: dict[str, str] = {}

    for relative_path, section in TOML_TARGETS.items():
        path = root / relative_path
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        value = document.get(section, {}).get("version")
        if not isinstance(value, str):
            raise ValueError(f"missing {section}.version in {relative_path}")
        versions[relative_path] = value

    for relative_path, paths in JSON_TARGETS.items():
        document = json.loads((root / relative_path).read_text(encoding="utf-8"))
        for keys in paths:
            label = f"{relative_path}:{'.'.join(keys)}"
            versions[label] = _json_value(document, keys)

    for relative_path, packages in LOCK_TARGETS.items():
        text = (root / relative_path).read_text(encoding="utf-8")
        for package in packages:
            versions[f"{relative_path}:{package}"] = _lock_version(text, package)

    readme = (root / "README.md").read_text(encoding="utf-8")
    badge_matches = re.findall(r"badge/версия-([0-9]+\.[0-9]+\.[0-9]+)-", readme)
    alt_matches = re.findall(r'alt="Версия ([0-9]+\.[0-9]+\.[0-9]+)"', readme)
    if len(badge_matches) != 1 or len(alt_matches) != 1:
        raise ValueError("expected exactly one version badge and alt text in README.md")
    versions["README.md:badge"] = badge_matches[0]
    versions["README.md:alt"] = alt_matches[0]

    return versions


def current_version(root: Path) -> str:
    versions = read_versions(root)
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        details = "\n".join(f"  {target}: {version}" for target, version in versions.items())
        raise ValueError(f"project versions are not synchronized:\n{details}")

    version = unique_versions.pop()
    if not SEMVER_RE.fullmatch(version):
        raise ValueError(f"unsupported project version: {version!r}")
    return version


def next_version(version: str, bump: str) -> str:
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported project version: {version!r}")

    major, minor, patch = (int(part) for part in match.groups())
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unsupported bump type: {bump!r}")


def _replace_toml_section_version(text: str, section: str, old: str, new: str) -> str:
    lines = text.splitlines(keepends=True)
    in_section = False
    replacements = 0
    version_line = re.compile(rf'^(\s*version\s*=\s*"){re.escape(old)}("\s*)$')

    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        newline = line[len(content) :]
        stripped = content.strip()
        if stripped == f"[{section}]":
            in_section = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = False
        if in_section and version_line.fullmatch(content):
            lines[index] = version_line.sub(rf"\g<1>{new}\g<2>", content) + newline
            replacements += 1

    if replacements != 1:
        raise ValueError(f"expected one version in TOML section [{section}], found {replacements}")
    return "".join(lines)


def _replace_lock_version(text: str, package: str, old: str, new: str) -> str:
    pattern = re.compile(
        rf'(^\[\[package\]\]\r?\nname = "{re.escape(package)}"\r?\nversion = ")'
        rf"{re.escape(old)}"
        r'(")',
        re.MULTILINE,
    )
    updated, replacements = pattern.subn(rf"\g<1>{new}\g<2>", text)
    if replacements != 1:
        raise ValueError(
            f"expected one lock entry for {package!r} at version {old}, found {replacements}"
        )
    return updated


def bump_project(root: Path, bump: str) -> str:
    old = current_version(root)
    new = next_version(old, bump)
    updates: dict[Path, str] = {}

    for relative_path, section in TOML_TARGETS.items():
        path = root / relative_path
        updates[path] = _replace_toml_section_version(
            path.read_text(encoding="utf-8"), section, old, new
        )

    for relative_path, paths in JSON_TARGETS.items():
        path = root / relative_path
        document = json.loads(path.read_text(encoding="utf-8"))
        for keys in paths:
            _set_json_value(document, keys, new)
        updates[path] = json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    for relative_path, packages in LOCK_TARGETS.items():
        path = root / relative_path
        updated = path.read_text(encoding="utf-8")
        for package in packages:
            updated = _replace_lock_version(updated, package, old, new)
        updates[path] = updated

    readme_path = root / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    badge_old = f"badge/версия-{old}-"
    alt_old = f'alt="Версия {old}"'
    if readme.count(badge_old) != 1 or readme.count(alt_old) != 1:
        raise ValueError("README.md version markers changed unexpectedly")
    updates[readme_path] = readme.replace(badge_old, f"badge/версия-{new}-").replace(
        alt_old, f'alt="Версия {new}"'
    )

    for path, content in updates.items():
        path.write_text(content, encoding="utf-8")

    actual = current_version(root)
    if actual != new:
        raise ValueError(f"post-bump verification failed: expected {new}, found {actual}")
    return new


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bump", choices=("check", "patch", "minor", "major"))
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.root.resolve()
    try:
        version = current_version(root) if args.bump == "check" else bump_project(root, args.bump)
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
