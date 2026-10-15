"""agy's sandbox, and the check that proves it."""

from __future__ import annotations

import os
import re
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from agy_case import _AgyCase

from scripts.review_loop import agy, agy_sandbox, agy_sandbox_check, backends

# agy_state_dir itself, kept before _AgyCase's autouse fixture replaces it.
from scripts.review_loop.agy_sandbox import agy_state_dir as real_agy_state_dir


class TestAgySandboxCheck(_AgyCase):
    """The startup check that proves the wrapper, with no model call."""

    def test_the_real_sandbox_passes(self) -> None:
        """Not a mock: the wrapper agy runs under, against the real filesystem."""
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert ok, reason

    @pytest.mark.parametrize(
        ("drop", "caught"),
        [
            # Each protection, removed from the real wrapper, must fail the real check.
            (("--unshare-pid",), "host's processes"),
            (("--tmpfs", "HOME"), "HOME/"),
            (("--tmpfs", "/tmp"), "/tmp/"),
            (("--tmpfs", "/run"), "/run/"),
            # bwrap itself, PID 1 inside, readable at /proc/1/environ -- caught though
            # the child's own environment is still cleared.
            (("-i",), "environment"),
        ],
    )
    def test_the_real_check_catches_a_weakened_wrapper(
        self, monkeypatch: pytest.MonkeyPatch, drop: tuple[str, ...], caught: str
    ) -> None:
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        real = agy_sandbox.agy_wrapper
        target = tuple(str(Path.home()) if a == "HOME" else a for a in drop)

        def weakened(ws: Path, st: Path) -> list[str]:
            wrap = real(ws, st)
            for i in range(len(wrap) - len(target) + 1):
                if tuple(wrap[i : i + len(target)]) == target:
                    return wrap[:i] + wrap[i + len(target) :]
            raise AssertionError(f"{target} is not in the wrapper")

        monkeypatch.setattr(agy_sandbox_check, "agy_wrapper", weakened)
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and caught.replace("HOME", str(Path.home())) in reason

    def test_no_place_for_the_canary_is_said_not_misreported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A canary in /tmp is hidden by the sandbox: "could not read" would be a lie.
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy_sandbox_check, "_canary_parent", lambda: None)
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and "/var/tmp" in reason

    def test_a_sandbox_that_lets_writes_through_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy_sandbox_check, "run", self._unconfined)
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and "ALLOWED a write" in reason

    def test_a_sandbox_that_cannot_start_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The codex ambiguity again: an untouched canary is also what nothing looks like."""
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(
            agy_sandbox_check,
            "run",
            lambda *a, **k: SimpleNamespace(
                stdout="",
                stderr="bwrap: setting up uid map: Permission denied",
                returncode=1,
            ),
        )
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and "could not start" in reason and "namespaces" in reason

    def test_an_account_with_no_passwd_entry_disables_only_agy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A container under a bare numeric UID: getpwuid raised out of detection, and
        # no reviewer started at all.
        def unknown(uid: int) -> object:
            raise KeyError(uid)

        monkeypatch.setattr(agy_sandbox.pwd, "getpwuid", unknown)
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and "passwd" in reason
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.setenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", "1")
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        found = backends.detect_backends()
        assert found["agy"] is False and found["claude"] is True

    def test_any_reachable_socket_is_refused_not_only_named_ones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A list of known sockets lags what is installed: Incus's was not on it.
        self._reach(monkeypatch, b"/var/lib/incus/unix.socket")
        reason = agy_sandbox_check._check_reachable(None, self.SOCKETS)  # type: ignore[arg-type]
        assert reason and "/var/lib/incus/unix.socket" in reason

    @pytest.mark.parametrize(
        "name",
        [b"/run/systemd/resolve/io.systemd.Resolve",
         b"/run/systemd/resolve/../../../var/tmp/x.sock"],
        ids=["resolvers-own", "climbing-out"],
    )  # fmt: skip
    def test_no_socket_is_let_through_not_even_the_resolvers(
        self, monkeypatch: pytest.MonkeyPatch, name: bytes
    ) -> None:
        # The resolver's directory was put back whole, and every socket in it -- or
        # bound by a name that only starts there -- was let through. systemd-resolved's
        # varlink socket answers more than DNS.
        self._reach(monkeypatch, name)
        reason = agy_sandbox_check._check_reachable(None, [*self.SOCKETS, name])  # type: ignore[arg-type]
        assert reason and "can reach" in reason

    def test_only_the_resolvers_file_is_put_back(self, tmp_path: Path) -> None:
        resolver = agy_sandbox._resolver_file()
        wrap = agy_sandbox.agy_wrapper(tmp_path / "ws", tmp_path / "st")
        if resolver is not None:
            assert wrap[wrap.index(str(resolver)) - 1] == "--ro-bind"
            assert str(resolver.parent) not in wrap

    def test_any_abstract_session_bus_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not only dbus-launch's under /tmp: IBus keeps one under ~/.cache.
        self._reach(monkeypatch)
        names = [*self.SOCKETS, b"\0/home/u/.cache/ibus/dbus-AbCd"]
        reason = agy_sandbox_check._check_reachable(None, names)  # type: ignore[arg-type]
        assert reason and "session bus" in reason and "@/home/u" in reason

    @pytest.mark.parametrize(
        "name",
        [b"/var/tmp/my app.sock", b"/var/tmp/a\nb.sock", b"/var/tmp/\xff.sock"],
        ids=["space", "line-break", "not-utf8"],
    )
    def test_a_socket_name_is_checked_byte_for_byte(
        self, monkeypatch: pytest.MonkeyPatch, name: bytes
    ) -> None:
        # Read from /proc/net/unix, a space split the name, a line break cut it in two,
        # and a byte that is not UTF-8 became another character: each a different path
        # from the socket that existed.
        self._reach(monkeypatch, name)
        reason = agy_sandbox_check._check_reachable(None, [*self.SOCKETS, name])  # type: ignore[arg-type]
        assert reason and "can reach" in reason
        # Named in the message escaped, not raw: someone else chose it.
        assert "\n" not in reason and "\xff" not in reason

    def test_the_kernel_lists_this_hosts_sockets(self) -> None:
        # Byte for byte: a line break in a name stays in it.
        if sys.platform != "linux":
            pytest.skip("sock_diag is Linux's")
        name = b"\0mmcra-test-\n-" + str(os.getpid()).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as mine:
            mine.bind(name)
            assert name in agy_sandbox_check._unix_sockets()

    @staticmethod
    def _diag(name: bytes, uid: int | None, flags: int = 0) -> bytes:
        """One sock_diag message for a socket called ``name``, owned by ``uid``."""
        attrs = b""
        for kind, value in (
            (0, name),
            (7, None if uid is None else struct.pack("=I", uid)),
        ):
            if value is None:
                continue
            attr = struct.pack("=HH", 4 + len(value), kind) + value
            attrs += attr + b"\0" * (-len(attr) % 4)
        body = struct.pack("=BBBBIII", 1, 1, 1, 0, 1, 0, 0) + attrs
        return struct.pack("=IHHII", 16 + len(body), 20, flags, 1, 0) + body

    @staticmethod
    def _done(status: int = 0) -> bytes:
        return struct.pack("=IHHII", 20, 3, 0, 1, 0) + struct.pack("=i", status)

    def _kernel_says(self, monkeypatch: pytest.MonkeyPatch, *reads: bytes) -> None:
        """Make the netlink socket return ``reads``, one per recv."""
        pending = list(reads)

        class Netlink:
            def __enter__(self) -> "Netlink":
                return self

            def __exit__(self, *exc: object) -> None:
                pass

            def settimeout(self, t: float) -> None:
                pass

            def sendall(self, data: bytes) -> None:
                pass

            def recv(self, n: int) -> bytes:
                return pending.pop(0) if pending else b""

        monkeypatch.setattr(agy_sandbox_check.socket, "socket", lambda *a: Netlink())

    def test_another_users_abstract_socket_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Any local user could switch the slot off by binding a bus-like name; their
        # bus admits them only. A path socket counts whoever owns it: its file's
        # permissions decide, and the probe asks.
        me, other = os.getuid(), os.getuid() + 12345
        self._kernel_says(
            monkeypatch,
            self._diag(b"\0/tmp/dbus-theirs", other)
            + self._diag(b"\0/tmp/dbus-mine", me)
            + self._diag(b"\0/tmp/dbus-unknown", None)
            + self._diag(b"relative.sock", other)
            + self._diag(b"/var/tmp/theirs.sock", other)
            + self._done(),
        )
        assert agy_sandbox_check._unix_sockets() == [
            b"\0/tmp/dbus-mine",
            b"\0/tmp/dbus-unknown",
            b"/var/tmp/theirs.sock",
        ]

    @pytest.mark.parametrize(
        "case", ["interrupted", "failed", "truncated", "attribute", "ended"]
    )
    def test_a_socket_list_cut_short_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch, case: str
    ) -> None:
        # Each was taken as the whole list: a socket missing from it was never probed.
        mine = self._diag(b"/var/tmp/a.sock", os.getuid())
        # An attribute claiming more bytes than its message holds.
        body = struct.pack("=BBBBIII", 1, 1, 1, 0, 1, 0, 0) + struct.pack("=HH", 200, 0)
        overlong = struct.pack("=IHHII", 16 + len(body), 20, 0, 1, 0) + body
        reads = {
            "interrupted": self._diag(b"/var/tmp/a.sock", 0, flags=0x10) + self._done(),
            "failed": mine + self._done(-4),
            "truncated": mine[:-6],
            "attribute": overlong + self._done(),
            "ended": mine,
        }[case]
        self._kernel_says(monkeypatch, reads)
        with pytest.raises(OSError):
            agy_sandbox_check._unix_sockets()

    def test_a_socket_list_the_kernel_will_not_give_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An empty list passed every socket check there was.
        def refused() -> list[bytes]:
            raise OSError("the kernel would not list its sockets (sock_diag)")

        monkeypatch.setattr(agy_sandbox_check, "_unix_sockets", refused)
        reason = agy_sandbox_check.agy_reachable(["true"], tmp_path)
        assert reason and "sock_diag" in reason

    def test_an_x_server_that_does_not_answer_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A timeout read as "does not admit you", and the check passed.
        name = f"\0/tmp/.X11-unix/X{os.getpid() % 10000 + 40000}".encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(name)
            server.listen(1)  # accepts the connection, never replies
            self._reach(monkeypatch)
            with pytest.raises(OSError, match="did not answer"):
                agy_sandbox_check._check_reachable(None, [name])  # type: ignore[arg-type]

    def test_a_timeout_does_not_print_the_wrapper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The wrapper carries agy's environment; TimeoutExpired's message is the whole
        # command, and a proxy URL in it can hold a password.
        wrap = ["env", "-i", "HTTPS_PROXY=http://user:hunter2@proxy:3128", "bwrap"]

        def slow(box: object, paths: list[bytes]) -> list[bytes]:
            raise subprocess.TimeoutExpired([*wrap, "sh"], 30)

        monkeypatch.setattr(agy_sandbox_check, "_reachable", slow)
        monkeypatch.setattr(agy_sandbox_check, "_unix_sockets", lambda: [b"/x.sock"])
        reason = agy_sandbox_check.agy_reachable(wrap, tmp_path)
        assert reason and "30s" in reason and "hunter2" not in reason

    def test_a_socket_probe_that_failed_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No xargs inside, or a sandbox that did not start: nothing printed, and
        # nothing printed read as nothing reachable.
        failed = "could not check the sockets agy's sandbox can reach: xargs: not found"
        self._reach(monkeypatch, failed)
        reason = agy_sandbox_check._check_reachable(None, self.SOCKETS)  # type: ignore[arg-type]
        assert reason == failed

    def test_the_socket_probe_fails_closed_for_real(self, tmp_path: Path) -> None:
        # The probe's own status, through a "sandbox" that is a failing command.
        box = agy_sandbox_check._Box(
            ["sh", "-c", "echo 'xargs: not found' >&2; exit 127", "--"], tmp_path
        )
        reason = agy_sandbox_check._reachable(box, [b"/var/tmp/x.sock"])
        assert isinstance(reason, str) and "xargs: not found" in reason

    def test_the_probes_marks_cannot_be_forged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A fixed mark: a name in a listing the sandbox failed to hide, holding a line
        # break and the mark, opened an answer of its own and rewrote the ones after.
        scripts: list[str] = []

        class Box:
            workspace = tmp_path

            def sh(self, script: str, **kw: object) -> str:
                scripts.append(script)
                return ""

        for _ in range(2):
            agy_sandbox_check._probe(Box(), tmp_path / "canary", [])  # type: ignore[arg-type]
        marks = [re.search(r"echo (@@\S+@@)agy", s).group(1) for s in scripts]  # type: ignore[union-attr]
        assert marks[0] != marks[1]

    def test_a_socket_with_a_relative_name_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._reach(monkeypatch)
        reason = agy_sandbox_check._check_reachable(
            None, [*self.SOCKETS, b"relative.sock"]
        )  # type: ignore[arg-type]
        assert reason and "relative" in reason

    @pytest.mark.parametrize(
        "caps",
        ["CapEff:\t000001ffffffffff", "", "CapEff:"],
        ids=["root", "none", "empty"],
    )
    def test_a_sandbox_holding_capabilities_is_not_a_pass(self, caps: str) -> None:
        # Run as root without a user namespace, bwrap kept every capability, and a
        # remount would have undone the read-only binds.
        assert agy_sandbox_check._check_capabilities({"caps": caps})
        assert (
            agy_sandbox_check._check_capabilities({"caps": "CapEff:\t0000000000000000"})
            is None
        )

    def test_the_sandbox_has_a_user_namespace_and_no_capabilities(self) -> None:
        wrap = agy_sandbox.agy_wrapper(Path("/var/tmp/ws"), Path("/var/tmp/st"))
        assert "--unshare-user" in wrap
        assert wrap[wrap.index("--cap-drop") + 1] == "ALL"

    @pytest.mark.parametrize(("reply", "refused"), [(b"\x01", True), (b"\x02", False)])
    def test_an_x_server_that_admits_this_user_without_a_cookie_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, reply: bytes, refused: bool
    ) -> None:
        # Only named before; stock GNOME and Xwayland admit the local user, and a
        # program inside could type into a terminal outside.
        name = f"\0/tmp/.X11-unix/X{os.getpid() % 10000 + 50000}".encode()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(name)
        server.listen(1)

        def answer() -> None:
            conn, _ = server.accept()
            with conn:
                conn.recv(12)
                conn.sendall(reply)

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        self._reach(monkeypatch)
        try:
            reason = agy_sandbox_check._check_reachable(None, [name])  # type: ignore[arg-type]
        finally:
            thread.join(timeout=5)
            server.close()
        assert bool(reason and "X server" in reason) is refused

    def test_no_home_at_all_disables_only_agy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # HOME unset and no passwd entry: Path.home() raised RuntimeError, which no
        # caller caught, and detection stopped with no reviewer started.
        def nowhere() -> Path:
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(agy.Path, "home", staticmethod(nowhere))
        monkeypatch.setattr(agy_sandbox, "agy_state_dir", real_agy_state_dir)
        with pytest.raises(OSError, match="home directory"):
            agy_sandbox.agy_state_dir()
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and "home directory" in reason

    def test_a_symlinked_home_puts_the_state_where_the_home_is_hidden(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The copy was mounted at the link's path, outside the home hidden at its
        # target, and the check refused the wrapper's own mount.
        real = tmp_path / "real-home"
        real.mkdir()
        (tmp_path / "link").symlink_to(real)
        monkeypatch.setenv("HOME", str(tmp_path / "link"))
        monkeypatch.setattr(agy_sandbox, "agy_state_dir", real_agy_state_dir)
        assert agy_sandbox.agy_state_dir().is_relative_to(real.resolve())

    def test_a_home_of_root_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Dropped from the list, / was neither hidden nor checked: /.ssh stayed readable.
        monkeypatch.setenv("HOME", "/")
        with pytest.raises(OSError, match="home directory of /"):
            agy_sandbox.user_homes()
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and "home directory of /" in reason

    def test_a_socket_under_a_root_only_directory_needs_no_mask(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Path.exists raised PermissionError there, and the slot was lost over it.
        def denied(self: Path, strict: bool = False) -> Path:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(agy.os.path, "lexists", lambda p: True)
        monkeypatch.setattr(Path, "resolve", denied)
        assert agy_sandbox._maskable("/var/lib/incus/unix.socket") is False

    def test_no_bwrap_is_not_a_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(agy.shutil, "which", lambda c: None)
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and "bwrap" in reason

    @pytest.mark.parametrize(
        "failure", [OSError("exec failed"), subprocess.TimeoutExpired(["bwrap"], 30)]
    )
    def test_a_check_that_crashes_disables_rather_than_aborts(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        # Raised out of detect_backends, this stopped every other reviewer too.
        def boom(*a: object, **k: object) -> None:
            raise failure

        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy_sandbox_check, "run", boom)
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and "check failed" in reason

    def test_the_check_proves_the_wrapper_a_reviewer_runs_under(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def record(cmd: list[str], **kw: object) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(stdout="", stderr="", returncode=1)

        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy_sandbox_check, "run", record)
        agy_sandbox_check.agy_is_confined()
        (first,) = calls  # it stops at "could not start"
        wrap = first[: first.index("--") + 1]
        state = Path(wrap[wrap.index(str(agy_sandbox.agy_state_dir())) - 1])
        last_bind = len(wrap) - 1 - wrap[::-1].index("--bind")
        workspace = Path(wrap[last_bind + 1])
        assert wrap == agy_sandbox.agy_wrapper(workspace, state)
