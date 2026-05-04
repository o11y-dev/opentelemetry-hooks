#!/usr/bin/env python3
"""Helpers for the GitHub release workflow."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def _validate_semver(version: str) -> str:
    if not SEMVER_PATTERN.fullmatch(version):
        raise ValueError(
            f"version must be in semver format (MAJOR.MINOR.PATCH, e.g. 0.3.0), got: {version}"
        )
    return version


def read_project_version(pyproject_path: Path = PYPROJECT) -> str:
    try:
        text = pyproject_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"{pyproject_path.name} not found") from exc

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{pyproject_path.name} is not valid TOML") from exc

    try:
        version = data["project"]["version"]
    except KeyError as exc:
        raise ValueError(f"{pyproject_path.name} missing [project].version") from exc

    return _validate_semver(str(version))


def version_tag_exists(version: str, root: Path = ROOT) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--tags", "--verify", "--quiet", f"refs/tags/v{version}"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def parse_semantic_release_output(output: str) -> str | None:
    if "No release will be made" in output:
        return None

    versions = [
        line.strip()
        for line in output.splitlines()
        if SEMVER_PATTERN.fullmatch(line.strip())
    ]
    return versions[-1] if versions else None


def determine_version(
    *,
    manual_version: str | None = None,
    force: str | None = None,
    root: Path = ROOT,
    runner: Callable[..., Any] | None = None,
) -> str | None:
    if manual_version:
        return _validate_semver(manual_version)

    project_version = read_project_version(root / "pyproject.toml")
    if not version_tag_exists(project_version, root):
        return project_version

    run_cmd = runner or subprocess.run
    command = ["semantic-release", "version", "--print"]
    if force:
        command.append(f"--{force}")
    result = run_cmd(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return parse_semantic_release_output(output)


def extract_release_notes(version: str, changelog_path: Path = CHANGELOG) -> str:
    _validate_semver(version)
    text = changelog_path.read_text(encoding="utf-8")
    heading = re.compile(
        rf"^## {re.escape(version)}(?: \([^)]+\))?\s*$",
        flags=re.MULTILINE,
    )
    match = heading.search(text)
    if not match:
        raise ValueError(f"no changelog section found for {version}")

    next_heading = re.search(r"^##\s+", text[match.end() :], flags=re.MULTILINE)
    section_end = match.end() + next_heading.start() if next_heading else None
    section = text[match.end() : section_end].strip()
    if not section:
        raise ValueError(f"changelog section for {version} is empty")
    return f"{section}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    determine = subparsers.add_parser("determine-version")
    determine.add_argument("--version")
    determine.add_argument("--force", choices=["patch", "minor", "major"])

    notes = subparsers.add_parser("release-notes")
    notes.add_argument("version")

    args = parser.parse_args(argv)

    try:
        if args.command == "determine-version":
            version = determine_version(manual_version=args.version, force=args.force)
            if version:
                print(version)
            return 0

        print(extract_release_notes(args.version), end="")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
