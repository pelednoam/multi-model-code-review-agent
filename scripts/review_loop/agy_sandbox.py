"""agy's sandbox: the bubblewrap wrapper it runs under, and the proof of it.

agy cannot make itself read-only (``agy``'s docstring has the measurements), so the
boundary is the operating system's. Measured under this wrapper with
--mode=accept-edits, the worst case, agy's own write_to_file failed with "read-only
file system". ``agy_is_confined`` proves the wrapper -- with no model call -- before
the slot is used, and ``agy_reachable`` checks again before each launch. What it closes
and what it does not is docs/agy-sandbox.md.
"""

from __future__ import annotations

import os
import pwd
import re
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .proc import run, signals_unwind

if TYPE_CHECKING:
    from collections.abc import Iterator


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
    return home / ".gemini" / "antigravity-cli"


#: Inside agy's state directory: what a sandboxed run gets read-only (its own helper
#: binaries -- writable, they are a place to plant a program the next *unsandboxed* agy
#: runs) and what it gets a per-run copy of (the sign-in token, which agy refreshes).
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
    read-only over it by ``agy_sandbox``. Conversations and plans land here and are
    thrown away with it.
    """
    real = agy_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    for name in _AGY_STATE_COPIED:
        if (real / name).is_file():
            shutil.copy2(real / name, state / name)
    return state


def _homes() -> list[Path]:
    """Every directory that is this user's home: ``$HOME``, and the account's own.

    They differ wherever HOME is pointed elsewhere -- CI, ``sudo -E``, a wrapper script,
    a nested agent -- and hiding only ``$HOME`` then leaves ``~/.ssh`` readable. Parents
    first, so a home inside the other is mounted on top of it, not under it.

    Raises OSError when a home cannot be hidden, and the sandbox check then fails rather
    than the run going on with a home it could not account for: an account with no
    passwd entry (a container under a bare numeric UID), whose home is unknown; or a
    home of ``/``, which cannot be replaced without replacing everything.
    """
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


def _resolver_dir() -> Path | None:
    """The directory under /run that DNS needs back, or None.

    /etc/resolv.conf is usually a link into /run (systemd-resolved, NetworkManager),
    which the sandbox replaces.
    """
    resolver = Path("/etc/resolv.conf").resolve().parent
    if resolver.is_relative_to("/run") and resolver != Path("/run"):
        return resolver
    return None


def _maskable(sock: str) -> bool:
    """Whether ``sock`` exists outside /run, which the sandbox replaces anyway.

    One the user cannot even stat -- under a root-only directory -- is one the user
    cannot reach, so it needs no mask; that is not an error to lose the slot over.
    """
    try:
        return os.path.lexists(sock) and not Path(sock).resolve().is_relative_to("/run")
    except OSError:
        return False


def agy_sandbox(workspace: Path, state: Path) -> list[str]:
    """The bubblewrap prefix agy runs under: ``agy_sandbox_parts``, joined."""
    env, bwrap = agy_sandbox_parts(workspace, state)
    return [*env, *bwrap]


def agy_sandbox_parts(workspace: Path, state: Path) -> tuple[list[str], list[str]]:
    """The bubblewrap prefix agy runs under, as ``env -i VAR=val...`` and bwrap's part.

    Everything is read-only except ``workspace``. The parts are separate so that a
    launcher can put a command between them: the bash runner's ``timeout`` goes after
    env, so its command line, readable by any local user in /proc, holds no values.

    It replaces the home directories (``_homes``), /tmp and /run with empty ones --
    putting back only the resolver's directory for DNS -- masks ``_HOST_SOCKETS``,
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
        "--new-session",
        "--die-with-parent",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
    ]  # fmt: skip
    resolver = _resolver_dir()
    if resolver is not None:
        wrap += ["--ro-bind", str(resolver), str(resolver)]
    for sock in _HOST_SOCKETS:
        if _maskable(sock):
            wrap += ["--ro-bind", "/dev/null", sock]
    for home in _homes():
        wrap += ["--tmpfs", str(home)]
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


# -- proving the sandbox -------------------------------------------------------


def _canary_parent() -> str | None:
    """Where to put a canary the sandbox leaves visible: not /tmp, which it hides."""
    return "/var/tmp" if os.access("/var/tmp", os.W_OK) else None


