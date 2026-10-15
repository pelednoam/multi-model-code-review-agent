"""agy's sandbox: the bubblewrap wrapper it runs under.

agy cannot make itself read-only (the measurements are in the ``agy`` module's
docstring), so the boundary is the operating system's. Measured under this wrapper with
--mode=accept-edits, the worst case, agy's own write_to_file failed with "read-only
file system". ``agy_sandbox_check`` proves it. What it closes and what it does not is
docs/agy-sandbox.md.
"""

from __future__ import annotations

import contextlib
import functools
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

try:
    import pwd
except ImportError:  # not a Unix: agy's sandbox is Linux-only, the loop is not
    pwd = None  # type: ignore[assignment]


# -- the sandbox ---------------------------------------------------------------


def agy_state_dir() -> Path:
    """Where agy keeps its sign-in, conversations, plans and helper binaries.

    Raises OSError, as every other failure to set agy up does, when there is no home
    to find it in -- HOME unset, and no passwd entry -- so that agy is dropped rather
    than detection stopping with RuntimeError.
    """
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise OSError(f"cannot find agy's home directory ({exc})") from exc
    # Resolved, as the homes the sandbox hides are: under a HOME that is a symlink,
    # the copy was mounted at the link's path, outside the home hidden at its target,
    # and the check found it there as something the wrapper had not put back.
    return home.resolve() / ".gemini" / "antigravity-cli"


#: Inside agy's state directory: what a sandboxed run gets mounted read-only -- its own
#: helper binaries, which it must not be able to change: written to, they would be a
#: place to plant a program for the next *unsandboxed* agy -- and what it gets a
#: per-run copy of (the sign-in token, which agy refreshes).
_AGY_STATE_SHARED = ("bin", "builtin")
_AGY_STATE_COPIED = ("antigravity-oauth-token",)

#: Sockets outside /run and /tmp that run code for whoever can connect -- docker's runs
#: a container with the host filesystem mounted -- masked where they exist. The check
#: refuses any other socket left reachable (``_check_sockets``); these are the ones
#: known to be common enough to mask rather than lose the slot over.
_HOST_SOCKETS = (
    "/var/run/docker.sock",
    "/var/snap/lxd/common/lxd/unix.socket",
    "/var/lib/lxd/unix.socket",
    "/var/lib/incus/unix.socket",
)

#: The only environment agy gets. The rest -- API keys, cloud tokens, socket addresses --
#: is one read of /proc/self/environ away from a file tool, with the network open.
#: Given to bwrap by ``env -i``, not as ``--setenv`` arguments: bwrap's command line is
#: world-readable in /proc for the whole run, and a proxy URL can carry a password.
#: env's own command line holds the values only until it execs the next program.
_AGY_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "TERM", "TZ",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)  # fmt: skip


def agy_seed_state(state: Path) -> Path:
    """Fill a fresh directory to stand in for agy's state during one sandboxed run.

    agy's real state is not mounted writable: a run that could write it could leave a
    ``settings.json`` granting every command, for the next unsandboxed agy to obey. It
    gets a copy of the sign-in token and nothing else; the helpers are mounted
    read-only over it by ``agy_wrapper``. Conversations and plans land here and are
    thrown away with it.
    """
    real = agy_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    for name in _AGY_STATE_COPIED:
        if (real / name).is_file():
            shutil.copy2(real / name, state / name)
    return state


def user_homes() -> list[Path]:
    """Every directory that is this user's home: ``$HOME``, and the account's own.

    They differ wherever HOME is pointed elsewhere -- CI, ``sudo -E``, a wrapper script,
    a nested agent -- and hiding only ``$HOME`` then leaves ``~/.ssh`` readable. Parents
    first, so a home inside the other is mounted on top of it, not under it.

    Raises OSError when a home cannot be hidden, and the sandbox check then fails rather
    than the run going on with a home it could not account for: an account with no
    passwd entry (a container under a bare numeric UID), whose home is unknown; or a
    home of ``/``, which cannot be replaced without replacing everything.
    """
    if pwd is None:
        raise OSError("no passwd database on this platform; agy's sandbox needs Linux")
    try:
        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError as exc:
        raise OSError(
            f"uid {os.getuid()} has no passwd entry; its home is unknown"
        ) from exc
    homes = {Path.home().resolve(), account.resolve()}
    if Path("/") in homes:
        raise OSError("a home directory of / cannot be hidden from agy")
    return sorted(homes, key=lambda h: len(h.parts))


def _resolver_file() -> Path | None:
    """The file under /run or /tmp that /etc/resolv.conf points to, or None.

    It is usually a link into /run (systemd-resolved, NetworkManager), which the
    sandbox replaces, and DNS needs it back. The file alone, not its directory:
    systemd-resolved keeps a varlink socket beside it, which answers more than DNS.
    """
    resolver = Path("/etc/resolv.conf").resolve()
    if resolver.is_file() and any(
        resolver.is_relative_to(hidden) for hidden in ("/run", "/tmp")
    ):
        return resolver
    return None


