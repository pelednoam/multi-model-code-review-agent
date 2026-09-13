"""Configuration constants for the convergence loop."""

from __future__ import annotations

from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent

REVIEWER_TIMEOUT = 600
MERGE_TIMEOUT = 900
TEST_TIMEOUT = 300

#: Everything install.sh copies into a host project. Excluded from the review
#: diff so that a host project's first review is not spent on the reviewer's
#: own source. Kept in sync with install.sh by a test.
VENDORED_PATHS: tuple[str, ...] = (
    ".claude/agents/ensemble-review.md",
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
    """
    return (repo / "install.sh").is_file() and (
        repo / "scripts" / "review_loop"
    ).is_dir()


def diff_pathspec(repo: Path) -> list[str]:
    """Git pathspec limiting the review diff to the host project's own code."""
    if is_agent_repo(repo):
        return ["."]
    return [".", *(f":(exclude){path}" for path in VENDORED_PATHS)]