class _Box:
    """One wrapper under test, and a way to run a shell script inside it."""

    def __init__(self, wrap: list[str], workspace: Path) -> None:
        self.wrap = wrap
        self.workspace = workspace
        self.stderr = ""

    def sh(self, script: str, **kw: Any) -> str:
        """What ``script`` printed inside the sandbox."""
        done = run(
            [*self.wrap, "sh", "-c", script], cwd=self.workspace, timeout=30, **kw
        )
        self.stderr = done.stderr or ""
        return done.stdout or ""

    def mounted_under(self, root: Path) -> set[str]:
        """The names directly under ``root`` that the wrapper mounted there."""
        # Mount destinations only -- an environment value such as a PATH under HOME is
        # not a mount. Paths, not strings: HOME=/home/user/ is the same directory.
        # --tmpfs DEST, and --bind/--ro-bind SRC DEST (and their -try forms).
        dests = []
        for i, arg in enumerate(self.wrap):
            if arg == "--tmpfs":
                dests.append(Path(self.wrap[i + 1]))
            elif arg.endswith("-bind"):
                dests.append(Path(self.wrap[i + 2]))
        return {
            d.relative_to(root).parts[0]
            for d in dests
            if d.is_relative_to(root) and d != root
        }


def _probe(box: _Box, canary: Path, roots: list[Path]) -> dict[str, str]:
    """Everything the check asks inside the sandbox, from one launch of it.

    One script with its answers marked, where one launch per question had the check
    start bwrap a dozen times. The two writes are in it too; whether they landed is
    read from outside.
    """
    # Opens each answer; what follows, up to the next mark, is it. New each run: a file
    # name the sandbox fails to hide can hold a line break and a mark of its own, and
    # one that could be guessed would rewrite the answers after it.
    mark = f"@@mmcra-probe-{secrets.token_hex(16)}@@"
    q = shlex.quote
    questions = {
        "agy": "agy --version >/dev/null 2>&1 && echo AGY_OK",
        # Read first, then try to overwrite.
        "canary": f"cat {q(str(canary))}; echo changed > {q(str(canary))}",
        "workspace": f"echo ok > {q(str(box.workspace / 'probe'))}",
        "pidns": "readlink /proc/self/ns/pid",
        "caps": "grep '^CapEff:' /proc/self/status",
        # Every process's environment, not only the shell's: bwrap is PID 1 inside.
        "env": 'printf %s "$MMCRA_ENV_PROBE"; cat /proc/[0-9]*/environ'
        " | tr '\\0' '\\n' | grep -q '^MMCRA_ENV_PROBE=' && echo LEAK",
        **{f"ls {i}": f"ls -A1 {q(str(root))}" for i, root in enumerate(roots)},
    }
    script = "\n".join(
        f"echo {mark}{key}; {{ {ask}; }} 2>/dev/null" for key, ask in questions.items()
    )
    said = box.sh(script, env={**os.environ, "MMCRA_ENV_PROBE": "x"})
    answers: dict[str, list[str]] = {}
    for line in said.split("\n"):
        if line.startswith(mark):
            answers[line[len(mark) :]] = []
        elif answers:
            answers[next(reversed(answers))].append(line)
    return {key: "\n".join(lines) for key, lines in answers.items()}


def _check_runs(seen: dict[str, str], box: _Box) -> str | None:
    # First: a canary that survives because the sandbox never launched is not a pass.
    # Not one answer means the probe never ran, and why is in bwrap's stderr.
    if seen:
        return None
    detail = (box.stderr.strip().splitlines() or [""])[-1][:200]
    hint = ""
    if "uid map" in detail or "userns" in detail or "namespace" in detail:
        hint = (
            " -- looks like the kernel is blocking unprivileged user namespaces;"
            " see docs/codex-sandbox.md"
        )
    return f"bwrap could not start: {detail}{hint}"


# Each check below reads the probe's answers and returns why the wrapper fails, or None.


def _check_agy_starts(seen: dict[str, str]) -> str | None:
    # A wrapper that hides agy's runtime passes every other check and leaves a slot
    # that never answers.
    return (
        None
        if "AGY_OK" in seen.get("agy", "")
        else "agy does not start inside its sandbox"
    )


