"""Hook home resolution and source-checkout dependencies on POSIX and Windows."""

import importlib
import os
import subprocess
import sys

READY_MARKER = ".otel-hook-ready"


def venv_python(venv_dir: str) -> str:
    parts = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    return os.path.join(venv_dir, *parts)


def resolve_hook_home(module_file: str) -> str:
    """Return the writable directory used for hook state, config, and the bootstrap venv.

    Resolution order:
    1. ``IDE_OTEL_HOOK_HOME`` environment variable (explicit override).
    2. ``%LOCALAPPDATA%/opentelemetry-hooks`` on Windows, or
       ``$XDG_DATA_HOME/opentelemetry-hooks`` on POSIX (defaults to
       ``~/.local/share/opentelemetry-hooks`` when ``XDG_DATA_HOME`` is unset)
       when the hook is running from an installed package — i.e. ``module_file``
       lives inside a *site-packages* directory.
    3. The directory that contains ``module_file`` — legacy behaviour for a
       source checkout (keep its companion modules).
    """
    explicit = os.environ.get("IDE_OTEL_HOOK_HOME", "").strip()
    if explicit:
        return os.path.abspath(explicit)

    # Detect installed-package mode by comparing the runtime file against the known
    # site-packages directories reported by sysconfig / site.
    this_file = os.path.abspath(module_file)
    in_site_packages = False
    try:
        import sysconfig
        purelib = sysconfig.get_path("purelib") or ""
        platlib = sysconfig.get_path("platlib") or ""
        for sp in (purelib, platlib):
            if sp and this_file.startswith(os.path.abspath(sp) + os.sep):
                in_site_packages = True
                break
    except Exception:
        pass
    if not in_site_packages:
        try:
            import site
            for sp in (site.getsitepackages() if hasattr(site, "getsitepackages") else []):
                if sp and this_file.startswith(os.path.abspath(sp) + os.sep):
                    in_site_packages = True
                    break
        except Exception:
            pass

    if in_site_packages:
        if sys.platform == "win32":
            local_data = os.environ.get("LOCALAPPDATA", "").strip() or os.path.join(
                os.path.expanduser("~"), "AppData", "Local",
            )
            return os.path.join(local_data, "opentelemetry-hooks")
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if not xdg_data:
            xdg_data = os.path.join(os.path.expanduser("~"), ".local", "share")
        return os.path.join(xdg_data, "opentelemetry-hooks")

    return os.path.dirname(this_file)


def venv_site_packages_path(venv_dir: str) -> str:
    """Return the site-packages path compatible with this Python runtime."""
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Lib", "site-packages")
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return os.path.join(venv_dir, "lib", version, "site-packages")


def select_bootstrap_venv_dir(hook_dir: str) -> str:
    """Reuse a compatible legacy venv or isolate bootstrap deps by Python ABI."""
    legacy = os.path.join(hook_dir, ".venv")
    # Windows venv layouts do not encode the Python ABI; use a versioned venv.
    if sys.platform != "win32" and os.path.isdir(venv_site_packages_path(legacy)):
        return legacy
    suffix = f".venv-py{sys.version_info.major}.{sys.version_info.minor}"
    return os.path.join(hook_dir, suffix)


def runtime_has_otel_dependencies() -> bool:
    """Return whether the invoking environment already owns the hook SDK deps."""
    required = (
        "opentelemetry.sdk.trace.export",
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    )
    try:
        for module in required:
            importlib.import_module(module)
        return True
    except (ImportError, AttributeError, ValueError):
        return False


def auto_provision_venv(venv_dir: str, lock_path: str) -> None:
    """Create .venv and install opentelemetry-sdk + exporter in the background if missing."""
    python_path = venv_python(venv_dir)
    if os.path.isfile(python_path) and os.path.isfile(os.path.join(venv_dir, READY_MARKER)):
        return
    lock_dir = os.path.dirname(lock_path)
    os.makedirs(lock_dir, exist_ok=True)
    if os.path.exists(lock_path):
        return
    try:
        with open(lock_path, "x") as f:
            f.write(str(os.getpid()))
        process_options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if sys.platform == "win32" else {"start_new_session": True}
        )
        subprocess.Popen(
            [sys.executable, __file__, venv_dir, lock_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **process_options,
        )
    except FileExistsError:
        # Another invocation owns provisioning; do not remove its lock.
        return
    except Exception:
        try:
            os.remove(lock_path)
        except OSError:
            pass


def provision(venv_dir: str, lock_path: str) -> None:
    """Install through argv lists so paths never become shell commands."""
    ready_path = os.path.join(venv_dir, READY_MARKER)
    try:
        # A Python executable alone does not prove pip finished successfully.
        if os.path.exists(ready_path):
            os.remove(ready_path)
        subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
        subprocess.run([
            venv_python(venv_dir), "-m", "pip", "install", "--quiet",
            "opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-grpc",
            "opentelemetry-exporter-otlp-proto-http",
        ], check=True)
        with open(ready_path, "w", encoding="utf-8"):
            pass
    finally:
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    provision(*sys.argv[1:])
