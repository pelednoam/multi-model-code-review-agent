"""Collect the scrubbed diff and run the preflight subprocess."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from .backends import _run
from .config import SCRIPTS_DIR, diff_pathspec, is_agent_repo

if TYPE_CHECKING:
    from pathlib import Path


#: scrub_diff.py exits 2 when it failed part-way through. What reached stdout
#: is a truncated patch whose tail was never scrubbed.
SCRUBBER_ABORTED = 2


class ScrubberFailedError(RuntimeError):
    """Raised when scrub_diff.py crashed instead of finishing.

    Distinct from :class:`SecretsDetectedError` because the remedy is
    different: nothing leaked that rotating a credential would fix, but the
    patch on disk is incomplete and must not be shown to a reviewer.
    """


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
    # Exclude the agent's own vendored files. install.sh copies them into the
    # host project, so without this a host's first review spends most of its
    # budget -- 60% of the diff on a fresh install -- reviewing the reviewer.
    pathspec = diff_pathspec(repo)
    git = _run(
        ["git", "diff", "--merge-base", "origin/main", "--", *pathspec], cwd=repo
    )
    if git.returncode != 0:
        fallback = _run(["git", "diff", "HEAD~1", "--", *pathspec], cwd=repo)
        if fallback.returncode != 0:
            raise RuntimeError(
                f"failed to collect diff: {git.stderr}\n{fallback.stderr}"
            )
        git = fallback
    elif not git.stdout.strip():
        git = _run(["git", "diff", "HEAD~1", "--", *pathspec], cwd=repo)
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
    if scrubber.returncode == SCRUBBER_ABORTED:
        raise ScrubberFailedError(
            "scrub_diff.py aborted part-way through. The diff on disk is "
            "truncated and was not fully scrubbed, so it must not be "
            "reviewed.\n"
            f"Scrubber stderr: {scrub_err.strip()}\n"
            f"Partial diff saved to: {diff_path}"
        )
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


def _preflight_script(repo: Path) -> Path:
    """The preflight to run for ``repo`` -- its own installed copy if it has one.

    The preflight reads ``scripts/preflight/config.py`` *next to itself*:
    SOURCE_DIRS, TEST_DIRS, SIGNED_MANIFESTS, all of it. Running this repo's
    copy against another project therefore audits the wrong tree's layout, and
    does it silently -- the coverage gate reports "no Python file matched
    SOURCE_DIRS", the changed-file list belongs to the agent rather than the
    project, and reviewers reason from both. Prefer the copy install.sh put in
    the project.
    """
    installed = repo / "scripts" / "review_preflight.py"
    if installed.is_file() and not is_agent_repo(repo):
        return installed
    return SCRIPTS_DIR / "review_preflight.py"


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
            str(_preflight_script(repo)),
            "--output",
            str(audit_path),
        ],
        cwd=repo,
    )
    if not audit_path.exists():
        raise RuntimeError(
            f"preflight failed (no audit JSON, exit {result.returncode}): {result.stderr}"
        )
    return audit_path