def _check_files(seen: dict[str, str], canary: Path, workspace: Path) -> str | None:
    # A reviewer that cannot read the system cannot run.
    if "original" not in seen.get("canary", ""):
        return "bwrap sandbox could not read outside the workspace"
    # The one that matters: was the write outside the workspace REFUSED?
    if not canary.is_file() or canary.read_bytes() != b"original":
        return "bwrap sandbox ALLOWED a write outside the workspace"
    # And inside it, allowed? agy keeps its working files there, and fails without
    # them looking like a reviewer with nothing to say.
    if not (workspace / "probe").is_file():
        return "bwrap sandbox refused a write inside agy's workspace"
    return None


def _check_hidden(seen: dict[str, str], box: _Box, roots: list[Path]) -> str | None:
    for i, root in enumerate(roots):
        listed = {name for name in seen.get(f"ls {i}", "").split("\n") if name}
        extra = listed - box.mounted_under(root)
        if extra:
            return f"bwrap sandbox shows {root}/{sorted(extra)[0]}"
    return None


def _check_processes(seen: dict[str, str]) -> str | None:
    # A namespace, compared as one: a host PID can exist inside too (the loop as PID 1
    # in a container), so looking one up proves nothing.
    inner = seen.get("pidns", "").strip()
    if not inner or inner == os.readlink("/proc/self/ns/pid"):
        return "bwrap sandbox shares the host's processes"
    return None


def _check_capabilities(seen: dict[str, str]) -> str | None:
    # Run as root, bwrap without a user namespace kept every capability, and a
    # process inside could remount the read-only filesystem writable.
    held = seen.get("caps", "").split()
    if held[:1] != ["CapEff:"] or held[1:] != ["0" * len(held[-1])]:
        return "bwrap sandbox keeps capabilities, so it is not read-only"
    return None


def _check_environment(seen: dict[str, str]) -> str | None:
    if seen.get("env", "").strip():
        return "bwrap sandbox passes the host environment through"
    return None


#: sock_diag, the kernel's socket listing over netlink (linux/sock_diag.h,
#: linux/unix_diag.h): a dump of every AF_UNIX socket, each with its name.
_NETLINK_SOCK_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLM_F_REQUEST, _NLM_F_DUMP = 0x1, 0x300
_NLMSG_ERROR, _NLMSG_DONE = 2, 3
_UDIAG_SHOW_NAME, _UNIX_DIAG_NAME = 0x1, 0
_NLMSG_HDR = struct.Struct("=IHHII")
_UNIX_DIAG_REQ = struct.Struct("=BBHIIIII")
_UNIX_DIAG_MSG = struct.Struct("=BBBBIII")
_RTATTR = struct.Struct("=HH")


def _unix_sockets() -> list[bytes]:
    """The name of every bound unix socket in this network namespace, byte for byte.

    A path name as it was bound (``b"/run/..."``); an abstract one with its leading NUL
    (``b"\\0/tmp/.X11-unix/X0"``). From the kernel over netlink, not /proc/net/unix:
    that file prints each name raw on a line of its own, so a name with a line break
    in it reads as two sockets, neither of them the real one, and a name that is not
    UTF-8 reads as some other name.

    Raises OSError when the kernel will not list them; an empty list would pass every
    check that reads it.
    """
    request = _UNIX_DIAG_REQ.pack(
        socket.AF_UNIX, 0, 0, 0xFFFFFFFF, 0, _UDIAG_SHOW_NAME, 0xFFFFFFFF, 0xFFFFFFFF
    )
    header = _NLMSG_HDR.pack(
        _NLMSG_HDR.size + len(request),
        _SOCK_DIAG_BY_FAMILY,
        _NLM_F_REQUEST | _NLM_F_DUMP,
        1,
        0,
    )
    names: list[bytes] = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_SOCK_DIAG) as nl:
        nl.settimeout(10)
        nl.sendall(header + request)
        while True:
            data = nl.recv(1 << 20)
            if not data:
                raise OSError("the kernel's socket list ended early")
            for kind, body in _netlink_messages(data):
                if kind == _NLMSG_DONE:
                    return names
                if kind == _NLMSG_ERROR:
                    raise OSError("the kernel would not list its sockets (sock_diag)")
                names += _diag_names(body)


