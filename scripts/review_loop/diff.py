"""Collect the scrubbed diff and run the preflight subprocess."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from .backends import _run
from .config import SCRIPTS_DIR

if TYPE_CHECKING:
    from pathlib import Path


class SecretsDetectedError(RuntimeError):
    """Raised when scrub_diff.py reports that secrets were redacted.

    The agent contract says secrets must never reach a reviewer -- the
    review must STOP so the user can rotate the secret and re-stage.
    Continuing with a partially-scrubbed diff would defeat the whole
    point of having a scrubber.
    """


def collect_diff(round_dir: Path, repo: Path) -> tuple[Path, int]:
    """Collect scrubbed diff against origin/main.

    Raises:
        SecretsDetectedError: scrub_diff.py redacted at least one line.
            The user must rotate the leaked secret and re-run.
    """
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
            [sys.executable, str(SCRIPTS_DIR / "scrub_diff.py")],
            stdin=subprocess.PIPE,
            stdout=out_f,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, scrub_err = scrubber.communicate(input=diff_input)
    if scrubber.returncode != 0:
        raise SecretsDetectedError(
            "scrub_diff.py redacted at least one line from the diff. "
            "The leaked secret must be rotated, the diff re-staged, and "
            "the review re-run before proceeding.\n"
            f"Scrubber stderr: {scrub_err.strip()}\n"
            f"Redacted diff saved to: {diff_path}"
        )
    with open(diff_path) as f:
        n_lines = sum(1 for _ in f)
    return diff_path, n_lines


def run_preflight(round_dir: Path, repo: Path) -> Path:
    """Run preflight audit, return path to audit JSON.

    review_preflight.py exits 1 when ``n_warnings > 0`` to signal humans
    that the tree has concerns worth eyeballing. That signal must NOT be
    fatal for the automated review loop -- if the audit JSON was written
    successfully, the reviewers can read the warnings from it and decide
    whether they're blocking. Only treat the run as failed if the audit
    JSON wasn't produced at all.
    """
    audit_path = round_dir / "audit.json"
    result = _run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "review_preflight.py"),
            "--output",
            str(audit_path),
        ],
        cwd=repo,
    )
    if not audit_path.exists():
        raise RuntimeError(
            f"preflight failed (no audit JSON, exit {result.returncode}): "
            f"{result.stderr}"
        )
    return audit_path
