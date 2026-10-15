"""Project root discovery shared by hook enrichment and agent setup."""
import os
import subprocess


_REPO_MARKERS = (".git", ".github", ".cursor", ".claude", ".gemini", ".codex", ".opencode", ".windsurf")


def _find_repo_root(cwd: str) -> str:
    """Walk up from cwd to find the nearest repo/project root.

    Tries `git rev-parse --show-toplevel` first (handles worktrees where .git
    is a file rather than a directory), then falls back to marker detection.
    """
    # Fast path: ask git (works for worktrees too)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.path.abspath(cwd),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # git not installed, timed out, or environment error — non-fatal, use marker walk
        pass
    # Fallback: walk up looking for well-known markers (os.path.exists handles
    # both regular dirs and file-form .git used by worktrees/submodules)
    current = os.path.abspath(cwd)
    while True:
        for marker in _REPO_MARKERS:
            if os.path.exists(os.path.join(current, marker)):
                return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(cwd)
        current = parent
