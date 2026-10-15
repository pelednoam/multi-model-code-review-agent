"""The proof of agy's sandbox (``agy_sandbox``), with no model call.

``agy_is_confined`` proves the wrapper before the slot is used, and ``agy_reachable``
checks again before each launch what it can reach.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from .agy_sandbox import agy_wrapper, user_homes
from .proc import run, signals_unwind
from .results import printable

if TYPE_CHECKING:
    from collections.abc import Iterator


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
        # Mount destinations only -- an environment value such as a PATH under HOME is
        # not a mount. Paths, not strings: HOME=/home/user/ is the same directory.
        # --tmpfs DEST, and --bind/--ro-bind SRC DEST: the only mounts it makes.
        self.mounts = [
            Path(wrap[i + (1 if arg == "--tmpfs" else 2)])
            for i, arg in enumerate(wrap)
            if arg == "--tmpfs" or arg.endswith("-bind")
        ]

    def sh(self, script: str, **kw: Any) -> str:
        """What ``script`` printed inside the sandbox."""
        # stdin: what the caller hands over, else nothing -- not the operator's
        # terminal, which the sandboxed shell would otherwise inherit.
        if "input" not in kw:
            kw["stdin"] = subprocess.DEVNULL
        done = run(
            [*self.wrap, "sh", "-c", script], cwd=self.workspace, timeout=30, **kw
        )
        self.stderr = done.stderr or ""
        return done.stdout or ""

    def mounted_under(self, root: Path) -> set[str]:
        """The names directly under ``root`` that the wrapper mounted there."""
        return {
            d.relative_to(root).parts[0]
            for d in self.mounts
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
        "userns": "unshare -U true >/dev/null 2>&1 && echo USERNS",
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
    # What the sandbox printed, escapes and all, made safe to print.
    detail = printable((box.stderr.strip().splitlines() or [""])[-1][:200])
    hint = ""
    if "uid map" in detail or "userns" in detail or "namespace" in detail:
        hint = (
            " -- looks like the kernel is blocking unprivileged user namespaces;"
            " see docs/agy-sandbox.md"
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
            # repr: a file name the sandbox failed to hide, chosen by whoever made it.
            return f"bwrap sandbox shows {root}/{sorted(extra)[0]!r}"
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


def _check_userns(seen: dict[str, str], box: _Box) -> str | None:
    # Where the wrapper asks bwrap to forbid them, a new user namespace inside must
    # fail: in one, a process maps itself root and can mount over the read-only binds.
    if "--disable-userns" in box.wrap and "USERNS" in seen.get("userns", ""):
        return "bwrap sandbox lets agy create a user namespace of its own"
    return None


def _check_environment(seen: dict[str, str]) -> str | None:
    if seen.get("env", "").strip():
        return "bwrap sandbox passes the host environment through"
    return None


#: sock_diag, the kernel's socket listing over netlink (linux/sock_diag.h,
#: linux/unix_diag.h): a dump of every AF_UNIX socket, each with its name and owner.
_NETLINK_SOCK_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLM_F_REQUEST, _NLM_F_DUMP, _NLM_F_DUMP_INTR = 0x1, 0x300, 0x10
_NLMSG_ERROR, _NLMSG_DONE = 2, 3
_UDIAG_SHOW_NAME, _UDIAG_SHOW_UID = 0x01, 0x40
_UNIX_DIAG_NAME, _UNIX_DIAG_UID = 0, 7
_NLMSG_HDR = struct.Struct("=IHHII")
_UNIX_DIAG_REQ = struct.Struct("=BBHIIIII")
_UNIX_DIAG_MSG = struct.Struct("=BBBBIII")
_RTATTR = struct.Struct("=HH")
#: unix_diag_msg's type and state that take a connection: a stream or seqpacket socket
#: in TCP_LISTEN, or any datagram one.
_SOCK_DGRAM, _TCP_LISTEN = 2, 10

#: An X server's abstract socket, and its display number.
_X_DISPLAY = re.compile(rb"\0/tmp/\.X11-unix/X(\d+)")


class _Sock(NamedTuple):
    """A bound unix socket: its name, and its owner (None where the kernel, before
    5.3, does not say)."""

    #: A path as it was bound (``b"/run/..."``), or an abstract name with its leading
    #: NUL (``b"\\0/tmp/.X11-unix/X0"``).
    name: bytes
    uid: int | None = None
    #: Whether it takes connections or datagrams from others: a listening stream
    #: socket, or a datagram one. A client's connected socket has a name too.
    accepts: bool = True
    #: SOCK_STREAM, SOCK_DGRAM or SOCK_SEQPACKET.
    kind: int = socket.SOCK_STREAM


def _unix_sockets() -> list[_Sock]:
    """Every bound unix socket in this network namespace, byte for byte, and its owner.

    From the kernel over netlink, not /proc/net/unix: that file prints each name raw on
    a line of its own, so a name with a line break in it reads as two sockets, neither
    of them the real one, and a name that is not UTF-8 reads as some other name.

    Raises OSError when the kernel will not list them, or the list comes back
    interrupted or malformed; a short list would pass every check that reads it.
    """
    request = _UNIX_DIAG_REQ.pack(
        socket.AF_UNIX,
        0,
        0,
        0xFFFFFFFF,
        0,
        _UDIAG_SHOW_NAME | _UDIAG_SHOW_UID,
        0xFFFFFFFF,
        0xFFFFFFFF,
    )
    header = _NLMSG_HDR.pack(
        _NLMSG_HDR.size + len(request),
        _SOCK_DIAG_BY_FAMILY,
        _NLM_F_REQUEST | _NLM_F_DUMP,
        1,
        0,
    )
    found: list[_Sock] = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_SOCK_DIAG) as nl:
        nl.settimeout(10)
        nl.sendall(header + request)
        while True:
            data = nl.recv(1 << 20)
            if not data:
                raise OSError("the kernel's socket list ended early")
            for kind, flags, body in _netlink_messages(data):
                if flags & _NLM_F_DUMP_INTR:
                    # The table changed while it was listed: a socket may be missing.
                    raise OSError("the kernel's socket list was interrupted")
                if kind == _NLMSG_DONE:
                    _check_done(body)
                    return found
                if kind == _NLMSG_ERROR:
                    raise OSError("the kernel would not list its sockets (sock_diag)")
                sock = _diag_socket(body)
                if sock.name:
                    found.append(sock)


def _netlink_messages(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    """Each message in one netlink read: its type, its flags and its body."""
    offset = 0
    while offset < len(data):
        if offset + _NLMSG_HDR.size > len(data):
            raise OSError("a truncated message in the kernel's socket list")
        length, kind, flags, _, _ = _NLMSG_HDR.unpack_from(data, offset)
        if length < _NLMSG_HDR.size or offset + length > len(data):
            raise OSError("a malformed message in the kernel's socket list")
        yield kind, flags, data[offset + _NLMSG_HDR.size : offset + length]
        offset += (length + 3) & ~3


def _check_done(body: bytes) -> None:
    """NLMSG_DONE carries the dump's status: anything but 0 is a list cut short."""
    if len(body) < 4 or struct.unpack_from("=i", body)[0] != 0:
        raise OSError("the kernel's socket list did not complete")


