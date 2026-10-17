"""Where a user's homes and a repository's git directories are: what the merge agent
may not write, what the tamper check watches, and what agy's sandbox hides -- found
one way, for all three."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Literal, overload

from .config import GIT
from .proc import run

try:
    import pwd
except ImportError:  # not on Windows
    pwd = None  # type: ignore[assignment]


def env_home() -> Path | None:
    """``$HOME``, resolved; None where there is none to find (no HOME, and no passwd
    entry to fall back on)."""
    with contextlib.suppress(OSError, RuntimeError):
        return Path.home().resolve()
    return None


@overload
def account_home(strict: Literal[True]) -> Path: ...
@overload
def account_home(strict: bool = False) -> Path | None: ...
def account_home(strict: bool = False) -> Path | None:
    """The account's own home, from the passwd database, resolved. None where there is
    no such entry -- a container under a bare numeric UID -- or, ``strict``, OSError
    saying why."""
    if pwd is None:
        if strict:
            raise OSError("no passwd database on this platform")
        return None
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (KeyError, OSError, RuntimeError) as exc:
        if strict:
            raise OSError(
                f"uid {os.getuid()} has no passwd entry; its home is unknown"
            ) from exc
        return None


def homes() -> set[Path]:
    """$HOME and the account's own home, which differ where HOME is pointed elsewhere
    (CI, ``sudo -E``, a wrapper script, a nested agent).

    Either may be missing -- a container's user with no HOME and no passwd entry --
    and then there is none of it: the repository's checks stand without.
    """
    return {home for home in (env_home(), account_home()) if home is not None}


def git_dirs(repo: Path) -> list[Path]:
    """The repository's git directories: its .git, and where git says they are -- in
    a linked worktree .git is a file naming one elsewhere, with the shared hooks and
    config in another."""
    dirs = [repo / ".git", repo.resolve() / ".git"]
    # And the hooks directory git runs from, which core.hooksPath can put anywhere.
    found = run(
        [GIT, "rev-parse", "--absolute-git-dir", "--git-common-dir",
         "--git-path", "hooks"],
        cwd=repo,
    )  # fmt: skip
    named = [(repo / line).resolve() for line in found.stdout.split("\n") if line]
    if found.returncode == 0 and named:
        # --git-common-dir and --git-path may be relative, to where git ran.
        *found_dirs, hooks = named
        dirs += found_dirs
        # A hooks directory that holds the checkout (core.hooksPath=.) is not denied or
        # watched whole, which would be the whole repository: its hooks are, by name.
        if repo.resolve().is_relative_to(hooks):
            dirs += [hooks / name for name in GIT_HOOK_NAMES]
        else:
            dirs.append(hooks)
    return list(dict.fromkeys(dirs))


#: The hooks git runs, by the names it runs them by (githooks(5)).
GIT_HOOK_NAMES = (
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit",
    "pre-merge-commit", "prepare-commit-msg", "commit-msg", "post-commit",
    "pre-rebase", "post-checkout", "post-merge", "pre-push", "pre-receive", "update",
    "proc-receive", "post-receive", "post-update", "reference-transaction",
    "push-to-checkout", "pre-auto-gc", "post-rewrite", "sendemail-validate",
    "fsmonitor-watchman", "p4-changelist", "p4-prepare-changelist",
    "p4-post-changelist", "p4-pre-submit", "post-index-change",
)  # fmt: skip