@functools.cache
def _bwrap_supports(flag: str) -> bool:
    """Whether the installed bwrap knows ``flag`` (``bwrap --help`` lists it)."""
    exe = shutil.which("bwrap")
    if exe is None:
        return False
    try:
        done = subprocess.run(
            [exe, "--help"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return flag in done.stdout + done.stderr


def _maskable(sock: str) -> bool:
    """Whether ``sock`` exists outside /run, which the sandbox replaces anyway.

    One the user cannot even stat -- under a root-only directory -- is one the user
    cannot reach, so it needs no mask; that is not an error to lose the slot over.
    """
    try:
        return os.path.lexists(sock) and not Path(sock).resolve().is_relative_to("/run")
    except OSError:
        return False


@contextlib.contextmanager
def scratch_dirs() -> Iterator[tuple[Path, Path]]:
    """A workspace and a state directory for one sandboxed agy run outside a round
    (the model listing, the check): empty, resolved, removed on the way out. A round's
    are kept with the round's other scratch instead (``reviewers._scratch_dir``)."""
    with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as st:
        yield Path(ws).resolve(), Path(st).resolve()


def agy_wrapper(workspace: Path, state: Path, hidden: Iterable[Path] = ()) -> list[str]:
    """The bubblewrap prefix agy runs under: ``agy_wrapper_parts``, joined."""
    env, bwrap = agy_wrapper_parts(workspace, state, hidden)
    return [*env, *bwrap]


def agy_wrapper_parts(
    workspace: Path, state: Path, hidden: Iterable[Path] = ()
) -> tuple[list[str], list[str]]:
    """The bubblewrap prefix agy runs under, as ``env -i VAR=val...`` and bwrap's part.

    Everything is read-only except ``workspace``. The parts are separate so that a
    launcher can put a command between them: the bash runner's ``timeout`` goes after
    env, so its command line, readable by any local user in /proc, holds no values.

    It replaces the home directories (``user_homes``), the directories in ``hidden``
    (the repository under review, this checkout), /tmp and /run with empty ones --
    putting back only the resolver's file for DNS -- masks ``_HOST_SOCKETS``,
    gives agy a throwaway copy of its state (``agy_seed_state``), runs bwrap as PID 1
    of a new process namespace and session, and clears the environment to
    ``_AGY_ENV`` with ``env -i`` before bwrap starts: bwrap's own /proc/1/environ
    would otherwise hand every API key to a file read. What that closes and what it
    does not -- the network, mainly -- is docs/agy-sandbox.md; ``agy_is_confined``
    proves it.

    The bash launcher in docs/ensemble-review.md builds its wrapper by calling this
    (through ``runner_cli``), so there is one copy.
    """
    real = agy_state_dir()
    # bwrap by absolute path: env -i has emptied PATH by the time it is looked up.
    wrap = [
        shutil.which("bwrap") or "bwrap",
        "--unshare-pid",
        "--unshare-ipc",
        # A user namespace of its own, holding no capabilities: run as root, bwrap
        # otherwise keeps them all, and a remount would undo every read-only bind.
        "--unshare-user",
        "--cap-drop", "ALL",
        # Its own hostname and cgroup view too, where the kernel allows the latter.
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--new-session",
        "--die-with-parent",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
    ]  # fmt: skip
    # And no user namespace of its own inside, where it could map itself root again
    # and mount over what this one made read-only (bwrap 0.8 and later).
    if _bwrap_supports("--disable-userns"):
        wrap.append("--disable-userns")
    resolver = _resolver_file()
    if resolver is not None:
        wrap += ["--ro-bind", str(resolver), str(resolver)]
    for sock in _HOST_SOCKETS:
        if _maskable(sock):
            wrap += ["--ro-bind", "/dev/null", sock]
    for home in user_homes():
        wrap += ["--tmpfs", str(home)]
    # The repository under review and this agent's checkout, wherever they are:
    # outside a home (/srv, a CI checkout) they were readable whole, .env files and
    # all, by a model that only ever needs the scrubbed diff it is sent. Not /, which
    # cannot be hidden without hiding everything.
    for directory in dict.fromkeys(p.resolve() for p in hidden):
        if directory != Path("/"):
            wrap += ["--tmpfs", str(directory)]
    exe = shutil.which("agy")
    if exe:
        target = str(Path(exe).resolve())
        for path in dict.fromkeys((exe, target)):
            wrap += ["--ro-bind", target, path]
    wrap += ["--bind", str(state), str(real)]
    for name in _AGY_STATE_SHARED:
        if (real / name).is_dir():
            wrap += ["--ro-bind", str(real / name), str(real / name)]
    wrap += ["--bind", str(workspace), str(workspace)]
    keep = [f"{var}={os.environ[var]}" for var in _AGY_ENV if var in os.environ]
    return [shutil.which("env") or "env", "-i", *keep], [*wrap, "--"]