def _diag_socket(body: bytes) -> _Sock:
    """One socket's unix_diag_msg: its name (empty if it has none), owner, and
    whether it accepts anything from others."""
    if len(body) < _UNIX_DIAG_MSG.size:
        raise OSError("a truncated socket in the kernel's socket list")
    _, sock_type, state, _, _, _, _ = _UNIX_DIAG_MSG.unpack_from(body)
    accepts = sock_type == _SOCK_DGRAM or state == _TCP_LISTEN
    name, uid = b"", None
    offset = _UNIX_DIAG_MSG.size
    while offset < len(body):
        if offset + _RTATTR.size > len(body):
            raise OSError("a truncated attribute in the kernel's socket list")
        length, attr_type = _RTATTR.unpack_from(body, offset)
        if length < _RTATTR.size or offset + length > len(body):
            raise OSError("a malformed attribute in the kernel's socket list")
        value = body[offset + _RTATTR.size : offset + length]
        if attr_type == _UNIX_DIAG_NAME:
            # A path ends at its first NUL (a binder may count the terminator); an
            # abstract name is every byte after its leading one.
            name = value if value[:1] == b"\0" else value.split(b"\0", 1)[0]
        elif attr_type == _UNIX_DIAG_UID and len(value) >= 4:
            uid = struct.unpack_from("=I", value)[0]
        offset += (length + 3) & ~3
    return _Sock(name, uid, accepts, sock_type)


