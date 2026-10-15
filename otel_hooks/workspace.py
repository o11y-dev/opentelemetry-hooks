"""Project root discovery shared by hook enrichment and agent setup."""
import os
import subprocess
import urllib.parse
from typing import Optional, Tuple

from .parsers.common import _first_present, _hash_text


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


def _path_search_root(path: str) -> Optional[str]:
    if not isinstance(path, str) or not path.strip():
        return None
    expanded = os.path.abspath(path.strip())
    if os.path.isdir(expanded):
        return expanded
    return os.path.dirname(expanded) or expanded

def _candidate_repo_paths(data: Optional[dict]) -> list[str]:
    if not isinstance(data, dict):
        return []
    candidates: list[str] = []
    cwd = data.get("cwd") or data.get("workspace_path")
    if isinstance(cwd, str) and cwd.strip():
        candidates.append(cwd.strip())

    def _append_path(value) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        raw = value.strip()
        if os.path.isabs(raw):
            candidates.append(raw)
            return
        if isinstance(cwd, str) and cwd.strip() and os.path.isabs(cwd.strip()):
            candidates.append(os.path.join(cwd.strip(), raw))

    _append_path(data.get("file_path"))
    edits = data.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                _append_path(entry.get("file_path") or entry.get("path"))
    elif isinstance(edits, dict):
        _append_path(edits.get("file_path") or edits.get("path"))
    return candidates

def _git_command_output(args: list[str], cwd: str) -> Optional[str]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None

def _parse_repository_remote(remote_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(remote_url, str) or not remote_url.strip():
        return None, None
    normalized = remote_url.strip()
    is_network_remote = False
    if "://" in normalized:
        is_network_remote = True
        path = urllib.parse.urlparse(normalized).path or ""
    elif "@" in normalized and ":" in normalized.split("@", 1)[1]:
        is_network_remote = True
        path = normalized.split(":", 1)[1]
    else:
        path = normalized
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None, None
    owner = parts[-2] if is_network_remote and len(parts) >= 2 else None
    return owner, parts[-1]

def _normalize_repository_remote(remote_url: Optional[str]) -> Optional[str]:
    """Return a credential-free host/path identity suitable for deterministic hashing."""
    if not isinstance(remote_url, str) or not remote_url.strip():
        return None
    value = remote_url.strip()
    host = ""
    path = ""
    if "://" in value:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        try:
            if parsed.port:
                host = f"{host}:{parsed.port}"
        except ValueError:
            return None
        path = parsed.path
    elif "@" in value and ":" in value.split("@", 1)[1]:
        host_path = value.split("@", 1)[1]
        host, path = host_path.split(":", 1)
        host = host.lower()
    else:
        return None
    path = "/".join(part for part in path.strip().strip("/").split("/") if part)
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return None
    return f"{host}/{path}"

def _resolve_repository_context(data: Optional[dict] = None, session_ctx: Optional[dict] = None) -> dict:
    repo_ctx: dict[str, str] = {}
    if session_ctx:
        for key in (
            "repo_root",
            "workspace_path",
            "vcs.repository.owner",
            "vcs.repository.name",
            "vcs.ref.head.name",
            "vcs.ref.head.type",
            "gen_ai.client.repository.remote.sha256",
        ):
            value = session_ctx.get(key)
            if isinstance(value, str) and value.strip():
                repo_ctx[key] = value.strip()
    explicit_workspace = _first_present(data or {}, ("workspace_path",))
    if isinstance(explicit_workspace, str) and explicit_workspace.strip():
        repo_ctx["workspace_path"] = os.path.abspath(os.path.expanduser(explicit_workspace.strip()))
    if session_ctx and session_ctx.get("repository_context_resolved") and repo_ctx.get("repo_root"):
        return repo_ctx
    if "repo_root" not in repo_ctx:
        for candidate in _candidate_repo_paths(data):
            search_root = _path_search_root(candidate)
            if not search_root:
                continue
            repo_root = _find_repo_root(search_root)
            if repo_root and any(os.path.exists(os.path.join(repo_root, marker)) for marker in _REPO_MARKERS):
                repo_ctx["repo_root"] = repo_root
                break
    repo_root = repo_ctx.get("repo_root")
    if not repo_root:
        return repo_ctx
    if not os.path.exists(os.path.join(repo_root, ".git")):
        return repo_ctx
    git_root = _git_command_output(["git", "rev-parse", "--show-toplevel"], cwd=repo_root)
    if git_root:
        repo_ctx["repo_root"] = git_root
        repo_ctx.setdefault("workspace_path", git_root)
        remote_url = None
        if (
            "vcs.repository.name" not in repo_ctx
            or "vcs.repository.owner" not in repo_ctx
            or "gen_ai.client.repository.remote.sha256" not in repo_ctx
        ):
            remote_url = _git_command_output(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=git_root,
            )
            owner, name = _parse_repository_remote(remote_url)
            if owner and "vcs.repository.owner" not in repo_ctx:
                repo_ctx["vcs.repository.owner"] = owner
            if name and "vcs.repository.name" not in repo_ctx:
                repo_ctx["vcs.repository.name"] = name
            normalized_remote = _normalize_repository_remote(remote_url)
            if normalized_remote:
                repo_ctx["gen_ai.client.repository.remote.sha256"] = _hash_text(normalized_remote)
        if "vcs.repository.name" not in repo_ctx:
            repo_name = os.path.basename(git_root.rstrip(os.sep))
            if repo_name:
                repo_ctx["vcs.repository.name"] = repo_name
        if "vcs.ref.head.name" not in repo_ctx:
            head_name = _git_command_output(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=git_root,
            )
            if head_name:
                repo_ctx["vcs.ref.head.name"] = head_name
                repo_ctx["vcs.ref.head.type"] = "branch"
    return repo_ctx

def _collect_repository_attributes(data: Optional[dict] = None, session_ctx: Optional[dict] = None) -> dict:
    attrs: dict[str, str] = {}
    repo_ctx = _resolve_repository_context(data, session_ctx=session_ctx)
    for key in (
        "vcs.repository.owner",
        "vcs.repository.name",
        "vcs.ref.head.name",
        "vcs.ref.head.type",
        "gen_ai.client.repository.remote.sha256",
    ):
        value = repo_ctx.get(key)
        if value:
            attrs[key] = value
    return attrs

def _workspace_observability_attributes(data: Optional[dict], session_ctx: Optional[dict]) -> dict:
    data = data or {}
    repo_ctx = _resolve_repository_context(data, session_ctx=session_ctx)
    cwd = _first_present(data, ("cwd",))
    if not isinstance(cwd, str) or not cwd.strip():
        cwd = None
    workspace = _first_present(data, ("workspace_path",)) or repo_ctx.get("workspace_path")
    workspace = workspace or repo_ctx.get("repo_root") or cwd
    attrs = {}
    if workspace:
        attrs["gen_ai.client.workspace"] = os.path.abspath(os.path.expanduser(str(workspace)))
    if cwd:
        attrs["gen_ai.client.cwd"] = os.path.abspath(os.path.expanduser(cwd))
    if repo_ctx.get("repo_root"):
        attrs["gen_ai.client.repository_root"] = repo_ctx["repo_root"]
    for key in (
        "vcs.repository.owner",
        "vcs.repository.name",
        "vcs.ref.head.name",
        "vcs.ref.head.type",
        "gen_ai.client.repository.remote.sha256",
    ):
        if repo_ctx.get(key):
            attrs[key] = repo_ctx[key]
    return attrs