def _netlink_messages(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Each message in one netlink read: its type and its body."""
    offset = 0
    while offset + _NLMSG_HDR.size <= len(data):
        length, kind, _, _, _ = _NLMSG_HDR.unpack_from(data, offset)
        if length < _NLMSG_HDR.size:
            raise OSError("a malformed message in the kernel's socket list")
        yield kind, data[offset + _NLMSG_HDR.size : offset + length]
        offset += (length + 3) & ~3


def _diag_names(body: bytes) -> list[bytes]:
    """The name in one socket's unix_diag_msg, if it has one."""
    offset = _UNIX_DIAG_MSG.size
    while offset + _RTATTR.size <= len(body):
        length, kind = _RTATTR.unpack_from(body, offset)
        if length < _RTATTR.size:
            break
        if kind == _UNIX_DIAG_NAME:
            name = body[offset + _RTATTR.size : offset + length]
            # A path ends at its first NUL (a binder may count the terminator); an
            # abstract name is every byte after its leading one.
            return [name if name[:1] == b"\0" else name.split(b"\0", 1)[0]]
        offset += (length + 3) & ~3
    return []


def _shown(name: bytes) -> str:
    """A socket's name for a message: someone else chose it, escapes and all."""
    return repr(name.replace(b"\0", b"@", 1).decode("utf-8", "backslashreplace"))


def _check_sockets(box: _Box, names: list[bytes]) -> str | None:
    """No socket ``names`` lists can be connected to from inside, but DNS's.

    ``names`` are the sockets of this network namespace, which the sandbox shares, at
    the path each was bound at. Every one of those the sandbox can still reach is
    refused, not a list of known ones, which always lags what is installed. The
    resolver's directory is put back on purpose, for DNS. A socket bound in another
    namespace (a container's), or whose directory has since moved, is not listed at
    its reachable path: docs/agy-sandbox.md says so.
    """
    relative = [n for n in names if n[:1] not in (b"/", b"\0")]
    if relative:
        return f"a socket bound by a relative name ({_shown(relative[0])}) cannot be checked"
    reached = _reachable(box, sorted({n for n in names if n[:1] == b"/"}))
    if isinstance(reached, str):
        return reached
    allowed = _resolver_dir()
    for path in reached:
        # The name as bound, which `..` can carry out of the resolver's directory
        # while it still starts with it.
        inside = Path(os.fsdecode(path))
        if not (
            allowed and ".." not in inside.parts and inside.is_relative_to(allowed)
        ):
            return f"bwrap sandbox can reach {_shown(path)}"
    # Abstract sockets belong to the network namespace, which agy keeps to reach its
    # model, so no mount hides them. A session bus -- dbus-launch's under /tmp, IBus's
    # under ~/.cache -- starts any program it is asked to.
    for name in names:
        if name[:1] == b"\0" and b"/dbus-" in name:
            return f"bwrap sandbox can reach a session bus ({_shown(name)})"
    return None


def _reachable(box: _Box, paths: list[bytes]) -> list[bytes] | str:
    """Which of ``paths`` are sockets the sandbox can write to, or why it is unknown.

    On stdin, one NUL-terminated path each, as bytes: not in the script, which a busy
    host's list would push past the 128 KB limit on one argument, and not decoded,
    which would change a name that is not UTF-8 into another.
    """
    if not paths:
        return []
    # Each batch exits 0 whatever it found, so a status of anything else is the probe
    # failing -- no xargs, a sandbox that did not start -- which must not read as a
    # sandbox that reaches nothing.
    probe = (
        "xargs -0 sh -c 'for s; do"
        ' [ -S "$s" ] && [ -w "$s" ] && printf "%s\\0" "$s";'
        " done; exit 0' sh"
    )
    done = subprocess.run(
        [*box.wrap, "sh", "-c", probe],
        cwd=box.workspace,
        input=b"".join(p + b"\0" for p in paths),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = (said or ["no detail"])[-1][:200]
        return f"could not check the sockets agy's sandbox can reach: {detail}"
    return [p for p in done.stdout.split(b"\0") if p]


_X_DISPLAY = re.compile(rb"\0/tmp/\.X11-unix/X(\d+)")


def _check_x_servers(names: list[bytes]) -> str | None:
    """No X server in ``names`` lets this user in without credentials.

    Its abstract socket is reachable from inside, and one that admits a client with no
    cookie -- stock GNOME and Xwayland grant ``SI:localuser`` -- takes keystrokes into a
    terminal outside the sandbox. Asked from here, which is the same thing: the server
    decides by the connecting uid, and inside it is this one.
    """
    for name in names:
        match = _X_DISPLAY.fullmatch(name)
        if match and _x_admits(name):
            display = f":{match.group(1).decode()}"
            return (
                f"the X server {display} admits this user without a cookie, and a"
                f" program inside could type into a terminal outside; `xhost"
                f" -si:localuser:$USER` (on DISPLAY={display}) closes that"
            )
    return None


def _x_admits(name: bytes) -> bool:
    """Whether the X server at abstract socket ``name`` accepts a client with no auth."""
    # The connection setup: little-endian, protocol 11.0, no authorization name or
    # data. The first byte of the reply is 1 for Success, 0 Failed, 2 Authenticate.
    setup = b"l\0" + struct.pack("<HHHHH", 11, 0, 0, 0, 0)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2)
            conn.connect(name)
            conn.sendall(setup)
            return conn.recv(1) == b"\x01"
    except OSError:
        return False


def _check_reachable(box: _Box, names: list[bytes]) -> str | None:
    """No socket and no X server among ``names`` (``_unix_sockets``) is reachable."""
    return _check_sockets(box, names) or _check_x_servers(names)


def agy_reachable(wrap: list[str], workspace: Path) -> str | None:
    """What the sandbox ``wrap`` can reach right now that it must not, or None.

    The startup check describes the host as it was then. A loop runs for half an hour,
    and a session bus, a socket or an X server that appears later would be reachable
    from every later round, so this part of it runs again before each launch. The list
    is read here: the sandbox shares this network namespace, so it is the same list.
    """
    try:
        return _check_reachable(_Box(wrap, workspace), _unix_sockets())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not check what agy's sandbox can reach: {exc}"


def agy_is_confined() -> tuple[bool, str]:
    """Verify, with no model call, the wrapper a reviewer runs agy under.

    It runs holding no capabilities, agy starts in it, it reads outside the workspace
    but writes only inside, both homes, /tmp and /run hold only what was put there,
    the process namespace is not the host's, the environment is cleared, and nothing
    that runs code outside -- a socket, a session bus, an X server -- is reachable.
    """
    if shutil.which("bwrap") is None:
        return False, (
            "bwrap (bubblewrap) is not installed, and nothing else can make agy"
            " read-only; install it, or set MMCRA_SKIP_AGY_SANDBOX_CHECK=1 on a host"
            " that is already sandboxed"
        )
    parent = _canary_parent()
    if parent is None:
        return False, (
            "agy sandbox check needs a writable /var/tmp for its canary (the sandbox"
            " hides /tmp); make it writable, or set MMCRA_SKIP_AGY_SANDBOX_CHECK=1"
            " on a host that is already sandboxed"
        )
    try:
        with (
            signals_unwind(),
            tempfile.TemporaryDirectory() as ws,
            tempfile.TemporaryDirectory(dir=parent) as out,
            tempfile.TemporaryDirectory() as st,
        ):
            workspace, canary = Path(ws).resolve(), Path(out) / "canary.txt"
            canary.write_bytes(b"original")
            # An empty state: this checks the mounts, which need no sign-in, so there
            # is no reason for a copy of the token to exist while it runs.
            box = _Box(agy_sandbox(workspace, Path(st).resolve()), workspace)
            roots = [*_homes(), Path("/tmp"), Path("/run")]
            seen = _probe(box, canary, roots)
            failure = _check_runs(seen, box)
            if failure:
                return False, failure
            # Read here, not inside: the sandbox shares this network namespace, so it
            # is the same list, and a name in it cannot reach the probe's answers.
            sockets = _unix_sockets()
            failure = (
                _check_capabilities(seen)
                or _check_agy_starts(seen)
                or _check_files(seen, canary, workspace)
                or _check_hidden(seen, box, roots)
                or _check_processes(seen)
                or _check_environment(seen)
                or _check_reachable(box, sockets)
            )
            if failure:
                return False, failure
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"agy sandbox check failed: {exc}"
    return True, "agy sandbox verified (read-only; home, /tmp, /run hidden; no env)"
