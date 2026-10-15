"""Commit and push after a successful round."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .proc import run

if TYPE_CHECKING:
    from pathlib import Path


def commit_and_push(round_num: int, n_fixes: int, repo: Path) -> bool:
    """Commit and push current changes."""
    run(["git", "add", "-A"], cwd=repo)
    status = run(["git", "status", "--short"], cwd=repo)
    if not status.stdout.strip():
        print(f"  Nothing to commit after round {round_num}")
        return True
    msg = (
        f"Round {round_num}: apply {n_fixes} fixes from ensemble review\n\n"
        "Applied by merge agent (clean context).\n\n"
        "Co-Authored-By: review-loop <noreply@anthropic.com>"
    )
    commit = run(["git", "commit", "-m", msg], cwd=repo)
    if commit.returncode != 0:
        print(f"  Commit failed: {commit.stderr}")
        return False
    push = run(["git", "push"], cwd=repo)
    if push.returncode != 0:
        print(f"  Push failed: {push.stderr}")
        return False
    return True
