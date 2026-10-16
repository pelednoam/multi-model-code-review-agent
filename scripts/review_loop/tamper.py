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

import contextlib
import hashlib
import os
import re
import subprocess
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def snapshot(repo: Path, exclude: Path | None = None) -> str:
    """Everything git can see about the working tree, as one string: a line per path,
    with what is in it.

    ``--porcelain`` lists tracked modifications, staged changes and untracked files,
    and ``--ignored=traditional`` every file under ignored paths -- a reviewer's
    write into an existing .venv or .claude was invisible without it. Each with a
    fingerprint of its content (its size and mtime, for an ignored one): a status
    line alone did not change when a file already modified, or already untracked,
    was written again. And the repository's own git control files -- hooks, config,
    info -- where a write becomes a command the loop's own git runs.

    ``exclude`` is the round's own directory, when it is inside the repository:
    the round writes its outputs there, and they are no reviewer's doing. Not
    covered: a change to an ignored file that keeps its size and mtime.
    """
    # No bytecode written into the tree from here on, by this process: a module the
    # loop imports late would otherwise leave a __pycache__ the check calls a write.
    sys.dont_write_bytecode = True
    cmd = [
        "git", "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z",
        "--untracked-files=all", "--ignored=traditional", "--", ".",
    ]  # fmt: skip
    if exclude is not None:
        with contextlib.suppress(ValueError):
            # literal: its name is a path, not a glob ([ ] in it matched something else).
            relative = exclude.resolve().relative_to(repo.resolve())
            cmd.append(f":(exclude,literal){relative}")
    done = subprocess.run(
        cmd, cwd=repo, capture_output=True, stdin=subprocess.DEVNULL, check=False
    )
    if done.returncode != 0:
        # Not knowing is not "unchanged": a line no other snapshot has.
        return f"?? git status failed ({done.returncode}) at {time.monotonic_ns()}\n"
    lines = []
    entries = done.stdout.decode("utf-8", errors="replace").split("\0")
    skip = False
    for entry in entries:
        if skip:  # a rename's or copy's source path
            skip = False
            continue
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        skip = status[0] in "RC"
        lines.append(f"{status} {path} {_fingerprint(repo / path, status == '!!')}")
    # The same git directories the merge agent may not write, the hooks directory git
    # runs from among them (core.hooksPath can put it anywhere): scanned whole when it
    # is that, and otherwise for what turns a write into a command.
    from .merge_agent import _MERGE_WRITE_DENIED, _git_dirs, _homes

    for git_dir in _git_dirs(repo):
        names = ("config", "config.worktree", "hooks", "info")
        parts = (
            [git_dir]
            if not (git_dir / "HEAD").exists()
            else [git_dir / n for n in names]
        )
        for part in parts:
            for control in _files(part):
                lines.append(f"git {control} {_fingerprint(control, False)}")
    # And the home's files that run code: hermes writes under --yolo, outside the
    # repository too, and a write to ~/.bashrc is no less a reviewer's for that. Files
    # named, not the directories (~/.config/**) the merge agent is kept out of whole.
    for home in _homes():
        for pattern in _MERGE_WRITE_DENIED:
            if pattern.endswith("/**") or any(c in pattern for c in "*?["):
                continue
            startup = home / pattern[2:]
            lines.append(f"home {startup} {_fingerprint(startup, False)}")
        for extra in (".ssh/authorized_keys", ".claude/settings.json"):
            lines.append(f"home {home / extra} {_fingerprint(home / extra, False)}")
    return "\n".join(sorted(set(lines))) + "\n"


def _files(path: Path) -> list[Path]:
    """``path`` if it is a file, or the files under it if it is a directory."""
    if path.is_file() or path.is_symlink():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() or p.is_symlink())
    return []


def _fingerprint(path: Path, cheap: bool) -> str:
    """What is in ``path``: its content's hash, a link's target, or -- ``cheap``, for
    the many files under an ignored path -- its size and mtime."""
    try:
        if path.is_symlink():
            return "link:" + os.readlink(path)
        if path.is_dir():
            return "dir"
        if cheap:
            info = path.stat()
            return f"{info.st_size}:{info.st_mtime_ns}"
        # Streamed: a large untracked file, a build artefact, was read whole twice a round.
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        return f"unreadable:{exc.errno}"


def changed(before: str, after: str) -> list[str]:
    """What the reviewers changed: each path whose line is new or different, and
    each that is gone."""
    was, now = set(before.splitlines()), set(after.splitlines())
    gone = [f"gone: {line}" for line in sorted(was - now)]
    return sorted(now - was) + gone


#: What the round itself writes into its directory by the time the check runs: the
#: diff and audit before the reviewers start, each slot's prompt and backend, and the
#: outputs published once they stop (``reviewers._OUTPUTS``).
_ROUND_FILES = re.compile(
    r"(?:diff\.patch|audit\.json|\.audit\.raw\.json"
    r"|(?:prompt|backend|raw|stderr|codex-out)-\d\.txt)"
)


def _strangers(round_dir: Path) -> list[str]:
    """Anything in the round's directory the round did not write: excluded from the
    tree's snapshot as a whole, it was a place a reviewer could plant a file unseen,
    for --auto-commit's `git add -A` to commit where the directory is in the tree."""
    try:
        names = sorted(entry.name for entry in round_dir.iterdir())
    except OSError:
        return []
    return [
        f"?? {round_dir / name}" for name in names if not _ROUND_FILES.fullmatch(name)
    ]


def report(repo: Path, before: str, exclude: Path | None = None) -> list[str]:
    """Say what the reviewers wrote, loudly, and hand back the list.

    Loudly on purpose. A reviewer writing into the repository it is reviewing
    is not a small thing: it puts text nobody wrote next to text somebody did,
    in a tree whose next command is very often ``git add -A``.
    """
    touched = changed(before, snapshot(repo, exclude))
    if exclude is not None:
        touched += _strangers(exclude)
    if not touched:
        return []
    print("\nWARNING: the reviewers changed the working tree. They must not.")
    print("A reviewer reads and reports; anything below was written by one of")
    print("them, and is not your work. Inspect before committing:")
    for line in touched:
        print(f"  {line}")
    return touched
