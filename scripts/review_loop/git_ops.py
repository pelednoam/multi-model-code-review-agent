"""Commit and push after a successful round."""

from __future__ import annotations

from pathlib import Path

from .backends import _run


def commit_and_push(round_num: int, n_fixes: int, repo: Path) -> bool:
    """Commit and push current changes."""
    _run(["git", "add", "-A"], cwd=repo)
    status = _run(["git", "status", "--short"], cwd=repo)
    if not status.stdout.strip():
        print(f"  Nothing to commit after round {round_num}")
        return True
    msg = (
        f"Round {round_num}: apply {n_fixes} fixes from ensemble review\n\n"
        "Applied by merge agent (clean context).\n\n"
        "Co-Authored-By: review-loop <noreply@anthropic.com>"
    )
    commit = _run(["git", "commit", "-m", msg], cwd=repo)
    if commit.returncode != 0:
        print(f"  Commit failed: {commit.stderr}")
        return False
    push = _run(["git", "push"], cwd=repo)
    if push.returncode != 0:
        print(f"  Push failed: {push.stderr}")
        return False
    return True
