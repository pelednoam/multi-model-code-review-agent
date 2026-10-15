"""agy, the Antigravity CLI, in slot 3: routing, launch, sandbox, output, runner."""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import REPO_ROOT, SCHEMA_PATH, runner_script, shell_function
from scripts.review_loop import agy, backends, reviewers
from scripts.review_loop.artifacts import describe_outputs
from scripts.review_loop.results import _unwrap_envelope
from scripts.review_loop.results import extract_results
from scripts.review_loop.reviewers import _choose_command

# agy_models itself, kept before _AgyCase's autouse fixture replaces it.
from scripts.review_loop.agy import agy_models as real_agy_models


class _AgyCase:
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
    def _fresh_pick(self, monkeypatch: pytest.MonkeyPatch) -> object:
        # The pick is cached per run; each test is a run. And no test may reach the
        # real `agy models` -- it is installed on some machines that run this suite.
        def unreachable() -> list[str]:
            raise AssertionError("a test reached the real `agy models`")

        monkeypatch.setattr(agy, "agy_models", unreachable)
        # Nor the real agy state, which holds a sign-in token.
        fake_state = Path(tempfile.mkdtemp()) / ".gemini" / "antigravity-cli"
        (fake_state / "bin").mkdir(parents=True)
        (fake_state / "antigravity-oauth-token").write_text("not-a-real-token")
        monkeypatch.setattr(agy, "agy_state_dir", lambda: fake_state)
        agy._picked_agy_model.cache_clear()
        yield
        agy._picked_agy_model.cache_clear()

    def _listing(self, monkeypatch: pytest.MonkeyPatch, slugs: list[str]) -> None:
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy, "agy_models", lambda: list(slugs))

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
        round_ = reviewers._Round("+DIFF-MARKER", "{}", "ctx", tmp_path, self.BACKENDS)
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

    @pytest.mark.usefixtures("on")
    def test_what_appeared_since_startup_is_checked_before_each_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The startup check describes the host as it was; a bus or socket that appears
        # during a half-hour loop was reachable from every later round.
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        self._launch(monkeypatch, tmp_path)  # installs the fake Popen
        monkeypatch.setattr(reviewers, "agy_reachable", lambda *a: "a session bus")
        round_ = reviewers._Round("+DIFF", "{}", "ctx", tmp_path, self.BACKENDS)
        tracked = reviewers._launch_one(3, reviewers._LENSES[2], round_)
        assert tracked is not None and tracked[2] == "claude"
        tracked[3].close()
        tracked[4].close()

    def test_an_interrupt_while_closing_the_input_stops_the_reviewer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Its stop was registered only after the input was closed, so an interrupt
        # there left a reviewer running that nothing tracked.
        started = self._launch_stub(monkeypatch, ["sleep", "60"])

        @contextlib.contextmanager
        def feed(text: object) -> object:
            yield None
            raise SystemExit(143)

        monkeypatch.setattr(reviewers, "_feed", feed)
        with pytest.raises(SystemExit):
            reviewers._spawn(1, ["sleep", "60"], "sleep", tmp_path, "x", tmp_path)
        (proc,) = started
        assert proc.poll() is not None

    def test_a_finished_reviewer_is_not_reaped_until_its_group_is_killed(
        self, tmp_path: Path
    ) -> None:
        # Reaped, its number -- its group's -- could go to someone else's new session,
        # which the group kill at the end of the round would then reach.
        proc, out_f, err_f = reviewers._spawn(
            1, ["true"], "true", tmp_path, None, tmp_path
        )
        try:
            while reviewers._exit_status(proc) is None:
                time.sleep(0.05)
            assert proc.returncode is None  # seen to exit, still unreaped
            assert reviewers._exit_status(proc) == 0
        finally:
            reviewers._stop(proc)
            out_f.close()
            err_f.close()
        assert proc.returncode == 0

    def test_a_reaped_reviewers_group_is_never_signalled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait()

        def killpg(*a: object) -> None:
            raise AssertionError("signalled a group number that may not be ours")

        monkeypatch.setattr(reviewers.os, "killpg", killpg)
        reviewers._stop(proc)  # type: ignore[arg-type]

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
    def _prepare_agy(
        tmp_path: Path, root: Path = REPO_ROOT, writable: bool = True, **env: str
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, list[str]]]:
        """Run the runner's prepare_agy for real; its AGY_* results, one list each."""
        runner = runner_script()
        prompt = tmp_path / "prompt-3.txt"
        prompt.write_text("Review this.\n", encoding="utf-8")
        if not writable:
            prompt.chmod(0o444)
        script = tmp_path / "t.sh"
        script.write_text(
            'REVIEW_ROOT="$1"\nAGY_SCRATCH=()\n'
            + 'AGY_SCHEMA="" AGY_WS="" AGY_STATE="" AGY_ENVP=() AGY_BWRAP=()\n'
            + shell_function(runner, "review_helper")
            + shell_function(runner, "prepare_agy")
            + 'prepare_agy 3 "$2"; rc=$?\n'
            + 'printf "\\0"\n'
            + "for v in AGY_SCRATCH AGY_ENVP AGY_BWRAP; do\n"
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
            if item.startswith("@AGY_"):
                key = item[1:]
                parts[key] = []
            elif item and parts:
                parts[key].append(item)
        return done, parts

    @staticmethod
    def _raw(round_dir: Path) -> Path:
        """Slot 3's raw output file, recorded as agy's -- as _launch_one records it."""
        (round_dir / "backend-3.txt").write_text("agy")
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
    def _bwrap_works() -> bool:
        """Real-sandbox tests need bwrap that runs, and agy (the check starts it)."""
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

    class _Reach:
        """A box in which exactly ``reachable`` answers the socket probe."""

        def __init__(self, reachable: str) -> None:
            self.reachable = reachable

        def sh(self, script: str, input: str = "") -> str:
            return self.reachable + "\0" if self.reachable + "\0" in input else ""

    UNIX = (
        "Num RefCount Protocol Flags Type St Inode Path\n"
        "0: 00000002 00000000 00010000 0001 01 1 /var/lib/incus/unix.socket\n"
        "0: 00000002 00000000 00010000 0001 01 2 /run/systemd/resolve/io.systemd.Resolve\n"
    )

    def _detect(
        self, monkeypatch: pytest.MonkeyPatch, verdict: tuple[bool, str] | None
    ) -> dict[str, bool]:
        monkeypatch.setenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", "1")
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")

        def check() -> tuple[bool, str]:
            if verdict is None:
                raise AssertionError("the agy check ran when it should not have")
            return verdict

        monkeypatch.setattr(backends, "agy_is_confined", check)
        return backends.detect_backends()


class TestAgyRouting(_AgyCase):
    """Which backend slot 3 gets, and which model agy runs."""

    @pytest.mark.usefixtures("on")
    def test_slot_3_runs_agy_when_switched_on(self, tmp_path: Path) -> None:
        choice = _choose_command(3, tmp_path / "p.txt", tmp_path, self.BACKENDS)
        assert choice is not None
        cmd, label = choice
        assert cmd[0] == "agy"
        assert cmd[cmd.index("--model") + 1] == self.MODEL
        # The prompt names the model that actually answered, not the lens default.
        assert label == self.MODEL

    def test_on_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("REVIEW_USE_AGY", raising=False)
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.delenv("REVIEW_USE_GEMINI", raising=False)
        assert self._cmd(tmp_path)[0] == "agy"

    def test_zero_switches_it_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Switched off at detection, which is what the routing reads.
        monkeypatch.setenv("REVIEW_USE_AGY", "0")
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.delenv("REVIEW_USE_GEMINI", raising=False)
        found = self._detect(monkeypatch, None)
        assert found["agy"] is False
        choice = _choose_command(3, tmp_path / "prompt.txt", tmp_path, found)
        assert choice is not None and choice[0][0] == "claude"

    def test_on_by_default_still_means_verified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Default-on changes who runs the check, not whether: unset still runs it.
        monkeypatch.delenv("REVIEW_USE_AGY", raising=False)
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        assert self._detect(monkeypatch, (False, "write landed"))["agy"] is False

    def test_no_gemini_listed_means_no_agy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.delenv("REVIEW_USE_GEMINI", raising=False)
        self._listing(monkeypatch, ["claude-sonnet-4-6", "gpt-oss-120b-medium"])
        assert self._cmd(tmp_path)[0] == "claude"

    @pytest.mark.usefixtures("on")
    def test_a_broken_schema_falls_through_instead_of_aborting(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The schema comes from this checkout, which may be the tree under review.
        broken = tmp_path / "schema.json"
        broken.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(reviewers, "RESULT_SCHEMA", broken)
        assert self._cmd(tmp_path)[0] == "claude"

    @pytest.mark.usefixtures("on")
    @pytest.mark.parametrize(
        "text", [None, '{"type": "not-a-type"}'], ids=["missing", "invalid"]
    )
    def test_a_missing_or_invalid_schema_falls_through_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str | None
    ) -> None:
        # Missing, agy ran with no schema at all; JSON that is not a schema, agy rejected
        # it only after launch, when the slot could no longer go anywhere else.
        schema = tmp_path / "schema.json"
        if text is not None:
            schema.write_text(text, encoding="utf-8")
        monkeypatch.setattr(reviewers, "RESULT_SCHEMA", schema)
        assert self._cmd(tmp_path)[0] == "claude"

    def test_unset_model_is_picked_from_what_this_machine_lists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.delenv("AGY_MODEL", raising=False)
        self._listing(monkeypatch, self.LISTED)
        choice = _choose_command(3, tmp_path / "p.txt", tmp_path, self.BACKENDS)
        assert choice is not None
        cmd, label = choice
        assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-high"
        assert label == "gemini-3.8-flash-high"

    def test_an_explicit_model_wins_without_asking_agy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGY_MODEL", "claude-opus-4-6-thinking")
        # The autouse fixture makes any call to `agy models` fail the test.
        assert agy.agy_model() == "claude-opus-4-6-thinking"

    def test_the_pick_is_made_once_per_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The confinement check and the reviewer must run the same model.
        calls: list[int] = []
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(
            agy, "agy_models", lambda: calls.append(1) or list(self.LISTED)
        )
        assert agy.agy_model() == agy.agy_model() == "gemini-3.8-flash-high"
        assert len(calls) == 1

    @pytest.mark.parametrize(
        ("listed", "best"),
        [
            # This machine today.
            (_AgyCase.LISTED, "gemini-3.8-flash-high"),
            # The day the plan includes Argon: a newer version beats a higher effort.
            ([*_AgyCase.LISTED, "gemini-4-argon"], "gemini-4-argon"),
            # Same version: Pro above Flash above Lite, whatever the effort.
            (
                [
                    "gemini-3.8-flash-lite-high",
                    "gemini-3.8-flash-low",
                    "gemini-3.8-pro-low",
                ],
                "gemini-3.8-pro-low",
            ),
            # Versions compare as numbers, not strings.
            (
                ["gemini-3.10-flash-low", "gemini-3.9-flash-high"],
                "gemini-3.10-flash-low",
            ),
            # Newer, but not a text model: an image or speech model cannot review.
            (
                [
                    "gemini-3.9-flash-image",
                    "gemini-3.9-pro-tts",
                    "gemini-3.8-flash-high",
                ],
                "gemini-3.8-flash-high",
            ),
            # Nothing Gemini: no pick, rather than a duplicate of another slot's provider.
            (["claude-opus-4-6-thinking", "gpt-oss-120b-medium"], None),
            ([], None),
        ],
    )
    def test_ranking(self, listed: list[str], best: str | None) -> None:
        assert agy.best_agy_model(listed) == best

    def test_the_listing_is_parsed_from_agys_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The real agy_models, with a fake agy behind it. Only that one attribute:
        # undo() would also drop the fake state directory the fixture put in place.
        monkeypatch.setattr(agy, "agy_models", real_agy_models)
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        out = "Fetching available models...\n" + "".join(
            f"{slug}\tDisplay Name\n" for slug in self.LISTED
        )
        seen: dict[str, object] = {}

        def run(cmd: list[str], **kw: object) -> SimpleNamespace:
            seen.update(cmd=cmd, cwd=kw.get("cwd"))
            return SimpleNamespace(stdout=out, stderr="")

        monkeypatch.setattr(agy, "run", run)
        assert agy.agy_models() == self.LISTED
        # Listing runs agy too: under the wrapper, in an empty directory of its own.
        cmd = seen["cmd"]
        assert isinstance(cmd, list) and cmd[-2:] == ["agy", "models"]
        wrap = cmd[: cmd.index("--") + 1]
        state = Path(wrap[wrap.index(str(agy.agy_state_dir())) - 1])
        assert wrap == agy.agy_sandbox(Path(str(seen["cwd"])), state)

    def test_a_listing_that_fails_is_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agy, "agy_models", real_agy_models)

        def hang(cmd: list[str], **kw: object) -> None:
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(agy, "run", hang)
        assert agy.agy_models() == []

    @pytest.mark.usefixtures("on")
    def test_only_slot_3(self, tmp_path: Path) -> None:
        for slot in (1, 2, 4):
            assert self._cmd(tmp_path, slot)[0] != "agy"

    @pytest.mark.usefixtures("on")
    def test_bedrock_opt_in_still_takes_slot_3(self, tmp_path: Path) -> None:
        assert self._cmd(tmp_path, prefer_hermes=True)[0] == "hermes"

    @pytest.mark.parametrize("asked", [None, "1"], ids=["default", "by-name"])
    def test_a_bedrock_project_without_hermes_gets_agy_only_by_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, asked: str | None
    ) -> None:
        # Pinned to Bedrock to keep the code with providers it chose; with Hermes
        # unavailable, slot 3 fell to agy -- a new provider -- by default.
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.delenv("REVIEW_USE_GEMINI", raising=False)
        if asked is None:
            monkeypatch.delenv("REVIEW_USE_AGY", raising=False)
        else:
            monkeypatch.setenv("REVIEW_USE_AGY", asked)
        found = {**self.BACKENDS, "hermes": False}
        choice = _choose_command(3, tmp_path / "p.txt", tmp_path, found, True)
        assert choice is not None
        assert choice[0][0] == ("agy" if asked else "claude")

    @pytest.mark.parametrize(
        ("value", "on"),
        [(None, True), ("1", True), ("yes", True),
         ("0", False), ("false", False), ("No", False), (" off ", False), ("", False)],
    )  # fmt: skip
    def test_every_value_that_reads_as_off_is_off(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None, on: bool
    ) -> None:
        # Only "0" switched it off; REVIEW_USE_AGY=false sent the diff to Google.
        if value is None:
            monkeypatch.delenv("REVIEW_USE_AGY", raising=False)
        else:
            monkeypatch.setenv("REVIEW_USE_AGY", value)
        assert agy.agy_enabled() is on

    @pytest.mark.usefixtures("on")
    def test_an_unverified_agy_is_not_used(self, tmp_path: Path) -> None:
        # detect_backends sets agy False when the canary fails; that must stick.
        choice = _choose_command(
            3, tmp_path / "p.txt", tmp_path, {**self.BACKENDS, "agy": False}
        )
        assert choice is not None and choice[0][0] != "agy"


