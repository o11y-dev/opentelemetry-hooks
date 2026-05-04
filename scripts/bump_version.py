#!/usr/bin/env python3
"""Bump version in pyproject.toml and stamp CHANGELOG.md with a release date."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
UNRELEASED_HEADING = re.compile(r"^## (?P<version>\d+\.\d+\.\d+) \(unreleased\)$", re.MULTILINE)


def bump_pyproject(version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    updated = re.sub(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if not re.search(r'^version\s*=\s*"[^"]+"', text, flags=re.MULTILINE):
        print(f"warning: version string not found in {PYPROJECT}", file=sys.stderr)
    PYPROJECT.write_text(updated, encoding="utf-8")


def _validate_section_not_empty(version: str, changelog_path: Path) -> None:
    """Fail if the stamped changelog section has no content."""
    text = changelog_path.read_text(encoding="utf-8")
    heading = re.compile(
        rf"^## {re.escape(version)}(?: \([^)]+\))?\s*$",
        flags=re.MULTILINE,
    )
    match = heading.search(text)
    if not match:
        return
    next_heading = re.search(r"^##\s+", text[match.end() :], flags=re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(text)
    section = text[match.end() : end].strip()
    if not section:
        print(
            f"error: changelog section for {version} in {changelog_path} is empty - "
            "please add release notes before releasing",
            file=sys.stderr,
        )
        raise SystemExit(1)


def stamp_changelog(version: str, changelog_path: Path = CHANGELOG, *, today: str | None = None) -> None:
    if not changelog_path.exists():
        return
    text = changelog_path.read_text(encoding="utf-8")

    already_stamped = re.compile(
        rf"^## {re.escape(version)} \(\d{{4}}-\d{{2}}-\d{{2}}\)\s*$",
        flags=re.MULTILINE,
    )
    if already_stamped.search(text):
        _validate_section_not_empty(version, changelog_path)
        return

    release_day = today or datetime.now(UTC).strftime("%Y-%m-%d")
    exact_heading = re.compile(rf"^## {re.escape(version)} \(unreleased\)$", re.MULTILINE)
    updated, replacements = exact_heading.subn(f"## {version} ({release_day})", text, count=1)
    if replacements == 0:
        updated, replacements = UNRELEASED_HEADING.subn(
            f"## {version} ({release_day})",
            text,
            count=1,
        )
    if updated == text:
        print(f"warning: no unreleased entry for {version} in {changelog_path}", file=sys.stderr)
    changelog_path.write_text(updated, encoding="utf-8")
    _validate_section_not_empty(version, changelog_path)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: bump_version.py <version>", file=sys.stderr)
        return 2
    version = args[0]
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        print(f"error: version must be semver (e.g. 0.3.0), got: {version}", file=sys.stderr)
        return 1
    bump_pyproject(version)
    stamp_changelog(version)
    print(f"Bumped to {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
