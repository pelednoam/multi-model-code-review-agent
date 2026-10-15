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
from typing import Any

from .agy_sandbox import agy_wrapper, scratch_dirs, user_homes
from .config import REPO_ROOT, env_flag
from .proc import run, signals_unwind
from .results import printable
from .unix_sockets import SOCK_DGRAM, Sock, unix_sockets


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
    said = box.sh(
        _probe_script(questions, mark), env={**os.environ, "MMCRA_ENV_PROBE": "x"}
    )
    return _parse_probe(said, mark, questions)


def _probe_script(questions: dict[str, str], mark: str) -> str:
    """One shell script asking every question, each answer opened by ``mark``.

    Each answer is followed by its exit status: an answer the probe never reached,
    or a listing that failed, is not one that found nothing. On a line of its own,
    since an answer need not end in a newline (the canary).
    """
    q = shlex.quote
    return "\n".join(
        f"echo {mark}{key}; {{ {ask}; }} 2>/dev/null;"
        f" status=$?; printf '\\n%s\\n' {q(f'{mark}={key}?')}\"$status\""
        for key, ask in questions.items()
    )


def _parse_probe(said: str, mark: str, questions: dict[str, str]) -> dict[str, str]:
    """The probe's answers by question, each status as ``key?``, and a probe cut
    short marked by ``_incomplete``, naming the answer it stopped at."""
    answers: dict[str, list[str]] = {}
    statuses: dict[str, str] = {}
    for line in said.split("\n"):
        if line.startswith(mark + "="):
            key, _, status = line[len(mark) + 1 :].rpartition("?")
            statuses[f"{key}?"] = status
        elif line.startswith(mark):
            answers[line[len(mark) :]] = []
        elif answers:
            answers[next(reversed(answers))].append(line)
    seen = {key: "\n".join(lines) for key, lines in answers.items()} | statuses
    unfinished = [key for key in questions if f"{key}?" not in statuses]
    if seen and unfinished:
        seen["_incomplete"] = unfinished[0]
    return seen


def _check_runs(seen: dict[str, str], box: _Box) -> str | None:
    # First: a canary that survives because the sandbox never launched is not a pass.
    # Not one answer means the probe never ran, and why is in bwrap's stderr.
    if "_incomplete" in seen:
        return f"the sandbox probe stopped at {seen['_incomplete']!r}"
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
        # A listing that failed lists nothing, which is not the same as empty.
        if seen.get(f"ls {i}?") != "0":
            return f"could not list {root} inside the sandbox"
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


#: An X server's abstract socket, and its display number.
_X_DISPLAY = re.compile(rb"\0/tmp/\.X11-unix/X(\d+)")


def _shown(name: bytes) -> str:
    """A socket's name for a message: someone else chose it, escapes and all."""
    return repr(name.replace(b"\0", b"@", 1).decode("utf-8", "backslashreplace"))


def _check_sockets(box: _Box, socks: list[Sock]) -> str | None:
    """No socket in ``socks`` (``unix_sockets``) can be connected to from inside.

    They are the sockets of this network namespace, which the sandbox shares. X
    servers, of any owner, are asked separately (``_check_x_servers``).
    """
    return (
        _check_relative_sockets(socks)
        or _check_path_sockets(box, socks)
        or _check_abstract_sockets(socks)
    )


def _check_relative_sockets(socks: list[Sock]) -> str | None:
    """A socket bound by a relative name cannot be checked: this user's or root's
    fails. Another user's lives in that user's own directory."""
    for sock in socks:
        if sock.name[:1] not in (b"/", b"\0") and sock.uid in (os.getuid(), 0, None):
            return f"a socket bound by a relative name ({_shown(sock.name)}) cannot be checked"
    return None


def _check_path_sockets(box: _Box, socks: list[Sock]) -> str | None:
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


