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


#: What to diff against when the caller does not say. The common case: a
#: branch under review, against the trunk it will land on.
DEFAULT_BASE = "origin/main"


def collect_diff(round_dir: Path, repo: Path, base: str = DEFAULT_BASE) -> tuple[Path, int]:
    """Collect the scrubbed diff between ``base`` and the working tree.

    ``base`` defaults to ``origin/main``, which is right while the work is on a
    branch. It is wrong the moment the work is *pushed*: the merge-base diff is
    then empty, this falls back to ``HEAD~1``, and a review of "everything I
    did today" silently becomes a review of the last commit. Reviewing a
    range that is already on the trunk -- after a merge, or before a release --
    is a real thing to want, and there was no way to ask for it.

    Raises:
        SecretsDetectedError: scrub_diff.py redacted at least one line.
            The user must rotate the leaked secret and re-run.
        RuntimeError: If ``base`` names nothing git can resolve. Deliberately
            not a fallback: a caller that named a base meant it, and quietly
            reviewing ``HEAD~1`` instead is how you end up believing something
            was reviewed that never was.
    """
    if base != DEFAULT_BASE:
        named = _run(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=repo)
        if named.returncode != 0:
            msg = f"--base {base!r} does not name a commit in this repository"
            raise RuntimeError(msg)
    diff_path = round_dir / "diff.patch"
    # Exclude the agent's own vendored files. install.sh copies them into the
    # host project, so without this a host's first review spends most of its
    # budget -- 60% of the diff on a fresh install -- reviewing the reviewer.
    pathspec = diff_pathspec(repo)
    git = _run(["git", "diff", "--merge-base", base, "--", *pathspec], cwd=repo)
    if git.returncode != 0:
        fallback = _run(["git", "diff", "HEAD~1", "--", *pathspec], cwd=repo)
        if fallback.returncode != 0:
            raise RuntimeError(
                f"failed to collect diff: {git.stderr}\n{fallback.stderr}"
            )
        git = fallback
    elif not git.stdout.strip() and base == DEFAULT_BASE:
        # Only for the default. An empty diff against a base somebody named is
        # an answer -- "nothing changed since there" -- not a reason to review
        # something else.
        git = _run(["git", "diff", "HEAD~1", "--", *pathspec], cwd=repo)
    diff_input = git.stdout or ""
    # Every hop is pinned to UTF-8 with replacement. scrub_diff.py pins its own
    # streams because CI runs under LC_ALL=C, but that was the only hop that
    # did: git's output, the pipe into the scrubber and the read-back all used
    # the ambient codec, so one non-ASCII byte anywhere in the tree still
    # raised -- and this repository's own sources are full of en dashes.
    with open(diff_path, "w", encoding="utf-8", errors="replace") as out_f:
        scrubber = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "scrub_diff.py")],
            stdin=subprocess.PIPE,
            stdout=out_f,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
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
    text = diff_path.read_text(encoding="utf-8", errors="replace")
    n_lines = len(text.splitlines())
    for note in dominant_files(text):
        print(f"  NOTE: {note}")
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


#: A single file bigger than this share of the diff is worth mentioning...
_DOMINANT_SHARE = 0.4

#: ...but only once it is big enough to matter. Half of a sixty-line diff is
#: just a small change to one file; half of a fifteen-thousand-line one is a
#: lockfile in front of four models.
_DOMINANT_MINIMUM = 500


def dominant_files(diff: str) -> list[str]:
    """Name any single file that is most of the diff.

    Reviewers are paid by the token and read in one pass, so a generated file
    left in the diff does not just waste money -- it crowds out the code. A
    lockfile was once 69% of a review: ten thousand lines of resolved dependency
    tree, in front of four models, instead of the change being reviewed.

    Reported rather than excluded. What counts as generated is the project's
    call, and ``.gitattributes`` is where that call belongs.
    """
    sizes = _lines_per_file(diff)
    total = sum(sizes.values())
    if total == 0:
        return []
    return [
        f"{path} is {count / total:.0%} of this diff ({count} lines). "
        f"If it is generated, `{path} -diff linguist-generated=true` in "
        f".gitattributes keeps it out of the review."
        for path, count in sorted(sizes.items(), key=lambda kv: -kv[1])
        if count / total >= _DOMINANT_SHARE and count >= _DOMINANT_MINIMUM
    ]


def _lines_per_file(diff: str) -> dict[str, int]:
    """How many lines of the diff belong to each file."""
    sizes: dict[str, int] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = _path_of(line)
            sizes.setdefault(current, 0)
        elif current:
            sizes[current] += 1
    return sizes


def _path_of(header: str) -> str:
    """The file a `diff --git` header is about."""
    for part in header.split():
        if part.startswith("b/"):
            return part[2:]
    return header