class TestAgyCommand(_AgyCase):
    """The agy invocation: its pinned flags, schema and input."""

    @pytest.mark.usefixtures("on")
    def test_the_safety_flags_are_pinned(self, tmp_path: Path) -> None:
        cmd = self._cmd(tmp_path)
        assert "--sandbox" in cmd
        # agy implements plan mode as a slash command: with expansion off it does
        # nothing, and agy says so. A no-op flag must not pose as a protection.
        assert not any(arg == "--mode" or arg.startswith("--mode=") for arg in cmd)
        # The prompt carries a contributor's diff; it must not expand as a slash command.
        assert "--disable-slash-commands" in cmd
        assert "--dangerously-skip-permissions" not in cmd

    @pytest.mark.usefixtures("on")
    def test_the_schema_is_enforced_by_the_cli(self, tmp_path: Path) -> None:
        # Inline: the sandbox hides home and /tmp, so a path to the file would not resolve.
        cmd = self._cmd(tmp_path)
        assert json.loads(cmd[cmd.index("--json-schema") + 1]) == json.loads(
            SCHEMA_PATH.read_text()
        )

    @pytest.mark.usefixtures("on")
    def test_the_prompt_is_not_an_argument(self, tmp_path: Path) -> None:
        # One argument is capped at 128 KB on Linux; a large diff passes that.
        cmd = self._cmd(tmp_path)
        assert not {"-p", "--print", "--prompt"} & set(cmd)
        assert cmd[cmd.index("--input-format") + 1] == "stream-json"
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"

    @pytest.mark.usefixtures("on")
    def test_the_loop_owns_the_deadline(self, tmp_path: Path) -> None:
        # REVIEWER_TIMEOUT can raise the loop's deadline past any ceiling fixed here.
        cmd = self._cmd(tmp_path)
        assert cmd[cmd.index("--print-timeout") + 1] == "0"


