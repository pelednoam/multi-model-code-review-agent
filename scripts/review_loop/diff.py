"""Collect the scrubbed diff and run the preflight subprocess."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from .backends import _run
from .config import SCRIPTS_DIR

if TYPE_CHECKING:
    from pathlib import Path


def collect_diff(round_dir: Path, repo: Path) -> tuple[Path, int]:
    """Collect scrubbed diff against origin/main."""
    diff_path = round_dir / "diff.patch"
    git = _run(["git", "diff", "--merge-base", "origin/main", "--", "."], cwd=repo)
    if git.returncode != 0:
        fallback = _run(["git", "diff", "HEAD~1"], cwd=repo)
        if fallback.returncode != 0:
            raise RuntimeError(
                f"failed to collect diff: {git.stderr}\n{fallback.stderr}"
            )
        git = fallback
    elif not git.stdout.strip():
        git = _run(["git", "diff", "HEAD~1"], cwd=repo)
    diff_input = git.stdout or ""
    with open(diff_path, "w") as out_f:
        scrubber = subprocess.Popen(
            ["python", str(SCRIPTS_DIR / "scrub_diff.py")],
            stdin=subprocess.PIPE,
            stdout=out_f,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, scrub_err = scrubber.communicate(input=diff_input)
    if scrubber.returncode != 0:
        print(
            f"  WARNING: scrubber redacted lines: {scrub_err.strip()}", file=sys.stderr
        )
    with open(diff_path) as f:
        n_lines = sum(1 for _ in f)
    return diff_path, n_lines


def run_preflight(round_dir: Path, repo: Path) -> Path:
    """Run preflight audit, return path to audit JSON."""
    audit_path = round_dir / "audit.json"
    result = _run(
        [
            "python",
            str(SCRIPTS_DIR / "review_preflight.py"),
            "--output",
            str(audit_path),
        ],
        cwd=repo,
    )
    if result.returncode != 0:
        raise RuntimeError(f"preflight failed: {result.stderr}")
    if not audit_path.exists():
        raise RuntimeError("preflight did not produce audit JSON")
    return audit_path
