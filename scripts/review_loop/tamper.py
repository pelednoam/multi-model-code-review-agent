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
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import paths
from .config import GIT
from .merge_agent import MERGE_WRITE_DENIED
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
    _prepare()
    control = _control_lines(repo)
    # And what the round wrote for the reviewers before they started, which they may
    # not rewrite either: what they were given is what the round reports they read.
    inputs = (
        []
        if exclude is None
        else [
            f"round {_shown(exclude / name)} {_fingerprint(exclude / name, False)}"
            for name in _ROUND_INPUTS
        ]
    )
    # Compared whole: one deleted (an attributes file, a hook) changes what git runs too.
    if before is not None and set(control) != {
        line for line in before.splitlines() if line.startswith(_CONTROL)
    }:
        skipped = "?? git status not run: its control files changed"
        return "\n".join([*control, *inputs, skipped])
    return "\n".join(sorted({*_status_lines(repo, exclude), *control, *inputs})) + "\n"


def _prepare() -> None:
    """What a snapshot sets up in this process: no bytecode written into the tree from
    here on -- a module the loop imports late would otherwise leave a __pycache__ the
    check calls a write -- and where this process's output goes (``_OWN_OUTPUTS``)."""
    sys.dont_write_bytecode = True
    _OWN_OUTPUTS.clear()
    _OWN_OUTPUTS.update(_own_outputs())


#: How ``_control_lines``' records start.
_CONTROL = ("git ", "home ", "exe ", "path ")

#: The home's directories whose files run with no exec bit needed -- sourced by the
#: next shell, started at the next login -- relative to the home, or to
#: $XDG_CONFIG_HOME for those under .config.
_HOME_RUN_DIRS = (".bashrc.d", ".oh-my-zsh/custom", "Library/LaunchAgents")
_XDG_RUN_DIRS = ("autostart", "systemd/user", "environment.d", "fish/conf.d")

#: What the loop and its reviewers run by name, found on PATH: a reviewer that rewrote
#: one (an npm CLI's script keeps its exec bit) had its code run by the next round.
_PROGRAMS = (
    "git", "claude", "codex", "agy", "gemini", "hermes", "bwrap", "node", "python3",
    "ruff", "mypy", "pytest", "sh", "bash", "env", "timeout", "setsid",
)  # fmt: skip


def _status_lines(repo: Path, exclude: Path | None) -> list[str]:
    """A line for each path ``git status`` lists, with what is in it."""
    # Every filter configured switched off, and the hooks: a filter already set up runs
    # a script in the tree (python3 tools/normalize.py), which a reviewer can rewrite
    # without touching a control file, and git status runs it on a file it changed.
    off = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    for name in _filters(repo):
        for key, value in (("clean", ""), ("process", ""), ("required", "false")):
            off += ["-c", f"filter.{name}.{key}={value}"]
    cmd = [
        GIT, *off, "status", "--porcelain=v1", "-z",
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


def _filters(repo: Path) -> set[str]:
    """The names of the filters configured for ``repo`` and its submodules, by reading
    their configs: ``git config`` runs none of them."""
    configs: list[list[str]] = [[]]  # the repository's own, with the global and system
    for git_dir in paths.git_dirs(repo):
        configs += [["--file", str(found)] for found in _module_configs(git_dir)]
    names = set()
    for where in configs:
        found = subprocess.run(
            [GIT, "config", *where, "--name-only", "--get-regexp", r"^filter\."],
            cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL,
            check=False,
        )  # fmt: skip
        for key in found.stdout.splitlines():
            # filter.<name>.<key>, and a name may hold dots.
            if key.count(".") >= 2:
                names.add(key.split(".", 1)[1].rsplit(".", 1)[0])
    return names


def _module_configs(git_dir: Path) -> list[Path]:
    """The configs of the submodules under ``git_dir/modules``, which git status reads
    when it looks inside each."""
    modules = git_dir / "modules"
    return sorted(modules.rglob("config")) if modules.is_dir() else []


def _control_lines(repo: Path) -> list[str]:
    """A line for each file outside git's own listing that a reviewer's write turns
    into a command the loop runs, read without running git on the tree."""
    lines = []
    # The same git directories the merge agent may not write, the hooks directory git
    # runs from among them (core.hooksPath can put it anywhere): scanned whole when it
    # is that, and otherwise for what turns a write into a command.
    for git_dir in paths.git_dirs(repo):
        names = ("config", "config.worktree", "hooks", "info")
        parts = (
            [git_dir]
            if not (git_dir / "HEAD").exists()
            else [git_dir / n for n in names]
        )
        # And each submodule's, whose config and hooks git status reads in it too.
        for config in _module_configs(git_dir):
            parts += [config.parent / n for n in names]
        for part in parts:
            for control in _files(part):
                lines.append(f"git {_shown(control)} {_fingerprint(control, False)}")
    # And the home's files that run code: hermes writes under --yolo, outside the
    # repository too, and a write to ~/.bashrc is no less a reviewer's for that. Files
    # named, not the directories (~/.config/**) the merge agent is kept out of whole.
    for home in paths.homes():
        for pattern in MERGE_WRITE_DENIED:
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
        # And the directories whose files run unasked (_HOME_RUN_DIRS).
        for run_dir in [
            *(home / name for name in _HOME_RUN_DIRS),
            *(xdg / name for name in _XDG_RUN_DIRS),
        ]:
            for runs in _files(run_dir):
                lines.append(f"home {_shown(runs)} {_fingerprint(runs, True)}")
    # And what runs next by name: each program the loop runs, where PATH finds it and
    # what it resolves to (_PROGRAMS); and what is in each directory on PATH this user
    # can write, where one planted under any of their names would be found first.
    for name in _PROGRAMS:
        found = shutil.which(name)
        if found:
            for program in dict.fromkeys((Path(found), Path(found).resolve())):
                lines.append(f"exe {_shown(program)} {_fingerprint(program, True)}")
    for entry in dict.fromkeys(os.environ.get("PATH", "").split(os.pathsep)):
        where = Path(entry or ".")
        if where.is_absolute() and where.is_dir() and os.access(where, os.W_OK):
            with contextlib.suppress(OSError):
                for program in sorted(where.iterdir()):
                    lines.append(
                        f"path {_shown(program)} {_fingerprint(program, True)}"
                    )
    return sorted(set(lines))


def _shown(name: str | Path) -> str:
    """``name`` with what does not print as itself escaped: a snapshot is a line per
    path, and a reviewer's file name with a line break in it was two."""
    # As JSON, ASCII only: one name to one string, which the escapes made by hand were
    # not -- a backslash, or a short escape followed by hex digits, read as another.
    return json.dumps(str(name), ensure_ascii=True)


def _files(path: Path) -> list[Path]:
    """``path`` if it is a file, or the files under it if it is a directory."""
    if path.is_file() or path.is_symlink():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() or p.is_symlink())
    return []


