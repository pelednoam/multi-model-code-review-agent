"""The shared base of the agy tests: fixtures and helpers, no tests of its own."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from helpers import REPO_ROOT, runner_script, shell_function

from scripts.review_loop import agy, agy_sandbox, agy_sandbox_check, backends, reviewers
from scripts.review_loop.reviewers import _choose_command


class AgyCase:
    """agy, the Antigravity CLI, in slot 3.

    agy cannot make itself read-only -- measured, it wrote outside its workspace with
    nothing denied -- so it runs under a bubblewrap wrapper, and that wrapper is proven
    at startup without a model call. Where bwrap can run, the tests below run the real
    wrapper and the real check, including with each protection removed.
    """

    MODEL = "gemini-4-argon"
    BACKENDS = {
        "hermes": True,
        "claude": True,
        "codex": False,
        "gemini": True,
        "agy": True,
    }

    # What `agy models` listed on the machine this was written on, 2026-10-14.
    LISTED = [
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ]

    @pytest.fixture(autouse=True)
    def _fresh_pick(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
    ) -> object:
        # The pick is cached per run; each test is a run. And no test may reach the
        # real `agy models` -- it is installed on some machines that run this suite.
        # The switch that runs agy unsandboxed is for hosts that are a sandbox; set
        # where the suite runs, it changed what every wrapper test saw. Tests of it
        # set it themselves.
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        # Nor the documented codex settings, which changed what a slot's choice named.
        for var in ("CODEX_MODEL", "CODEX_REASONING_EFFORT", "REVIEW_USE_CODEX"):
            monkeypatch.delenv(var, raising=False)
        # What the installed bwrap supports, asked once now: asked later, the question
        # would go to whatever fake process a test has put in place.
        agy_sandbox._bwrap_supports("--disable-userns")

        def unreachable(*a: object) -> list[str]:
            raise AssertionError("a test reached the real `agy models`")

        monkeypatch.setattr(agy, "agy_models", unreachable)
        # Nor the real agy state, which holds a sign-in token.
        fake_state = tmp_path_factory.mktemp("home") / ".gemini" / "antigravity-cli"
        (fake_state / "bin").mkdir(parents=True)
        (fake_state / "antigravity-oauth-token").write_text("not-a-real-token")
        monkeypatch.setattr(agy_sandbox, "agy_state_dir", lambda: fake_state)
        # Run as root -- in a container, a CI job -- the check refused before reaching
        # what each test is about. The real-sandbox tests skip there (_bwrap_works).
        monkeypatch.setattr(agy_sandbox_check, "_as_root", lambda: False)
        agy._PICK.clear()
        yield
        agy._PICK.clear()

    def _listing(self, monkeypatch: pytest.MonkeyPatch, slugs: list[str]) -> None:
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy, "agy_models", lambda *a: list(slugs))

    @pytest.fixture
    def on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.delenv("REVIEW_USE_GEMINI", raising=False)

    def _cmd(
        self, tmp_path: Path, slot: int = 3, prefer_hermes: bool = False
    ) -> list[str]:
        choice = _choose_command(
            slot, tmp_path / "prompt.txt", tmp_path, self.BACKENDS, prefer_hermes
        )
        assert choice is not None
        return choice[0]

    def _launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[object, dict[str, object]]:
        seen: dict[str, object] = {}
        # What the sandbox can reach is checked by its own tests; Popen is faked here.
        monkeypatch.setattr(reviewers, "agy_reachable", lambda *a: None)

        class _Proc:
            pid = 1

            def __init__(self, cmd: list[str], **kw: object) -> None:
                feed = kw["stdin"]
                text = feed.read() if hasattr(feed, "read") else None
                seen.update(kw, cmd=cmd, stdin=text)

        monkeypatch.setattr(reviewers.subprocess, "Popen", _Proc)
        round_ = reviewers._Round(
            "+DIFF-MARKER", "{}", "ctx", tmp_path, self.BACKENDS, out_dir=tmp_path
        )
        tracked = reviewers._launch_one(3, reviewers._LENSES[2], round_)
        if tracked is not None:
            tracked[3].close()
            tracked[4].close()
        return tracked, seen

    @pytest.fixture
    def scratch_made(self, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """Each launch makes one scratch directory, as agy's does, and records it."""
        made: list[Path] = []

        def launch(*a: object, **k: object) -> None:
            made.append(reviewers._scratch_dir("mmcra-test-"))

        monkeypatch.setattr(reviewers, "_launch_one", launch)
        return made

    def _launch_stub(
        self, monkeypatch: pytest.MonkeyPatch, cmd: list[str]
    ) -> list[subprocess.Popen[str]]:
        """Launch slot 1 as ``cmd``, a real process; return what was started."""
        started: list[subprocess.Popen[str]] = []
        real = subprocess.Popen

        def popen(*a: object, **k: object) -> subprocess.Popen[str]:
            started.append(real(*a, **k))  # type: ignore[call-overload]
            return started[-1]

        monkeypatch.setattr(reviewers, "_choose_command", lambda *a: (cmd, None))
        monkeypatch.setattr(reviewers.subprocess, "Popen", popen)
        return started

    @staticmethod
    def _run_prepare_agy(
        tmp_path: Path,
        root: Path = REPO_ROOT,
        writable: bool = True,
        helper_case: str = "",
        **env: str,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, list[str]]]:
        """Run the runner's prepare_agy for real; its AGY_* results, one list each.

        Real but for two helper commands: what this host's sandbox can reach is the
        sandbox check's to test (a runner test should not need bwrap to work here),
        and a prompt that cannot be written (``writable``) is a failing preamble, not a
        read-only file, which root writes anyway.
        """
        runner = runner_script()
        prompt = tmp_path / "prompt-3.txt"
        prompt.write_text("Review this.\n", encoding="utf-8")
        real = shell_function(runner, "review_helper").replace(
            "review_helper() {", "real_review_helper() {", 1
        )
        script = tmp_path / "t.sh"
        script.write_text(
            'REVIEW_ROOT="$1"\nHELPER_ROOT="$1"\nOUT_DIR="$(dirname "$2")"\n'
            + 'PROMPT_DIR="$OUT_DIR" REVIEW_TMP="$OUT_DIR"\n'
            + "SCRATCH=()\n"
            + 'AGY_SCHEMA="" AGY_WS="" AGY_STATE="" AGY_ENVP=() AGY_BWRAP=()\n'
            + real
            + "review_helper() {\n"
            + '  case "$1" in\n'
            + "    agy-reachable) return 0 ;;\n"
            # The real preamble, or a failing one.
            + ("" if writable else "    agy-input) return 1 ;;\n")
            + helper_case
            + "  esac\n"
            + '  real_review_helper "$@"\n'
            + "}\n"
            + shell_function(runner, "make_scratch")
            + shell_function(runner, "prepare_agy")
            + 'prepare_agy 3 "$2"; rc=$?\n'
            + 'printf "\\0"\n'
            + "for v in SCRATCH AGY_ENVP AGY_BWRAP; do\n"
            + '  declare -n a=$v; printf "%s\\0" "@$v" "${a[@]}"; unset -n a\n'
            + "done\n"
            + "exit $rc\n"
        )
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        done = subprocess.run(
            ["bash", str(script), str(root), str(prompt)],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(home), "TMPDIR": str(tmp_path), **env},
        )  # fmt: skip
        parts: dict[str, list[str]] = {}
        for item in done.stdout.split("\0"):
            if item in ("@SCRATCH", "@AGY_ENVP", "@AGY_BWRAP"):
                key = item[1:]
                parts[key] = []
            elif item and parts:
                parts[key].append(item)
        return done, parts

    #: Slot 3 answered by agy, as launch_reviewers returns it.
    AGY = {3: "agy"}

    @staticmethod
    def _raw(round_dir: Path) -> Path:
        """Slot 3's raw output file."""
        return round_dir / "raw-3.txt"

    @staticmethod
    def _stream(result: dict[str, object]) -> str:
        return "\n".join(
            json.dumps(event)
            for event in (
                {"event": "init", "cwd": "/tmp/mmcra-agy-x", "permission_mode": "plan"},
                {"event": "step_update", "state": "DONE", "text_delta": "{ not it"},
                {"event": "result", "result": result},
            )
        )

    def _review(self) -> dict[str, object]:
        return {
            "reviewer": "readability_performance",
            "model": self.MODEL,
            "findings": [{"severity": "warning", "issue": "dead code"}],
            "overall_assessment": "One dead branch.",
        }

    @staticmethod
    def _quiet_host(monkeypatch: pytest.MonkeyPatch) -> None:
        """This host's own sockets out of a real-sandbox test of the wrapper.

        A host with an X server or a session bus of its own correctly fails the check
        for that; these tests are about the filesystem and processes, and the socket
        rules have tests of their own, with sockets they control.
        """
        monkeypatch.setattr(agy_sandbox_check, "unix_sockets", lambda: [])

    @staticmethod
    def _bwrap_works() -> bool:
        """Real-sandbox tests need bwrap that runs, and agy (the check starts it) -- and
        not root, which the check refuses (``agy_sandbox_check._as_root``)."""
        if os.geteuid() == 0:
            return False
        if shutil.which("bwrap") is None or shutil.which("agy") is None:
            return False
        done = subprocess.run(
            [
                "bwrap",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "true",
            ],
            capture_output=True,
            check=False,
        )
        return done.returncode == 0

    @staticmethod
    def _unconfined(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        # A "sandbox" that runs the command with the wrapper stripped: no confinement.
        # agy need not be installed for this: its start-up step is answered here.
        bare = cmd[cmd.index("--") + 1 :]
        bare[-1] = bare[-1].replace("agy --version", "true")
        kw = {k: v for k, v in kw.items() if k in ("input", "env")}
        return subprocess.run(bare, capture_output=True, text=True, check=False, **kw)

    @staticmethod
    def _reach(monkeypatch: pytest.MonkeyPatch, reachable: bytes | str = b"") -> None:
        """A sandbox in which exactly ``reachable`` answers the socket probe; a str is
        the probe failing, with that reason."""

        def probe(box: object, paths: list[bytes]) -> list[bytes] | str:
            if isinstance(reachable, str):
                return reachable
            return [p for p in paths if p == reachable]

        monkeypatch.setattr(agy_sandbox_check, "_reachable", probe)

    #: The host's sockets, as ``agy_sandbox_check.unix_sockets`` lists them.
    SOCKETS = [
        agy_sandbox_check.Sock(b"/var/lib/incus/unix.socket", 0),
        agy_sandbox_check.Sock(b"/run/systemd/resolve/io.systemd.Resolve", 991),
    ]

    @staticmethod
    def _mine(name: bytes, accepts: bool = True) -> agy_sandbox_check.Sock:
        """A socket of this user's, as ``agy_sandbox_check.unix_sockets`` lists it."""
        return agy_sandbox_check.Sock(name, os.getuid(), accepts)

    def _detect(
        self, monkeypatch: pytest.MonkeyPatch, verdict: tuple[bool, str] | None
    ) -> dict[str, bool]:
        monkeypatch.setenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", "1")
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")

        def check(*a: object) -> tuple[bool, str]:
            if verdict is None:
                raise AssertionError("the agy check ran when it should not have")
            return verdict

        monkeypatch.setattr(backends, "agy_is_confined", check)
        return backends.detect_backends()
