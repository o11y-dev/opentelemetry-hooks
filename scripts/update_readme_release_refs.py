#!/usr/bin/env python3
"""Update README release references to a specific tagged version."""

from __future__ import annotations

import re
import sys
from pathlib import Path


README_TAG_REF_RE = re.compile(
    r"(git\+https://github\.com/o11y-dev/opentelemetry-hooks\.git@)v\d+\.\d+\.\d+"
)


def update_readme_release_refs(text: str, version: str) -> str:
    """Rewrite README Git install examples to the provided tag version."""
    return README_TAG_REF_RE.sub(rf"\1v{version}", text)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: update_readme_release_refs.py <readme_path> <version>", file=sys.stderr)
        return 2

    readme_path = Path(args[0])
    version = args[1]
    original = readme_path.read_text(encoding="utf-8")
    updated = update_readme_release_refs(original, version)

    if updated != original:
        readme_path.write_text(updated, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