def _shown(name: bytes) -> str:
    """A socket's name for a message: someone else chose it, escapes and all."""
    return repr(name.replace(b"\0", b"@", 1).decode("utf-8", "backslashreplace"))


def _check_sockets(box: _Box, socks: list[_Sock]) -> str | None:
    """No socket in ``socks`` (``_unix_sockets``) can be connected to from inside.

    They are the sockets of this network namespace, which the sandbox shares. X
    servers, of any owner, are asked separately (``_check_x_servers``).
    """
    return (
        _check_relative_sockets(socks)
        or _check_path_sockets(box, socks)
        or _check_abstract_sockets(socks)
    )


def _check_relative_sockets(socks: list[_Sock]) -> str | None:
    """A socket bound by a relative name cannot be checked: this user's or root's
    fails. Another user's lives in that user's own directory."""
    for sock in socks:
        if sock.name[:1] not in (b"/", b"\0") and sock.uid in (os.getuid(), 0, None):
            return f"a socket bound by a relative name ({_shown(sock.name)}) cannot be checked"
    return None


def _check_path_sockets(box: _Box, socks: list[_Sock]) -> str | None:
    """No path socket, whoever owns it, that the sandbox can write to where it was
    bound.

    Refused, not looked up in a list of known ones, which always lags what is
    installed; none is let through. A socket bound in another namespace (a
    container's), or whose directory has since moved, is not found at the path that
    reaches it: docs/agy-sandbox.md says so.
    """
    paths = sorted({sock.name for sock in socks if sock.name[:1] == b"/"})
    reached = _reachable(box, paths)
    if isinstance(reached, str):
        return reached
    if reached:
        return f"bwrap sandbox can reach {_shown(reached[0])}"
    return None


def _check_abstract_sockets(socks: list[_Sock]) -> str | None:
    """No abstract socket of this user's that takes connections.

    No mount hides one: it belongs to the network namespace, which agy keeps to reach
    its model. Whatever listens there admits this user, whatever it is called (a
    session bus can take any name); a client's connected socket has a name too, and
    takes nothing. Where the kernel does not say whose (before 5.3), the listener
    says, asked over a connection (``_listener_uid``). Root's system daemons check
    who connects, and another user's admit that user only: counting those would let
    any local user switch the slot off by binding one.
    """
    me = os.getuid()
    for sock in socks:
        if (
            not sock.accepts
            or sock.name[:1] != b"\0"
            or _X_DISPLAY.fullmatch(sock.name)
        ):
            continue
        owner = sock.uid
        if owner is None:
            if sock.kind == _SOCK_DGRAM:
                # A datagram socket has no listener to ask: not knowing is not a pass.
                return f"cannot tell whose datagram socket {_shown(sock.name)} is"
            owner = _listener_uid(sock.name, sock.kind)
        if owner == me:
            return (
                f"bwrap sandbox can reach {_shown(sock.name)}, an abstract socket of"
                " this user's, and whatever listens there admits this user"
            )
    return None


