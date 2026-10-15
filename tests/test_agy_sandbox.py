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
from agy_case import AgyCase
from helpers import REPO_ROOT

from scripts.review_loop import agy, agy_sandbox, agy_sandbox_check, backends

# agy_state_dir itself, kept before AgyCase's autouse fixture replaces it.
from scripts.review_loop.agy_sandbox import agy_state_dir as real_agy_state_dir


#: Abstract unix sockets, SO_PEERCRED and sock_diag are Linux's.
LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="needs Linux sockets")


class TestAgySandboxCheck(AgyCase):
    """The startup check that proves the wrapper, with no model call."""

    def test_the_real_sandbox_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not a mock: the wrapper agy runs under, against the real filesystem."""
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        self._quiet_host(monkeypatch)
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert ok, reason

    def test_this_hosts_sockets_are_checked_without_error(self, tmp_path: Path) -> None:
        # Whatever the verdict on this host, the real list is read and judged.
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        (tmp_path / "st").mkdir()
        wrap = agy_sandbox.agy_wrapper(tmp_path, tmp_path / "st")
        reason = agy_sandbox_check.agy_reachable(wrap, tmp_path)
        assert reason is None or "could not check" not in reason, reason

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

        def weakened(ws: Path, st: Path, hidden: object = ()) -> list[str]:
            wrap = real(ws, st, hidden)  # type: ignore[arg-type]
            for i in range(len(wrap) - len(target) + 1):
                if tuple(wrap[i : i + len(target)]) == target:
                    return wrap[:i] + wrap[i + len(target) :]
            raise AssertionError(f"{target} is not in the wrapper")

        monkeypatch.setattr(agy_sandbox_check, "agy_wrapper", weakened)
        self._quiet_host(monkeypatch)
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
        reason = agy_sandbox_check._check_reachable(
            None, [*self.SOCKETS, self._mine(name)]
        )  # type: ignore[arg-type]
        assert reason and "can reach" in reason

    def test_only_the_resolvers_file_is_put_back(self, tmp_path: Path) -> None:
        resolver = agy_sandbox._resolver_file()
        wrap = agy_sandbox.agy_wrapper(tmp_path / "ws", tmp_path / "st")
        if resolver is not None:
            assert wrap[wrap.index(str(resolver)) - 1] == "--ro-bind"
            assert str(resolver.parent) not in wrap

    @pytest.mark.parametrize(
        "name",
        [b"\0/home/u/.cache/ibus/dbus-AbCd", b"\0/tmp/my-own-bus", b"\0ISCSI-like"],
        ids=["ibus", "custom-address", "any-name"],
    )
    def test_any_abstract_socket_of_this_users_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, name: bytes
    ) -> None:
        # Not only dbus-launch's under /tmp: IBus keeps one under ~/.cache, and a
        # session bus can listen at an address of any name -- which the old "/dbus-"
        # test let through.
        self._reach(monkeypatch)
        socks = [*self.SOCKETS, self._mine(name)]
        reason = agy_sandbox_check._check_reachable(None, socks)  # type: ignore[arg-type]
        assert reason and "abstract socket of this user's" in reason

    def test_abstract_sockets_that_admit_only_others_are_not_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Root's daemons and another user's sockets check who connects; a client's
        # connected socket of this user's takes no connections at all. Counting any of
        # them switched the slot off on an ordinary host.
        # As user 1000, whoever runs the tests: as root, root's sockets are this user's.
        monkeypatch.setattr(agy_sandbox_check.os, "getuid", lambda: 1000)
        Sock = agy_sandbox_check.Sock
        self._reach(monkeypatch)
        socks = [
            Sock(b"\0/org/kernel/linux/storage/multipathd", 0),
            Sock(b"\0/tmp/dbus-theirs", 2000),
            Sock(b"\0c99a1981b9af149d/bus/systemd/bus-system", 1000, accepts=False),
            Sock(b"theirs-relative.sock", 2000),
        ]
        assert agy_sandbox_check._check_reachable(None, socks) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("listener", "refused"), [(1000, True), (0, False), (None, False)]
    )
    def test_where_the_kernel_does_not_say_whose_the_listener_is_asked(
        self, monkeypatch: pytest.MonkeyPatch, listener: int | None, refused: bool
    ) -> None:
        # Before 5.3 the owner is unknown: only a bus-like name counted, and any other
        # of this user's listening sockets passed.
        monkeypatch.setattr(agy_sandbox_check.os, "getuid", lambda: 1000)
        monkeypatch.setattr(
            agy_sandbox_check, "_listener_uid", lambda name, kind: listener
        )
        self._reach(monkeypatch)
        unknown = [agy_sandbox_check.Sock(b"\0some-daemon", None)]
        reason = agy_sandbox_check._check_reachable(None, unknown)  # type: ignore[arg-type]
        assert bool(reason) is refused

    def test_an_owner_that_cannot_be_learnt_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A datagram socket has no listener to ask, and a listener that does not answer
        # is not one that admits no one: both passed, as "nothing listening".
        monkeypatch.setattr(agy_sandbox_check.os, "getuid", lambda: 1000)
        self._reach(monkeypatch)
        dgram = agy_sandbox_check.Sock(b"\0notify", None, True, socket.SOCK_DGRAM)
        reason = agy_sandbox_check._check_reachable(None, [dgram])  # type: ignore[arg-type]
        assert reason and "cannot tell whose" in reason

        def silent(name: bytes, kind: int) -> int:
            raise OSError("'@slow' did not answer")

        monkeypatch.setattr(agy_sandbox_check, "_listener_uid", silent)
        slow = [agy_sandbox_check.Sock(b"\0slow", None)]
        with pytest.raises(OSError, match="did not answer"):
            agy_sandbox_check._check_reachable(None, slow)  # type: ignore[arg-type]

    @LINUX_ONLY
    def test_a_seqpacket_listener_is_asked_as_one(self) -> None:
        # Asked with a stream connection, it refused, and read as nothing listening.
        name = f"\0mmcra-seq-{os.getpid()}".encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as mine:
            mine.bind(name)
            mine.listen(1)
            uid = agy_sandbox_check._listener_uid(name, socket.SOCK_SEQPACKET)
            assert uid == os.getuid()

    def test_run_as_root_roots_sockets_are_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agy_sandbox_check.os, "getuid", lambda: 0)
        self._reach(monkeypatch)
        socks = [agy_sandbox_check.Sock(b"\0/org/kernel/linux/storage/multipathd", 0)]
        assert agy_sandbox_check._check_reachable(None, socks)  # type: ignore[arg-type]

    @LINUX_ONLY
    def test_the_listener_says_whose_it_is(self) -> None:
        name = f"\0mmcra-listener-{os.getpid()}".encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as mine:
            mine.bind(name)
            mine.listen(1)
            assert agy_sandbox_check._listener_uid(name) == os.getuid()
        assert agy_sandbox_check._listener_uid(name) is None

    def test_another_users_x_server_is_still_asked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Owner filtering skipped other users' X servers, one of which may admit this
        # user (xhost +).
        asked: list[bytes] = []

        def admits(name: bytes) -> bool:
            asked.append(name)
            return True

        monkeypatch.setattr(agy_sandbox_check, "_x_admits", admits)
        self._reach(monkeypatch)
        theirs = agy_sandbox_check.Sock(b"\0/tmp/.X11-unix/X7", os.getuid() + 12345)
        reason = agy_sandbox_check._check_reachable(None, [theirs])  # type: ignore[arg-type]
        assert asked == [theirs.name] and reason and "X server :7" in reason

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
        reason = agy_sandbox_check._check_reachable(
            None, [*self.SOCKETS, self._mine(name)]
        )  # type: ignore[arg-type]
        assert reason and "can reach" in reason
        # Named in the message escaped, not raw: someone else chose it.
        assert "\n" not in reason and "\xff" not in reason

    @LINUX_ONLY
    def test_the_kernel_lists_this_hosts_sockets(self) -> None:
        # Byte for byte: a line break in a name stays in it.
        name = b"\0mmcra-test-\n-" + str(os.getpid()).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as mine:
            mine.bind(name)
            mine.listen(1)
            (listed,) = [s for s in agy_sandbox_check.unix_sockets() if s.name == name]
            assert listed.uid in (os.getuid(), None) and listed.accepts

    @staticmethod
    def _diag(name: bytes, uid: int | None, flags: int = 0, state: int = 10) -> bytes:
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
        body = struct.pack("=BBBBIII", 1, 1, state, 0, 1, 0, 0) + attrs
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

    def test_the_kernels_list_carries_owner_and_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Owners and states decide which abstract sockets count; the list keeps all.
        me, other = os.getuid(), os.getuid() + 12345
        self._kernel_says(
            monkeypatch,
            self._diag(b"\0/tmp/dbus-theirs", other)
            + self._diag(b"\0client", me, state=1)
            + self._diag(b"/var/tmp/theirs.sock", other)
            + self._done(),
        )
        Sock = agy_sandbox_check.Sock
        assert agy_sandbox_check.unix_sockets() == [
            Sock(b"\0/tmp/dbus-theirs", other, True),
            Sock(b"\0client", me, False),
            Sock(b"/var/tmp/theirs.sock", other, True),
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
            agy_sandbox_check.unix_sockets()

    def test_a_socket_list_the_kernel_will_not_give_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An empty list passed every socket check there was.
        def refused() -> list[bytes]:
            raise OSError("the kernel would not list its sockets (sock_diag)")

        monkeypatch.setattr(agy_sandbox_check, "unix_sockets", refused)
        reason = agy_sandbox_check.agy_reachable(["true"], tmp_path)
        assert reason and "sock_diag" in reason

    @LINUX_ONLY
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
                agy_sandbox_check._check_reachable(None, [self._mine(name)])  # type: ignore[arg-type]

    def test_a_timeout_does_not_print_the_wrapper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The wrapper carries agy's environment; TimeoutExpired's message is the whole
        # command, and a proxy URL in it can hold a password.
        # Built here, not written out: the diff scrubber rightly redacts a URL with a
        # password in it, and this one is in the diff.
        proxy = "http://user" + ":hunter2" + "@proxy:3128"
        wrap = ["env", "-i", f"HTTPS_PROXY={proxy}", "bwrap"]

        def slow(box: object, paths: list[bytes]) -> list[bytes]:
            raise subprocess.TimeoutExpired([*wrap, "sh"], 30)

        monkeypatch.setattr(agy_sandbox_check, "_reachable", slow)
        monkeypatch.setattr(
            agy_sandbox_check, "unix_sockets", lambda: [self._mine(b"/x.sock")]
        )
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
            None, [*self.SOCKETS, self._mine(b"relative.sock")]
        )  # type: ignore[arg-type]
        assert reason and "relative" in reason

    def test_a_sandbox_that_allows_a_user_namespace_inside_is_not_a_pass(
        self, tmp_path: Path
    ) -> None:
        # In a user namespace of its own, a process maps itself root and can mount over
        # the read-only binds; where bwrap can forbid that, the check holds it to it.
        forbidding = agy_sandbox_check._Box(
            ["bwrap", "--disable-userns", "--"], tmp_path
        )
        assert agy_sandbox_check._check_userns({"userns": "USERNS"}, forbidding)
        assert agy_sandbox_check._check_userns({"userns": ""}, forbidding) is None
        older = agy_sandbox_check._Box(["bwrap", "--"], tmp_path)
        assert agy_sandbox_check._check_userns({"userns": "USERNS"}, older) is None

    def test_the_wrapper_forbids_nested_user_namespaces_where_bwrap_can(
        self, tmp_path: Path
    ) -> None:
        wrap = agy_sandbox.agy_wrapper(tmp_path / "ws", tmp_path / "st")
        assert "--unshare-uts" in wrap and "--unshare-cgroup-try" in wrap
        assert ("--disable-userns" in wrap) == agy_sandbox._bwrap_supports(
            "--disable-userns"
        )

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

    def test_a_listing_that_failed_is_not_an_empty_one(self, tmp_path: Path) -> None:
        # A failed `ls`, or one the probe never reached, listed nothing, and read as a
        # hidden directory.
        box = agy_sandbox_check._Box(["bwrap", "--"], tmp_path)
        root = [Path("/tmp")]
        assert agy_sandbox_check._check_hidden({"ls 0": "", "ls 0?": "2"}, box, root)
        assert agy_sandbox_check._check_hidden({}, box, root)
        assert (
            agy_sandbox_check._check_hidden({"ls 0": "", "ls 0?": "0"}, box, root)
            is None
        )

    def test_a_probe_cut_short_is_not_a_pass(self, tmp_path: Path) -> None:
        box = agy_sandbox_check._Box(["bwrap", "--"], tmp_path)
        reason = agy_sandbox_check._check_runs(
            {"agy": "", "_incomplete": "canary"}, box
        )
        assert reason and "canary" in reason

    def test_strict_mode_counts_every_abstract_listener(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another owner's listener is assumed to check who connects; where that is not
        # good enough, the operator can say so.
        monkeypatch.setattr(agy_sandbox_check.os, "getuid", lambda: 1000)
        self._reach(monkeypatch)
        root_daemon = [
            agy_sandbox_check.Sock(b"\0/org/kernel/linux/storage/multipathd", 0)
        ]
        assert agy_sandbox_check._check_reachable(None, root_daemon) is None  # type: ignore[arg-type]
        monkeypatch.setenv("MMCRA_AGY_STRICT_SOCKETS", "1")
        reason = agy_sandbox_check._check_reachable(None, root_daemon)  # type: ignore[arg-type]
        assert reason and "uid 0" in reason

    def test_nested_hidden_directories_are_mounted_parents_first(
        self, tmp_path: Path
    ) -> None:
        # A parent mounted after its child took the child's mount point with it, and
        # the check, finding nothing where it looked, failed a good sandbox.
        outer = tmp_path / "srv"
        inner = outer / "checkout"
        wrap = agy_sandbox.agy_wrapper(tmp_path / "ws", tmp_path / "st", [inner, outer])
        assert wrap.index(str(outer.resolve())) < wrap.index(str(inner.resolve()))

    def test_agy_does_not_run_as_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Its capabilities dropped, root inside still owns root's files (/etc/shadow,
        # host keys) and can read them, with the network open.
        monkeypatch.setattr(agy_sandbox_check.os, "geteuid", lambda: 0)
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert not ok and "running as root" in reason

    def test_the_repository_under_review_is_hidden(self, tmp_path: Path) -> None:
        # Outside a home (/srv, a CI checkout) it was readable whole, secrets and all.
        repo = tmp_path / "srv" / "project"
        wrap = agy_sandbox.agy_wrapper(
            tmp_path / "ws", tmp_path / "st", [repo, Path("/")]
        )
        assert wrap[wrap.index(str(repo.resolve())) - 1] == "--tmpfs"
        # Not /, which would hide everything.
        assert ["--tmpfs", "/"] not in [wrap[i : i + 2] for i in range(len(wrap))]

    def test_the_sandbox_has_a_user_namespace_and_no_capabilities(self) -> None:
        wrap = agy_sandbox.agy_wrapper(Path("/var/tmp/ws"), Path("/var/tmp/st"))
        assert "--unshare-user" in wrap
        assert wrap[wrap.index("--cap-drop") + 1] == "ALL"

    @LINUX_ONLY
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
            reason = agy_sandbox_check._check_reachable(None, [self._mine(name)])  # type: ignore[arg-type]
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
        # This checkout and the directory it runs from hidden, as a review hides them.
        hidden = [
            p
            for p in dict.fromkeys(x.resolve() for x in (Path.cwd(), REPO_ROOT))
            if p != Path("/")
        ]
        assert wrap == agy_sandbox.agy_wrapper(workspace, state, hidden)