def _check_abstract_sockets(socks: list[Sock]) -> str | None:
    """No abstract socket of this user's that takes connections.

    No mount hides one: it belongs to the network namespace, which agy keeps to reach
    its model. Whatever listens there admits this user, whatever it is called (a
    session bus can take any name); a client's connected socket has a name too, and
    takes nothing. Where the kernel does not say whose (before 5.3), the listener
    says, asked over a connection (``_listener_uid``).

    Root's and other users' are not counted by default. That is an assumption, not
    proof: they usually check who connects (multipathd, iscsid and a user's session
    bus all do), and counting them would switch the slot off on a stock Ubuntu host
    and let any local user do the same by binding one; but a daemon that admits this
    user over one stays reachable. ``MMCRA_AGY_STRICT_SOCKETS=1`` counts every
    abstract listener but the X servers, which are asked; docs/agy-sandbox.md says so.
    """
    me = os.getuid()
    strict = env_flag("MMCRA_AGY_STRICT_SOCKETS")
    for sock in socks:
        if (
            not sock.accepts
            or sock.name[:1] != b"\0"
            or _X_DISPLAY.fullmatch(sock.name)
        ):
            continue
        owner = sock.uid
        if owner is None:
            if sock.kind == SOCK_DGRAM:
                # A datagram socket has no listener to ask: not knowing is not a pass.
                return f"cannot tell whose datagram socket {_shown(sock.name)} is"
            owner = _listener_uid(sock.name, sock.kind)
        if strict and owner is not None and owner != me:
            return (
                f"bwrap sandbox can reach {_shown(sock.name)}, an abstract socket of"
                f" uid {owner}'s (MMCRA_AGY_STRICT_SOCKETS)"
            )
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
    # Run even with no paths: the wrapper is started either way, so a wrapper that
    # cannot start fails here, at launch, rather than passing for reaching nothing.
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


def _check_x_servers(socks: list[Sock]) -> str | None:
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


def _check_reachable(box: _Box, socks: list[Sock]) -> str | None:
    """No socket and no X server among ``socks`` (``unix_sockets``) is reachable."""
    return _check_sockets(box, socks) or _check_x_servers(socks)


def agy_reachable(wrap: list[str], workspace: Path) -> str | None:
    """What the sandbox ``wrap`` can reach right now that it must not, or None.

    The startup check describes the host as it was then. A loop runs for half an hour,
    and a session bus, a socket or an X server that appears later would be reachable
    from every later round, so this part of it runs again before each launch. The list
    is read here: the sandbox shares this network namespace, so it is the same list.
    """
    try:
        return _check_reachable(_Box(wrap, workspace), unix_sockets())
    except subprocess.TimeoutExpired as exc:
        return f"could not check what agy's sandbox can reach: {_timed_out(exc)}"
    except OSError as exc:
        return f"could not check what agy's sandbox can reach: {exc}"


def _timed_out(exc: subprocess.TimeoutExpired) -> str:
    """A timeout, said without its command: that is the wrapper, and it carries the
    environment agy is given -- a proxy URL with a password in it, say."""
    return f"the sandbox did not answer within {exc.timeout:.0f}s"


def _as_root() -> bool:
    """Whether this process runs as root, whom the sandbox cannot confine."""
    return os.geteuid() == 0


def agy_is_confined(also_hide: tuple[Path, ...] = ()) -> tuple[bool, str]:
    """Verify, with no model call, the wrapper a reviewer runs agy under.

    It runs holding no capabilities, agy starts in it, it reads outside the workspace
    but writes only inside, both homes, /tmp and /run hold only what was put there,
    the process namespace is not the host's, the environment is cleared, and nothing
    that runs code outside -- a socket, a session bus, an X server -- is reachable.
    This checkout, the working directory and ``also_hide`` (the repository under
    review) are hidden, and shown to be.
    """
    if _as_root():
        # Its capabilities dropped, root inside still owns root's files: /etc/shadow,
        # host keys, whatever is mode 600 under /var/lib -- readable, with the network
        # open. No mount hides them all.
        return False, (
            "agy's sandbox cannot keep root's own files from a process running as"
            " root; run as an unprivileged user, or set MMCRA_SKIP_AGY_SANDBOX_CHECK=1"
            " on a disposable host"
        )
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
            scratch_dirs() as (workspace, state),
            tempfile.TemporaryDirectory(dir=parent) as out,
        ):
            canary = Path(out) / "canary.txt"
            canary.write_bytes(b"original")
            # An empty state: this checks the mounts, which need no sign-in, so there
            # is no reason for a copy of the token to exist while it runs.
            # This checkout and the directory it runs from, hidden as a review hides
            # them -- but not one that holds the canary, which must stay readable.
            hidden = [
                path
                for path in dict.fromkeys(
                    p.resolve() for p in (Path.cwd(), REPO_ROOT, *also_hide)
                )
                if path != Path("/") and not canary.is_relative_to(path)
            ]
            box = _Box(agy_wrapper(workspace, state, hidden), workspace)
            roots = [*user_homes(), *hidden, Path("/tmp"), Path("/run")]
            seen = _probe(box, canary, roots)
            failure = _check_runs(seen, box)
            if failure:
                return False, failure
            # Read here, not inside: the sandbox shares this network namespace, so it
            # is the same list, and a name in it cannot reach the probe's answers.
            sockets = unix_sockets()
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