def _fingerprint(path: Path, cheap: bool) -> str:
    """What is in ``path``: its content's hash, a link's target, or -- ``cheap``, for
    the many files under an ignored path -- its size and mtime. With its mode: a hook
    made executable runs at the next commit, and its bytes are the same. One lstat a
    file: it is called for every file under every ignored path, twice a round."""
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            # And what it leads to: a write through an existing link to a startup file
            # or a hook changes the target, and the link alone looked the same.
            try:
                target = path.resolve()
            except RuntimeError:  # a loop of links, which raised and ended the review
                return f"link:{_shown(os.readlink(path))}:loop"
            through = _fingerprint(target, cheap) if target.is_file() else "dangling"
            # Escaped as a name is: a reviewer chooses a link's target as freely.
            return f"link:{_shown(os.readlink(path))}:{through}"
        mode = f"{info.st_mode:o}"
        if (info.st_dev, info.st_ino) in _OWN_OUTPUTS:
            # The loop's own output, redirected into the tree (> data/reviews/run.log):
            # it grows with every line printed, and is no reviewer's doing.
            return "the loop's output"
        if stat.S_ISDIR(info.st_mode):
            # What is under it: a submodule, or a repository inside the tree, is one
            # entry in git's listing however many of its files a reviewer changed.
            digest = hashlib.sha256()
            for record in _tree(path):
                digest.update(record.encode("utf-8", "surrogateescape") + b"\0")
            return f"dir:{mode}:{digest.hexdigest()}"
        if not stat.S_ISREG(info.st_mode):
            # A FIFO or a device, which is not read: opening the one waits for a writer,
            # and reading the other may never end.
            return f"special:{mode}:{info.st_rdev}"
        if cheap:
            return f"{mode}:{info.st_size}:{info.st_mtime_ns}"
        return f"{mode}:{_content_hash(path)}"
    except OSError as exc:
        return f"unreadable:{exc.errno}"


def _tree(top: Path) -> list[str]:
    """A record for each entry under ``top`` -- its path, mode, size and mtime, or a
    link's target -- from one scandir walk, links not followed: sorted, so that the
    same tree gives the same records."""
    records = []
    pending = [top]
    while pending:
        here = pending.pop()
        try:
            entries = list(os.scandir(here))
        except OSError as exc:
            records.append(f"{_shown(here.relative_to(top))} unreadable:{exc.errno}")
            continue
        for entry in entries:
            name = _shown(Path(entry.path).relative_to(top))
            try:
                info = entry.stat(follow_symlinks=False)
                if (info.st_dev, info.st_ino) in _OWN_OUTPUTS:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    records.append(f"{name} link:{_shown(os.readlink(entry.path))}")
                    continue
                records.append(
                    f"{name} {info.st_mode:o}:{info.st_size}:{info.st_mtime_ns}"
                    if not stat.S_ISDIR(info.st_mode)
                    else f"{name} dir:{info.st_mode:o}"
                )
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
            except OSError as exc:
                records.append(f"{name} unreadable:{exc.errno}")
    return sorted(records)


def _content_hash(path: Path) -> str:
    """The SHA-256 of ``path``'s content, streamed -- a large untracked file, a build
    artefact, was read whole twice a round -- and only if what was opened is a regular
    file: one swapped for a FIFO after it was looked at does not block the open."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "special"
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: Files the loop's output is copied into by another process (``tee``), named with
#: ``--log-file``: no reviewer's write either.
LOG_FILES: set[Path] = set()


#: ``_own_outputs()``, as it was when the snapshot being taken began: looked up once,
#: not for each of the many files under an ignored path.
_OWN_OUTPUTS: set[tuple[int, int]] = set()


def _own_outputs() -> set[tuple[int, int]]:
    """The files this process's stdout and stderr go to, when they are files, and
    ``LOG_FILES``."""
    found = set()
    for log in LOG_FILES:
        with contextlib.suppress(OSError):
            info = log.stat()
            found.add((info.st_dev, info.st_ino))
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
    r"(?:diff\.patch|audit\.json|(?:prompt|backend|raw|stderr|codex-out)-[1-4]\.txt)"
)

#: Of those, what is there before the reviewers start.
_ROUND_INPUTS = ("diff.patch", "audit.json")


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
