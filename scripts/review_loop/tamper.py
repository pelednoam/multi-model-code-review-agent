"""Noticing when a reviewer wrote to the tree it was reviewing.

Reviewers are told to read and report. They are not told to edit, and the
loop's own ``--report-only`` says in as many words that it "does not touch the
working tree". Neither statement is enforced by the thing that makes it: a
reviewer is a CLI agent, and what it may do is whatever its own flags allow.

The claude backend is confined by ``--disallowedTools`` and was verified with a
canary; the comment in ``_claude_cmd`` records that ``--allowedTools`` alone was
not enough. The hermes backend runs ``--yolo`` with the ``file`` toolset, which
is every file operation with no approval prompt -- so it can write anywhere it
can reach, including the repository under review -- and nothing checks whether
it did.

**No confirmed instance.** A 96 KB document appearing in a reviewed project's
``docs/`` looked like one and was not: it was an outside review a person had
put there, and attributing it to a reviewer was wrong. What remains is an
unrestricted capability with nothing watching its outcome, plus one unexplained
artefact -- an untracked ``analysis/provenance.py`` belonging to an entirely
different project, sitting in this repository's own tree.

Enough to justify looking; not enough to justify a prohibition. So this
reports and does not fail the round. A capability that can write into the tree
under review should leave a trace somebody reads, whether or not it has been
exercised yet -- and the cost is one ``git status`` per round.

Rather than chase each backend's flags, this watches the outcome. Take the
tree's state before the reviewers run and compare it after: anything that moved
is a reviewer that wrote, whichever one it was and whatever it was allowed. A
backend added tomorrow is covered on the day it is added.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def snapshot(repo: Path) -> str:
    """Everything git can see about the working tree, as one string.

    ``--porcelain`` covers tracked modifications, staged changes and untracked
    files, which is all three of the ways a reviewer can leave a mark. It does
    not cover a file whose content changed and changed back, which is not a
    shape anything here produces.
    """
    done = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout


def changed(before: str, after: str) -> list[str]:
    """Every line the reviewers added to the tree's status, if any."""
    was = set(before.splitlines())
    return sorted(line for line in after.splitlines() if line not in was)


def report(repo: Path, before: str) -> list[str]:
    """Say what the reviewers wrote, loudly, and hand back the list.

    Loudly on purpose. A reviewer writing into the repository it is reviewing
    is not a small thing: it puts text nobody wrote next to text somebody did,
    in a tree whose next command is very often ``git add -A``.
    """
    touched = changed(before, snapshot(repo))
    if not touched:
        return []
    print("\nWARNING: the reviewers changed the working tree. They must not.")
    print("A reviewer reads and reports; anything below was written by one of")
    print("them, and is not your work. Inspect before committing:")
    for line in touched:
        print(f"  {line}")
    return touched
