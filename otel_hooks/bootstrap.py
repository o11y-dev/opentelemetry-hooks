"""Background dependency installation for source checkouts on POSIX and Windows."""

import os
import subprocess
import sys


def venv_python(venv_dir: str) -> str:
    parts = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    return os.path.join(venv_dir, *parts)


def provision(venv_dir: str, lock_path: str) -> None:
    """Install through argv lists so paths never become shell commands."""
    try:
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
        subprocess.run([
            venv_python(venv_dir), "-m", "pip", "install", "--quiet",
            "opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-grpc",
            "opentelemetry-exporter-otlp-proto-http",
        ], check=True)
    finally:
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    provision(*sys.argv[1:])