class TestAgyLaunch(_AgyCase):
    """Starting agy: its workspace, its sandbox, its scratch and its cleanup."""

    @pytest.mark.usefixtures("on")
    def test_launch_is_sandboxed_outside_the_repo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        tracked, seen = self._launch(monkeypatch, tmp_path)
        assert tracked is not None
        assert tracked[2] == "agy"  # reported as agy, not as its wrapper

        workspace = Path(str(seen["cwd"]))
        assert workspace.is_dir() and not any(workspace.iterdir())
        assert REPO_ROOT not in workspace.parents
        cmd = seen["cmd"]
        assert isinstance(cmd, list)
        wrap = cmd[: cmd.index("--") + 1]
        state = Path(wrap[wrap.index(str(agy.agy_state_dir())) - 1])
        assert wrap == agy.agy_sandbox(workspace, state)
        assert cmd[len(wrap)] == "agy"
        # A throwaway copy of agy's state, holding the sign-in and nothing else.
        assert sorted(p.name for p in state.iterdir()) == ["antigravity-oauth-token"]
        assert REPO_ROOT not in state.parents

        stdin = seen["stdin"]
        assert isinstance(stdin, str)
        (line,) = stdin.splitlines()
        content = json.loads(line)["message"]["content"]
        assert content.startswith(agy.AGY_PREAMBLE)
        assert "+DIFF-MARKER" in content
        # The prompt file is the record of what agy was actually sent.
        assert (tmp_path / "prompt-3.txt").read_text() == content

    def test_scratch_goes_when_the_round_ends(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scratch_made: list[Path]
    ) -> None:
        # agy's state copy holds its sign-in token: not kept for the rest of the loop.
        monkeypatch.setattr(reviewers, "_await_reviewers", lambda *a: None)
        reviewers.launch_reviewers(tmp_path, "diff", "{}", "", {})
        assert scratch_made and not any(p.exists() for p in scratch_made)

    def test_scratch_goes_when_the_round_is_interrupted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scratch_made: list[Path]
    ) -> None:
        def interrupted(*a: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(reviewers, "_await_reviewers", interrupted)
        with pytest.raises(KeyboardInterrupt):
            reviewers.launch_reviewers(tmp_path, "diff", "{}", "", {})
        assert scratch_made and not any(p.exists() for p in scratch_made)

    def test_a_finished_reviewers_helpers_are_killed_with_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The round's cleanup killed only reviewers still running; one that had exited
        # left its helper running in its group, spending quota and writing round_dir.
        cmd = ["sh", "-c", "sleep 300 & echo $! > helper; exit 0"]
        tracked: list[reviewers.ReviewerProc] = []

        def launch(slot: int, *a: object, **k: object) -> reviewers.ReviewerProc | None:
            if slot != 1:
                return None
            proc, out_f, err_f = reviewers._spawn(
                1, cmd, "sh", tmp_path, None, tmp_path
            )
            tracked.append((1, proc, "sh", out_f, err_f))
            return tracked[-1]

        monkeypatch.setattr(reviewers, "_launch_one", launch)

        def await_exit(procs: list[reviewers.ReviewerProc], *a: object) -> None:
            # As the loop waits: seeing the exit without reaping the reviewer.
            while reviewers._exit_status(procs[0][1]) is None:
                time.sleep(0.05)

        monkeypatch.setattr(reviewers, "_await_reviewers", await_exit)
        reviewers.launch_reviewers(tmp_path, "diff", "{}", "", {})
        helper = int((tmp_path / "helper").read_text())
        time.sleep(0.2)
        stat = Path(f"/proc/{helper}/stat")
        assert not stat.exists() or stat.read_text().split()[2] == "Z"

    def test_scratch_has_one_exit_hook_not_one_per_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def registered(*a: object, **k: object) -> None:
            raise AssertionError("an exit hook per scratch directory")

        monkeypatch.setattr(reviewers.atexit, "register", registered)
        reviewers._scratch_dir("mmcra-test-")
        reviewers._release_scratch()

    def test_an_interrupted_launch_kills_its_reviewer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Between Popen and the return, the caller has nothing to clean up with.
        started = self._launch_stub(monkeypatch, ["sleep", "60"])

        def interrupted(*a: object, **k: object) -> None:
            if "launched via" in str(a[0]):
                raise SystemExit(143)

        monkeypatch.setattr(reviewers, "print", interrupted, raising=False)
        with pytest.raises(SystemExit):
            round_ = reviewers._Round("diff", "{}", "", tmp_path, self.BACKENDS)
            reviewers._launch_one(1, reviewers._LENSES[0], round_)
        (proc,) = started
        assert proc.poll() is not None

    def test_a_large_prompt_does_not_hold_up_the_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Through a pipe, a prompt over 64 KB blocked until the reviewer read it.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "claude").write_text("#!/bin/sh\nsleep 60\n")
        (bin_dir / "claude").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        started = self._launch_stub(monkeypatch, ["claude"])
        begun = time.monotonic()
        diff = "+" + "x" * 300_000
        round_ = reviewers._Round(diff, "{}", "", tmp_path, self.BACKENDS)
        tracked = reviewers._launch_one(1, reviewers._LENSES[0], round_)
        elapsed = time.monotonic() - begun
        assert tracked is not None
        reviewers._kill_group(tracked[1])
        tracked[1].wait()
        tracked[3].close()
        tracked[4].close()
        assert started and elapsed < 10

    @pytest.mark.parametrize("helper", ["agy_models", "agy_is_confined"])
    @pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
    def test_detection_cleans_up_on_a_signal(
        self, tmp_path: Path, helper: str, sig: str
    ) -> None:
        # Both make temporary directories -- agy_models a copy of the sign-in token -- and
        # a plain SIGTERM, or the SIGHUP of a closed terminal, ended Python without
        # removing them.
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        home = tmp_path / "home"
        state = home / ".gemini" / "antigravity-cli"
        state.mkdir(parents=True)
        (state / "antigravity-oauth-token").write_text("t")
        script = (
            "import os, signal, sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from scripts.review_loop import agy\n"
            "agy.tempfile.tempdir = sys.argv[2]\n"
            "agy._canary_parent = lambda: sys.argv[2]\n"
            # Nothing real runs (run is replaced), so bwrap need not be installed.
            "agy.shutil.which = lambda c: '/usr/bin/' + c\n"
            "def killed(*a, **k):\n"
            f"    os.kill(os.getpid(), signal.{sig})\n"
            "    time.sleep(30)\n"
            "agy.run = killed\n"
            f"agy.{helper}()\n"
        )
        env = {**os.environ, "HOME": str(home)}
        env.pop("MMCRA_SKIP_AGY_SANDBOX_CHECK", None)
        done = subprocess.run(
            [sys.executable, "-I", "-c", script, str(REPO_ROOT), str(scratch)],
            capture_output=True, text=True, env=env, timeout=20,
        )  # fmt: skip
        assert done.returncode == 128 + getattr(signal, sig), done.stderr
        assert not list(scratch.iterdir())

    def test_the_sandbox_check_copies_no_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # It checks the mounts, which need no sign-in.
        def seeded(*a: object) -> None:
            raise AssertionError("the check copied agy's sign-in token")

        def nothing_runs(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 1, "", "bwrap: no")

        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy, "agy_seed_state", seeded)
        monkeypatch.setattr(agy, "run", nothing_runs)
        ok, reason = agy.agy_is_confined()
        assert not ok and "could not start" in reason

    @pytest.mark.usefixtures("on")
    def test_egress_is_said_where_agy_runs_not_where_it_is_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:  # fmt: skip
        # Said at detection, it was also said for a round whose slot 3 went to Hermes.
        monkeypatch.setenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", "1")
        self._detect(monkeypatch, None)
        assert agy.AGY_EGRESS not in capsys.readouterr().out
        self._launch(monkeypatch, tmp_path)
        assert agy.AGY_EGRESS in capsys.readouterr().out
        # And the full-mode runner says the same thing.
        assert agy.AGY_EGRESS in runner_script()

    def test_the_runner_keeps_values_off_timeouts_command_line(
        self, tmp_path: Path
    ) -> None:
        # timeout ran ahead of `env -i`, holding every allowlisted value -- a proxy URL
        # with a password -- in a command line any local user can read.
        proxy = "http://u:secret@proxy:1"
        done, parts = self._prepare_agy(tmp_path, HTTPS_PROXY=proxy)
        assert done.returncode == 0, done.stdout + done.stderr
        assert parts["AGY_ENVP"][1] == "-i"
        assert f"HTTPS_PROXY={proxy}" in parts["AGY_ENVP"]
        assert parts["AGY_BWRAP"][0].endswith("bwrap")
        assert not any("secret" in a for a in parts["AGY_BWRAP"])
        # And timeout goes between them.
        launch = (
            '"${AGY_ENVP[@]}" timeout --foreground -k 10 "$REVIEWER_TIMEOUT"'
            ' "${AGY_BWRAP[@]}" agy'
        )
        assert launch in runner_script()
        # The preamble went into the prompt file, and both scratch directories are listed.
        assert (tmp_path / "prompt-3.txt").read_text().startswith(agy.AGY_PREAMBLE)
        assert len(parts["AGY_SCRATCH"]) == 2

    def test_an_unusable_schema_hands_the_slot_on(self, tmp_path: Path) -> None:
        # A root whose code is this checkout's but whose schema is not a schema.
        root = tmp_path / "root"
        (root / "docs").mkdir(parents=True)
        (root / "scripts").symlink_to(REPO_ROOT / "scripts")
        (root / "docs" / "ensemble_review_result_schema.json").write_text('{"type": 7}')
        done, parts = self._prepare_agy(tmp_path, root=root)
        assert done.returncode == 1
        assert "SKIPPED (agy result schema unusable)" in done.stdout
        assert parts["AGY_SCRATCH"] == []  # nothing made, nothing left behind

    def test_scratch_is_listed_the_moment_it_exists(self, tmp_path: Path) -> None:
        # A launch that fails after making its directories leaves them to the runner,
        # which removes whatever AGY_SCRATCH lists: no failure path cleans up alone.
        root = tmp_path / "root"
        (root / "docs").mkdir(parents=True)
        (root / "scripts").symlink_to(REPO_ROOT / "scripts")
        shutil.copy(SCHEMA_PATH, root / "docs" / "ensemble_review_result_schema.json")
        done, parts = self._prepare_agy(tmp_path, root=root, writable=False)
        assert done.returncode == 1, done.stdout
        assert "could not prepare agy's prompt" in done.stdout
        assert len(parts["AGY_SCRATCH"]) == 2

    @pytest.mark.usefixtures("on")
    def test_a_host_that_is_already_a_sandbox_runs_agy_bare(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", "1")
        _, seen = self._launch(monkeypatch, tmp_path)
        cmd = seen["cmd"]
        assert isinstance(cmd, list) and cmd[0] == "agy"

    @pytest.mark.usefixtures("on")
    def test_a_workspace_inside_the_repo_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # TMPDIR pointed into the project puts the one writable directory there.
        repo = tmp_path / "repo"
        (repo / "tmp").mkdir(parents=True)
        monkeypatch.setattr(reviewers, "REPO_ROOT", repo)
        monkeypatch.setattr(reviewers.tempfile, "tempdir", str(repo / "tmp"))
        tracked, seen = self._launch(monkeypatch, tmp_path)
        # Not agy, and not an empty slot either: the next backend takes it.
        cmd = seen["cmd"]
        assert isinstance(cmd, list) and "agy" not in cmd and cmd[0] == "claude"
        assert not list((repo / "tmp").iterdir())  # and the refused workspace is gone

    def test_the_repo_under_review_is_refused_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # --repo can name a project that is neither this checkout nor the cwd.
        target = tmp_path / "target"
        (target / ".tmp").mkdir(parents=True)
        monkeypatch.setattr(reviewers.tempfile, "tempdir", str(target / ".tmp"))
        assert reviewers._agy_workspace(target) is None
        assert reviewers._agy_workspace(tmp_path / "elsewhere") is not None

    def test_strays_survive_odd_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HOME with a trailing slash crashed detection; a space in TMPDIR failed it."""
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        spaced = Path(tempfile.mkdtemp(prefix="review scratch "))
        monkeypatch.setenv("HOME", str(Path.home()) + "/")
        monkeypatch.setattr(agy.tempfile, "tempdir", str(spaced))
        ok, reason = agy.agy_is_confined()
        assert ok, reason

    def test_a_slot_whose_agy_cannot_be_set_up_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.setattr(reviewers, "_agy_workspace", lambda repo=None: None)
        _, seen = self._launch(monkeypatch, tmp_path)
        cmd = seen["cmd"]
        assert isinstance(cmd, list) and cmd[0] == "claude"

    @pytest.mark.parametrize("value", ["", "0"])
    def test_skip_flag_zero_keeps_the_sandbox(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        # REVIEW_USE_AGY=0 means off; MMCRA_SKIP_AGY_SANDBOX_CHECK=0 must not mean "skip".
        monkeypatch.setenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", value)
        assert agy.agy_unsandboxed() is False


class TestAgyOutput(_AgyCase):
    """Reading agy's stream: only a finished turn is a review."""

    def test_structured_output_is_the_result(self, tmp_path: Path) -> None:
        self._raw(tmp_path).write_text(
            self._stream(
                {
                    "status": "SUCCESS",
                    "response": "done",
                    "structured_output": self._review(),
                }
            )
        )
        result = extract_results(tmp_path)[2]
        assert result is not None
        assert result["findings"][0]["issue"] == "dead code"
        assert (tmp_path / "result-3.json").exists()

    def test_the_response_text_is_the_fallback(self, tmp_path: Path) -> None:
        self._raw(tmp_path).write_text(
            self._stream({"status": "SUCCESS", "response": json.dumps(self._review())})
        )
        result = extract_results(tmp_path)[2]
        assert result is not None and result["model"] == self.MODEL

    def test_a_line_separator_inside_a_string_does_not_lose_the_review(
        self, tmp_path: Path
    ) -> None:
        # Valid JSON may carry U+0085 raw; str.splitlines() splits on it, NDJSON does not.
        review = {**self._review(), "overall_assessment": "one\u0085two"}
        event = {
            "event": "result",
            "result": {"status": "SUCCESS", "structured_output": review},
        }
        init = json.dumps({"event": "init"})
        self._raw(tmp_path).write_text(
            init + "\n" + json.dumps(event, ensure_ascii=False), encoding="utf-8"
        )
        result = extract_results(tmp_path)[2]
        assert result is not None and result["overall_assessment"] == "one\u0085two"

    @pytest.mark.parametrize("status", ["ERROR", "CANCELED", "INTERRUPTED", "WAITING"])
    def test_an_unfinished_turn_is_not_a_review(
        self, tmp_path: Path, status: str
    ) -> None:
        # However review-shaped its output, a turn that did not finish is a hole in
        # the round, not a clean bill -- or it would count towards convergence.
        self._raw(tmp_path).write_text(
            self._stream({"status": status, "structured_output": self._review()})
        )
        assert extract_results(tmp_path)[2] is None

    def test_a_failed_turn_says_why(self, tmp_path: Path) -> None:
        self._raw(tmp_path).write_text(
            self._stream({"status": "ERROR", "error": "quota exhausted for this model"})
        )
        assert "quota exhausted" in describe_outputs(tmp_path)

    def test_a_refused_tool_call_is_named_not_silent(self, tmp_path: Path) -> None:
        # The first real sweep: `ls -la`, denied, turn over, empty response, and the
        # round said only "NO OUTPUT".
        self._raw(tmp_path).write_text(
            self._stream(
                {
                    "status": "SUCCESS",
                    "response": "",
                    "denied_actions": [
                        {"action": "command", "display_name": "RunCommand"}
                    ],
                }
            )
        )
        said = describe_outputs(tmp_path)
        assert "refused tool call" in said and "RunCommand" in said

    def test_a_malformed_denied_list_still_reports(self, tmp_path: Path) -> None:
        self._raw(tmp_path).write_text(
            self._stream({"status": "ERROR", "error": "bad", "denied_actions": True})
        )
        assert "bad" in describe_outputs(tmp_path)

    def test_an_agy_shaped_line_from_another_backend_is_not_agys(
        self, tmp_path: Path
    ) -> None:
        # Any reviewer can be talked into printing it; a failed status would otherwise
        # turn that reviewer's real answer into a missing one.
        ok = {"status": "SUCCESS", "structured_output": self._review()}
        injected = self._stream(ok)
        (tmp_path / "raw-3.txt").write_text(injected)
        (tmp_path / "backend-3.txt").write_text("gemini")
        assert extract_results(tmp_path)[2] is None
        # The same stream from agy is agy's answer.
        (tmp_path / "backend-3.txt").write_text("agy")
        assert extract_results(tmp_path)[2] is not None

    def test_a_stream_with_no_result_event_is_no_review(self) -> None:
        # Without a result event, the stream was read as plain text, and a bare object
        # in it passed as agy's verdict.
        assert _unwrap_envelope(json.dumps(self._review()), agy_ran=True) == ""

    def test_claude_envelopes_are_unaffected(self) -> None:
        claude = json.dumps({"type": "result", "result": '{"a": 1}'})
        assert _unwrap_envelope(claude) == '{"a": 1}'


class TestAgySandboxCheck(_AgyCase):
    """The startup check that proves the wrapper, with no model call."""

    def test_the_real_sandbox_passes(self) -> None:
        """Not a mock: the wrapper agy runs under, against the real filesystem."""
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        ok, reason = agy.agy_is_confined()
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
        real = agy.agy_sandbox
        target = tuple(str(Path.home()) if a == "HOME" else a for a in drop)

        def weakened(ws: Path, st: Path) -> list[str]:
            wrap = real(ws, st)
            for i in range(len(wrap) - len(target) + 1):
                if tuple(wrap[i : i + len(target)]) == target:
                    return wrap[:i] + wrap[i + len(target) :]
            raise AssertionError(f"{target} is not in the wrapper")

        monkeypatch.setattr(agy, "agy_sandbox", weakened)
        ok, reason = agy.agy_is_confined()
        assert not ok and caught.replace("HOME", str(Path.home())) in reason

    def test_no_place_for_the_canary_is_said_not_misreported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A canary in /tmp is hidden by the sandbox: "could not read" would be a lie.
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy, "_canary_parent", lambda: None)
        ok, reason = agy.agy_is_confined()
        assert not ok and "/var/tmp" in reason

    def test_a_sandbox_that_lets_writes_through_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy, "run", self._unconfined)
        ok, reason = agy.agy_is_confined()
        assert not ok and "ALLOWED a write" in reason

    def test_a_sandbox_that_cannot_start_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The codex ambiguity again: an untouched canary is also what nothing looks like."""
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(
            agy,
            "run",
            lambda *a, **k: SimpleNamespace(
                stdout="", stderr="bwrap: setting up uid map: Permission denied"
            ),
        )
        ok, reason = agy.agy_is_confined()
        assert not ok and "could not start" in reason and "namespaces" in reason

    def test_an_account_with_no_passwd_entry_disables_only_agy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A container under a bare numeric UID: getpwuid raised out of detection, and
        # no reviewer started at all.
        def unknown(uid: int) -> object:
            raise KeyError(uid)

        monkeypatch.setattr(agy.pwd, "getpwuid", unknown)
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        ok, reason = agy.agy_is_confined()
        assert not ok and "passwd" in reason
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.setenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", "1")
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        found = backends.detect_backends()
        assert found["agy"] is False and found["claude"] is True

    def test_any_reachable_socket_is_refused_not_only_named_ones(self) -> None:
        # A list of known sockets lags what is installed: Incus's was not on it.
        box = self._Reach("/var/lib/incus/unix.socket")
        reason = agy._check_reachable(box, self.UNIX)  # type: ignore[arg-type]
        assert reason and "/var/lib/incus/unix.socket" in reason

    def test_the_resolvers_socket_is_put_back_on_purpose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agy, "_resolver_dir", lambda: Path("/run/systemd/resolve"))
        box = self._Reach("/run/systemd/resolve/io.systemd.Resolve")
        assert agy._check_reachable(box, self.UNIX) is None  # type: ignore[arg-type]

    def test_any_abstract_session_bus_is_refused(self) -> None:
        # Not only dbus-launch's under /tmp: IBus keeps one under ~/.cache.
        unix = self.UNIX + "0: 2 0 10000 0001 01 3 @/home/u/.cache/ibus/dbus-AbCd\n"
        reason = agy._check_reachable(self._Reach("nothing"), unix)  # type: ignore[arg-type]
        assert reason and "session bus" in reason

    def test_a_socket_path_with_a_space_is_checked_whole(self) -> None:
        # Split on whitespace, "/var/tmp/my app.sock" was probed as "/var/tmp/my".
        unix = self.UNIX + "0: 2 0 10000 0001 01 4 /var/tmp/my app.sock\n"
        reason = agy._check_reachable(self._Reach("/var/tmp/my app.sock"), unix)  # type: ignore[arg-type]
        assert reason and "/var/tmp/my app.sock" in reason

    @pytest.mark.parametrize("unix", ["", "cat: /proc/net/unix: No such file\n"])
    def test_a_socket_list_that_cannot_be_read_is_not_a_pass(self, unix: str) -> None:
        # An empty list passed every socket check there was.
        reason = agy._check_reachable(self._Reach("x"), unix)  # type: ignore[arg-type]
        assert reason and "socket list" in reason

    def test_a_socket_with_a_relative_name_is_not_a_pass(self) -> None:
        unix = self.UNIX + "0: 2 0 10000 0001 01 5 relative.sock\n"
        reason = agy._check_reachable(self._Reach("x"), unix)  # type: ignore[arg-type]
        assert reason and "relative" in reason

    @pytest.mark.parametrize(("reply", "refused"), [(b"\x01", True), (b"\x02", False)])
    def test_an_x_server_that_admits_this_user_without_a_cookie_is_refused(
        self, reply: bytes, refused: bool
    ) -> None:
        # Only named before; stock GNOME and Xwayland admit the local user, and a
        # program inside could type into a terminal outside.
        name = f"@/tmp/.X11-unix/X{os.getpid() % 10000 + 50000}"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind("\0" + name[1:])
        server.listen(1)

        def answer() -> None:
            conn, _ = server.accept()
            with conn:
                conn.recv(12)
                conn.sendall(reply)

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        try:
            unix = "Num RefCount Protocol Flags Type St Inode Path\n"
            unix += f"0: 2 0 10000 0001 01 6 {name}\n"
            reason = agy._check_reachable(self._Reach("x"), unix)  # type: ignore[arg-type]
        finally:
            thread.join(timeout=5)
            server.close()
        assert bool(reason and "X server" in reason) is refused

    def test_a_home_of_root_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Dropped from the list, / was neither hidden nor checked: /.ssh stayed readable.
        monkeypatch.setenv("HOME", "/")
        with pytest.raises(OSError, match="home directory of /"):
            agy._homes()
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        ok, reason = agy.agy_is_confined()
        assert not ok and "home directory of /" in reason

    def test_a_socket_under_a_root_only_directory_needs_no_mask(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Path.exists raised PermissionError there, and the slot was lost over it.
        def denied(self: Path, strict: bool = False) -> Path:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(agy.os.path, "lexists", lambda p: True)
        monkeypatch.setattr(Path, "resolve", denied)
        assert agy._maskable("/var/lib/incus/unix.socket") is False

    def test_no_bwrap_is_not_a_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(agy.shutil, "which", lambda c: None)
        ok, reason = agy.agy_is_confined()
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
        monkeypatch.setattr(agy, "run", boom)
        ok, reason = agy.agy_is_confined()
        assert not ok and "check failed" in reason

    def test_the_check_proves_the_wrapper_a_reviewer_runs_under(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def record(cmd: list[str], **kw: object) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(stdout="", stderr="")

        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy, "run", record)
        agy.agy_is_confined()
        (first,) = calls  # it stops at "could not start"
        wrap = first[: first.index("--") + 1]
        state = Path(wrap[wrap.index(str(agy.agy_state_dir())) - 1])
        last_bind = len(wrap) - 1 - wrap[::-1].index("--bind")
        workspace = Path(wrap[last_bind + 1])
        assert wrap == agy.agy_sandbox(workspace, state)


class TestAgyDetection(_AgyCase):
    """Whether agy is used at all, decided once at startup."""

    @pytest.mark.usefixtures("on")
    def test_an_unconfined_agy_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        found = self._detect(monkeypatch, (False, "write landed"))
        assert found["agy"] is False
        assert found["claude"] is True

    @pytest.mark.usefixtures("on")
    def test_a_confined_agy_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        assert self._detect(monkeypatch, (True, "verified"))["agy"] is True

    def test_nothing_runs_when_switched_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "0")
        # And the backend table says so, rather than reporting an unused agy as present.
        assert self._detect(monkeypatch, None)["agy"] is False

    def test_switched_on_without_a_model_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        self._listing(monkeypatch, ["claude-sonnet-4-6"])
        assert self._detect(monkeypatch, (True, "verified"))["agy"] is False

    def test_an_unconfined_host_never_lists_models(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Picking a model runs agy; a host that will drop agy must not run it at all.
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)

        def listed() -> list[str]:
            raise AssertionError("agy models ran before the sandbox was proven")

        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(agy, "agy_models", listed)
        assert self._detect(monkeypatch, (False, "no bwrap"))["agy"] is False

    def test_a_picked_model_still_has_to_pass_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        self._listing(monkeypatch, self.LISTED)
        assert self._detect(monkeypatch, (False, "no bwrap"))["agy"] is False

    def test_the_loop_decides_on_bedrock_before_it_detects(self) -> None:
        # Detection ran first, so the loop could not tell it the project was pinned.
        entry = (REPO_ROOT / "scripts" / "review_until_converged.py").read_text()
        decided = entry.index("prefer_hermes = args.prefer_hermes or")
        assert entry.index("detect_backends(prefer_hermes=prefer_hermes)") > decided

    def test_a_bedrock_project_does_not_run_agy_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not its check, and not a signed-in `agy models` call to Google either.
        monkeypatch.delenv("REVIEW_USE_AGY", raising=False)
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.setenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", "1")
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")

        def ran(*a: object) -> object:
            raise AssertionError("agy ran for a project pinned to Bedrock")

        monkeypatch.setattr(backends, "agy_is_confined", ran)
        monkeypatch.setattr(agy, "agy_models", ran)
        assert backends.detect_backends(prefer_hermes=True)["agy"] is False
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.setattr(backends, "agy_is_confined", lambda: (True, "verified"))
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        assert backends.detect_backends(prefer_hermes=True)["agy"] is True

    @pytest.mark.usefixtures("on")
    def test_the_check_can_be_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", "1")
        assert self._detect(monkeypatch, None)["agy"] is True

    def test_both_homes_are_hidden_when_home_points_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # CI, sudo -E and wrapper scripts point HOME at a scratch directory; hiding only
        # that one left the account's ~/.ssh readable.
        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        monkeypatch.setenv("HOME", str(tmp_path))
        _, bwrap = agy.agy_sandbox_parts(tmp_path / "ws", tmp_path / "st")
        hidden = [Path(bwrap[i + 1]) for i, a in enumerate(bwrap) if a == "--tmpfs"]
        assert tmp_path in hidden
        if account != Path("/"):
            assert account in hidden
            # A home inside the other is mounted after it, on top, not hidden under it.
            if tmp_path.is_relative_to(account):
                assert hidden.index(account) < hidden.index(tmp_path)


class TestAgyRunner(_AgyCase):
    """The bash runner's agy launcher, which must not be the weaker path."""

    def test_the_bash_launcher_matches_the_python_one(self) -> None:
        """The doc is the program for full mode; it must not be the weaker path."""
        runner = runner_script()
        for flag in agy.AGY_SAFETY_ARGS:
            assert flag in runner
        assert "--print-timeout 0" in runner
        # One copy of the sandbox, the schema and the preamble: the runner calls the
        # loop's own code through runner_cli, not one-liners of its own.
        prepare = shell_function(runner, "prepare_agy")
        assert 'review_helper agy-sandbox "$AGY_WS" "$AGY_STATE"' in prepare
        assert "review_helper agy-schema" in prepare
        # The preamble written into the prompt file, which then goes in as one line.
        assert 'review_helper agy-preamble "$prompt_file"' in prepare
        assert 'review_helper agy-message "$prompt_file"' in runner
        assert runner.count("python3") == 1  # review_helper's own definition
        assert 'prepare_agy "$num" "$prompt_file" || return 1' in runner
        # Its output parsed as agy's only because agy ran it -- as the runner itself
        # recorded, not as a file a reviewer could rewrite says.
        assert "JOB_BACKEND[$num]=agy" in runner
        assert 'extract_json "$src" "$result" "$num" "${JOB_BACKEND[$num]:-}"' in runner
        assert 'echo agy > "$REVIEW_TMP/backend-$num.txt"' in runner  # for watching
        assert 'cat "$REVIEW_TMP/backend-' not in runner
        # Scratch listed in the runner's own shell, not in a file a reviewer could write.
        assert 'AGY_SCRATCH+=("$AGY_WS")' in prepare
        assert 'AGY_SCRATCH+=("$AGY_STATE")' in prepare
        assert "scratch-" not in runner
        assert "trap finish_round EXIT" in runner
        # A failed mktemp must not fall back to running agy in the repository.
        assert 'AGY_WS="$(mktemp -d)" && [ -n "$AGY_WS" ]' in prepare
        assert 'rm -rf "$AGY_WS" "$AGY_STATE"' in runner
        # Absolute, or the job's cd into the workspace sends its output there.
        assert 'REVIEW_TMP="$(cd "$REVIEW_TMP" && pwd -P)" || exit 1' in runner

    def test_a_bedrock_project_gets_agy_only_by_name_in_the_runner(self) -> None:
        runner = runner_script()
        gate = "{ ! $PREFER_HERMES || review_helper flag REVIEW_USE_AGY; }"
        assert f"elif ${{HAS_AGY:-false}} && {gate}" in runner