def _listener_uid(name: bytes, kind: int = socket.SOCK_STREAM) -> int | None:
    """Whose process listens at abstract socket ``name``, from the connection's peer
    credentials -- or None if nothing listens there any more, and so admits no one.

    Connected with the socket's own type: a seqpacket listener refuses a stream
    connection, which read as nothing listening. Anything else that keeps the answer
    from coming -- a timeout, say -- raises OSError: not knowing is not a pass.
    """
    try:
        with socket.socket(socket.AF_UNIX, kind) as conn:
            conn.settimeout(2)
            conn.connect(name)
            creds = conn.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
    except (ConnectionRefusedError, FileNotFoundError):
        return None
    except TimeoutError as exc:
        raise OSError(f"{_shown(name)} did not answer") from exc
    _, uid, _ = struct.unpack("3i", creds)
    return uid


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
    # subprocess.run, not run(): stdin and stdout are bytes, since a path need not be
    # UTF-8.
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
        detail = printable((said or ["no detail"])[-1][:200])
        return f"could not check the sockets agy's sandbox can reach: {detail}"
    return [p for p in done.stdout.split(b"\0") if p]


def _check_x_servers(socks: list[_Sock]) -> str | None:
    """No X server in ``socks`` lets this user in without credentials, whoever owns it.

    Its abstract socket is reachable from inside, and one that admits a client with no
    cookie -- stock GNOME and Xwayland grant ``SI:localuser`` -- takes keystrokes into a
    terminal outside the sandbox. Asked from here, which is the same thing: the server
    decides by the connecting uid, and inside it is this one.
    """
    for name in (sock.name for sock in socks):
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
    """Whether the X server at abstract socket ``name`` accepts a client with no auth.

    No server there is a no. A server that does not answer in time, or fails in some
    other way, raises OSError: not knowing is not a pass.
    """
    # The connection setup: little-endian, protocol 11.0, no authorization name or
    # data. The first byte of the reply is 1 for Success, 0 Failed, 2 Authenticate.
    setup = b"l\0" + struct.pack("<HHHHH", 11, 0, 0, 0, 0)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2)
            conn.connect(name)
            conn.sendall(setup)
            reply = conn.recv(1)
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except TimeoutError as exc:
        raise OSError(f"the X server {_shown(name)} did not answer") from exc
    # Closed without a word: it did not admit this client.
    return reply == b"\x01"


def _check_reachable(box: _Box, socks: list[_Sock]) -> str | None:
    """No socket and no X server among ``socks`` (``_unix_sockets``) is reachable."""
    return _check_sockets(box, socks) or _check_x_servers(socks)


def agy_reachable(wrap: list[str], workspace: Path) -> str | None:
    """What the sandbox ``wrap`` can reach right now that it must not, or None.

    The startup check describes the host as it was then. A loop runs for half an hour,
    and a session bus, a socket or an X server that appears later would be reachable
    from every later round, so this part of it runs again before each launch. The list
    is read here: the sandbox shares this network namespace, so it is the same list.
    """
    try:
        return _check_reachable(_Box(wrap, workspace), _unix_sockets())
    except subprocess.TimeoutExpired as exc:
        return f"could not check what agy's sandbox can reach: {_timed_out(exc)}"
    except OSError as exc:
        return f"could not check what agy's sandbox can reach: {exc}"


def _timed_out(exc: subprocess.TimeoutExpired) -> str:
    """A timeout, said without its command: that is the wrapper, and it carries the
    environment agy is given -- a proxy URL with a password in it, say."""
    return f"the sandbox did not answer within {exc.timeout:.0f}s"


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
            box = _Box(agy_wrapper(workspace, Path(st).resolve()), workspace)
            roots = [*user_homes(), Path("/tmp"), Path("/run")]
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
                or _check_userns(seen, box)
                or _check_reachable(box, sockets)
            )
            if failure:
                return False, failure
    except subprocess.TimeoutExpired as exc:
        return False, f"agy sandbox check failed: {_timed_out(exc)}"
    except OSError as exc:
        return False, f"agy sandbox check failed: {exc}"
    return True, "agy sandbox verified (read-only; home, /tmp, /run hidden; no env)"
