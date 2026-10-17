"""Configuration constants for the convergence loop."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent

#: git, by the path it has when the loop starts: a reviewer that can write files can
#: plant one under its name early on PATH (~/.local/bin/git), for the loop's own checks
#: of what the reviewers wrote to run.
GIT = os.path.abspath(_found) if (_found := shutil.which("git")) else "git"

#: Seconds a reviewer gets on a *small* diff. Scaled by size below, because a
#: flat budget kills exactly the reviewer worth waiting for: the slowest one is
#: consistently the one with the most to say, and on a 5,000-line diff it
#: needed twenty-five minutes to produce fifteen findings.
REVIEWER_TIMEOUT = 600

#: Extra seconds per line of diff. Measured: the reviewers that finish take
#: roughly this long per line, and the budget has to cover the slowest.
REVIEWER_SECONDS_PER_LINE = 0.25

#: The ceiling, so a runaway diff cannot hang a round indefinitely.
REVIEWER_TIMEOUT_MAX = 2700

#: How long to keep checking for a result after a reviewer was killed. Some
#: CLIs write their output through a child process that outlives the one we
#: started, so a review can land *after* the timeout -- and was thrown away.
LATE_RESULT_GRACE = 120

#: How often to say who is still working. Four models on a large diff is
#: fifteen minutes of silence otherwise.
PROGRESS_INTERVAL = 30

#: How often to check whether a reviewer has finished. Short enough that the
#: round does not sit idle after the last one exits.
POLL_INTERVAL = 2
MERGE_TIMEOUT = 900
TEST_TIMEOUT = 300

#: The reviewer schema, handed to agy's ``--json-schema`` so the shape of its
#: answer is enforced by the CLI rather than only asked for in the prompt.
RESULT_SCHEMA = REPO_ROOT / "docs" / "ensemble_review_result_schema.json"

#: ``agy models`` is a listing, not a model call, but it goes over the network.
AGY_MODELS_TIMEOUT = 60


def env_flag(name: str) -> bool:
    """Whether the environment variable ``name`` is set to yes: 1, true, yes or on.

    One reading for every switch that turns a safety check off. Anything else --
    unset, empty, ``0``, ``false``, ``no``, a typo -- leaves the check on.
    """
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


#: Everything install.sh copies into a host project. Excluded from the review
#: diff so that a host project's first review is not spent on the reviewer's
#: own source. Kept in sync with install.sh by a test.
VENDORED_PATHS: tuple[str, ...] = (
    ".claude/agents/ensemble-review.md",
    # Copied into host projects by install.sh, so a host project reviewing
    # itself was reviewing this document -- which is not its code, has not
    # changed, and is 100 lines of budget spent on the agent's own manual.
    # The drift test caught it the moment the installer learned to copy it.
    "docs/codex-sandbox.md",
    "docs/agy-sandbox.md",
    "docs/ensemble_review_result_schema.json",
    "scripts/scrub_diff.py",
    "scripts/review_preflight.py",
    "scripts/review_until_converged.py",
    "scripts/validate_review_results.py",
    "scripts/preflight",
    "scripts/review_loop",
)


def is_agent_repo(repo: Path) -> bool:
    """Whether ``repo`` is this agent's own checkout rather than a host project.

    install.sh is not among the files it copies, so its presence next to the
    loop package is what distinguishes the source repository from an installed
    copy. Developing the agent must still review the agent.

    And only the checkout this loop runs from, or a worktree of its repository: a
    host project reviewed with --repo from the agent's checkout may have an install.sh
    of its own. (A host running its vendored copy on itself, with one, is not told
    apart: the files are all the branch's.)
    """
    if not (
        (repo / "install.sh").is_file() and (repo / "scripts" / "review_loop").is_dir()
    ):
        return False
    return _common_dir(repo) == _common_dir(REPO_ROOT)


def _common_dir(repo: Path) -> Path | None:
    """The repository ``repo`` is a worktree of, by its git common directory."""
    try:
        found = subprocess.run(
            [GIT, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL,
            check=False,
        )  # fmt: skip
    except OSError:
        return None
    if found.returncode != 0 or not found.stdout.strip():
        return repo.resolve()
    return Path(found.stdout.strip()).resolve()


def diff_pathspec(repo: Path) -> list[str]:
    """Git pathspec limiting the review diff to the host project's own code."""
    if is_agent_repo(repo):
        return ["."]
    return [".", *(f":(exclude){path}" for path in VENDORED_PATHS)]


#: A project that ships its own gate knows better than this one does what its
#: gate is. Checked in order; the first that exists and is executable replaces
#: the four built-in steps entirely.
GATE_SCRIPTS: tuple[str, ...] = (
    "tools/gate.sh",
    "scripts/gate.sh",
    "gate.sh",
)


def project_gate(repo: Path) -> Path | None:
    """The project's own gate script, if it has one."""
    for candidate in GATE_SCRIPTS:
        path = repo / candidate
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def interpreter(repo: Path) -> str:
    """The Python that can import the project under review.

    ``sys.executable`` is this agent's interpreter, which for any project with
    its own virtualenv -- uv, poetry, plain venv -- cannot import the package
    being reviewed. Running ``python -m pytest`` with it collected nothing but
    ``ModuleNotFoundError``, so both the coverage measurement and the mandatory
    gate reported failure for every such project, and no round could ever
    commit.
    """
    venv = repo / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def reviewer_timeout(n_lines: int) -> int:
    """How long a reviewer gets, for a diff of this size.

    Flat timeouts fail in the direction that costs most. A reviewer that is
    still working at the deadline is not stuck, it is *reading*, and killing it
    discards the whole round's most detailed output -- measured, the slot that
    times out most is also the one with the highest findings-per-round.
    """
    scaled = REVIEWER_TIMEOUT + int(n_lines * REVIEWER_SECONDS_PER_LINE)
    override = os.environ.get("REVIEWER_TIMEOUT")
    if override and override.isdigit():
        return int(override)
    return min(scaled, REVIEWER_TIMEOUT_MAX)
