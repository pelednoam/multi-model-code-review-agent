"""Git state collection: run git commands safely, build the audit's git block."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from .config import REPO_ROOT


def _run_git(
    args: list[str],
    warnings: list[str],
) -> str:
    """Run a git command and return stdout.

    Args:
        args: Arguments to pass after ``git -C <repo>``.
        warnings: Mutable list to append warning messages to
            if the command fails.

    Returns:
        Stripped stdout. Empty string on failure or timeout.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        cmd = " ".join(["git", *args])
        warnings.append(f"git timed out (30s): {cmd}")
        return ""
    if result.returncode != 0:
        cmd = " ".join(["git", *args])
        stderr_stripped = result.stderr.strip()
        stderr_lines = stderr_stripped.splitlines()
        first = stderr_lines[0] if stderr_lines else "(no stderr)"
        warnings.append(f"git failed (exit {result.returncode}): {cmd} -- {first}")
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def is_safe_relpath(relpath: str) -> bool:
    """Check that a git-reported path stays inside the repo root."""
    resolved = (REPO_ROOT / relpath).resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return False
    return True


def collect_git_state(
    warnings: list[str],
) -> dict[str, Any]:
    """Collect comprehensive git state beyond just the diff.

    Args:
        warnings: Mutable list for git command failure messages.
    """
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], warnings)
    commit = _run_git(["rev-parse", "HEAD"], warnings)
    status_short = _run_git(["status", "--short"], warnings)
    cached = _run_git(["diff", "--cached", "--name-only"], warnings)
    untracked = _run_git(["ls-files", "--others", "--exclude-standard"], warnings)
    changed_vs_main = _run_git(
        ["diff", "--merge-base", "origin/main", "--name-only"], warnings
    )

    return {
        "branch": branch,
        "commit": commit[:12],
        "status_short": status_short.splitlines() if status_short else [],
        "cached_files": cached.splitlines() if cached else [],
        "untracked_files": untracked.splitlines() if untracked else [],
        "changed_vs_main": changed_vs_main.splitlines() if changed_vs_main else [],
    }
