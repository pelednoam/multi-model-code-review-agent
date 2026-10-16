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
import json
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from .config import GIT
from .results import printable


def snapshot(repo: Path, exclude: Path | None = None, before: str | None = None) -> str:
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

    ``before``, the snapshot taken before the reviewers ran: when the files that make
    git run a command (its config and attributes, a filter's, the hooks, a program
    planted under its name) are not what they were, ``git status`` is not run -- it is
    what would run the reviewer's command -- and the snapshot says so instead.
    """
    # No bytecode written into the tree from here on, by this process: a module the
    # loop imports late would otherwise leave a __pycache__ the check calls a write.
    sys.dont_write_bytecode = True
    control = _control_lines(repo)
    if before is not None and not set(control) <= set(before.splitlines()):
        return "\n".join([*control, "?? git status not run: its control files changed"])
    return "\n".join(sorted({*_status_lines(repo, exclude), *control})) + "\n"


def _status_lines(repo: Path, exclude: Path | None) -> list[str]:
    """A line for each path ``git status`` lists, with what is in it."""
    cmd = [
        GIT, "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z",
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
        return [f"?? git status failed ({done.returncode}) at {time.monotonic_ns()}"]
    lines = []
    # The filesystem's own decoding: with "replace", two names differing in a byte that
    # is not UTF-8 became one, and neither file could be read back to fingerprint.
    entries = os.fsdecode(done.stdout).split("\0")
    skip = False
    for entry in entries:
        if skip:  # a rename's or copy's source path
            skip = False
            continue
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        skip = status[0] in "RC"
        lines.append(
            f"{status} {_shown(path)} {_fingerprint(repo / path, status == '!!')}"
        )
    return lines


def _control_lines(repo: Path) -> list[str]:
    """A line for each file outside git's own listing that a reviewer's write turns
    into a command the loop runs, read without running git on the tree."""
    lines = []
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
                lines.append(f"git {_shown(control)} {_fingerprint(control, False)}")
    # And the home's files that run code: hermes writes under --yolo, outside the
    # repository too, and a write to ~/.bashrc is no less a reviewer's for that. Files
    # named, not the directories (~/.config/**) the merge agent is kept out of whole.
    for home in _homes():
        for pattern in _MERGE_WRITE_DENIED:
            if pattern.endswith("/**") or any(c in pattern for c in "*?["):
                continue
            startup = home / pattern[2:]
            lines.append(f"home {_shown(startup)} {_fingerprint(startup, False)}")
        # And what the loop's own git and ssh read next: their configs, which can name a
        # command to run (core.fsmonitor, a credential helper, ProxyCommand).
        xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        for extra in (
            home / ".ssh" / "authorized_keys", home / ".ssh" / "config",
            home / ".claude" / "settings.json", xdg / "git" / "config",
            xdg / "git" / "attributes",
        ):  # fmt: skip
            lines.append(f"home {_shown(extra)} {_fingerprint(extra, False)}")
        # And the user's own bin directories, early on PATH: a program planted there
        # under git's name, or python's, is what the loop runs next.
        for bin_dir in (".local/bin", "bin", ".cargo/bin"):
            for program in _files(home / bin_dir):
                lines.append(f"home {_shown(program)} {_fingerprint(program, True)}")
    return sorted(set(lines))


def _shown(name: str | Path) -> str:
    """``name`` with what does not print as itself escaped: a snapshot is a line per
    path, and a reviewer's file name with a line break in it was two."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in str(name))


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
            # And what it leads to: a write through an existing link to a startup file
            # or a hook changes the target, and the link alone looked the same.
            try:
                target = path.resolve()
            except RuntimeError:  # a loop of links, which raised and ended the review
                return f"link:{os.readlink(path)}:loop"
            through = _fingerprint(target, cheap) if target.is_file() else "dangling"
            return "link:" + os.readlink(path) + ":" + through
        info = path.stat()
        if (info.st_dev, info.st_ino) in _own_outputs():
            # The loop's own output, redirected into the tree (> data/reviews/run.log):
            # it grows with every line printed, and is no reviewer's doing.
            return "the loop's output"
        if path.is_dir():
            # What is under it: a submodule, or a repository inside the tree, is one
            # entry in git's listing however many of its files a reviewer changed.
            inside = [
                (str(child.relative_to(path)), _fingerprint(child, True))
                for child in _files(path)
            ]
            return "dir:" + hashlib.sha256(json.dumps(inside).encode()).hexdigest()
        if cheap:
            return f"{info.st_size}:{info.st_mtime_ns}"
        # Streamed: a large untracked file, a build artefact, was read whole twice a round.
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        return f"unreadable:{exc.errno}"


def _own_outputs() -> set[tuple[int, int]]:
    """The files this process's stdout and stderr go to, when they are files."""
    found = set()
    for fd in (1, 2):
        with contextlib.suppress(OSError):
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode):
                found.add((info.st_dev, info.st_ino))
    return found


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
    r"|(?:prompt|backend|raw|stderr|codex-out)-[1-4]\.txt)"
)


def _strangers(round_dir: Path) -> list[str]:
    """Anything in the round's directory the round did not write: excluded from the
    tree's snapshot as a whole, it was a place a reviewer could plant a file unseen,
    for --auto-commit's `git add -A` to commit where the directory is in the tree."""
    try:
        entries = sorted(round_dir.iterdir())
    except OSError:
        return []
    # A regular file under one of those names, for a slot there is: a directory or a
    # link planted under one is no output of the round's.
    return [
        f"?? {_shown(entry)}"
        for entry in entries
        if not _ROUND_FILES.fullmatch(entry.name)
        or entry.is_symlink()
        or not entry.is_file()
    ]


def report(repo: Path, before: str, exclude: Path | None = None) -> list[str]:
    """Say what the reviewers wrote, loudly, and hand back the list.

    Loudly on purpose. A reviewer writing into the repository it is reviewing
    is not a small thing: it puts text nobody wrote next to text somebody did,
    in a tree whose next command is very often ``git add -A``.
    """
    touched = changed(before, snapshot(repo, exclude, before))
    if exclude is not None:
        touched += _strangers(exclude)
    if not touched:
        return []
    print("\nWARNING: the reviewers changed the working tree. They must not.")
    print("A reviewer reads and reports; anything below was written by one of")
    print("them, and is not your work. Inspect before committing:")
    for line in touched:
        # A name a reviewer chose: an escape sequence or a line break in it could hide
        # this warning, or forge one.
        print(f"  {printable(line).replace(chr(10), '?')}")
    return touched
