#!/usr/bin/env python3
"""Resolve a target-platform wheelhouse and write a hash-locked offline install list."""
import argparse
from email.parser import BytesParser
import hashlib
from pathlib import Path
import subprocess
import sys
import zipfile


def lock_wheels(directory: Path) -> str:
    records = {}
    for wheel in sorted(directory.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise ValueError("wheel must contain exactly one distribution metadata file")
            message = BytesParser().parsebytes(archive.read(metadata[0]))
        name, version = message["Name"], message["Version"]
        if not name or not version or name.lower() in records:
            raise ValueError("wheelhouse contains missing or duplicate distribution metadata")
        with wheel.open("rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()
        records[name.lower()] = f"{name}=={version} --hash=sha256:{digest}"
    if "opentelemetry-hooks" not in records:
        raise ValueError("wheelhouse must contain opentelemetry-hooks")
    return "\n".join(records[name] for name in sorted(records)) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--constraints", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--platform", required=True, help="pip target tag, e.g. win_amd64 or macosx_11_0_arm64")
    parser.add_argument("--python-version", default="3.12")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new directory so stale wheels cannot enter the lock")
    wheels = args.output / "wheels"
    wheels.mkdir(parents=True)
    subprocess.run([
        sys.executable, "-m", "pip", "download", "--only-binary=:all:",
        "--platform", args.platform, "--python-version", args.python_version,
        "--implementation", "cp", "--abi", "cp" + args.python_version.replace(".", ""),
        "--constraint", str(args.constraints), "--dest", str(wheels), str(args.wheel.resolve()),
    ], check=True)
    (args.output / "requirements.lock").write_text(lock_wheels(wheels), encoding="utf-8")


if __name__ == "__main__":
    main()
