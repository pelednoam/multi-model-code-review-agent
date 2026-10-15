"""agy launched: its workspace, its scratch, what it is sent."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import sys
import time
from pathlib import Path

import pytest
from agy_case import AgyCase
from helpers import REPO_ROOT, SCHEMA_PATH, runner_script

from scripts.review_loop import agy, agy_sandbox, agy_sandbox_check, reviewers


class TestAgyLaunch(AgyCase):
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
        state = Path(wrap[wrap.index(str(agy_sandbox.agy_state_dir())) - 1])
        hidden = reviewers._under_review(None)  # the repository, and this checkout
        assert wrap == agy_sandbox.agy_wrapper(workspace, state, hidden)
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

    def test_reviewers_write_outside_the_round_until_all_are_stopped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Writing into the round directory while they ran, one reviewer with a file
        # tool could write another's output and answer for it.
        seen: dict[str, object] = {}
        self._launch_stub(monkeypatch, ["sh", "-c", "echo answer; echo noise >&2"])

        def await_and_look(
            procs: list[reviewers.ReviewerProc], out_dir: Path, n: int
        ) -> None:
            for _, proc, *_ in procs:
                proc.wait()
            seen["out_dir"] = out_dir
            seen["in_round"] = sorted(p.name for p in tmp_path.glob("raw-*.txt"))

        monkeypatch.setattr(reviewers, "_await_reviewers", await_and_look)
        answered = reviewers.launch_reviewers(tmp_path, "diff", "{}", "", self.BACKENDS)
        assert seen["out_dir"] != tmp_path and seen["in_round"] == []
        # Copied in once every reviewer had stopped; the private directory is gone.
        assert (tmp_path / "raw-1.txt").read_text() == "answer\n"
        assert (tmp_path / "stderr-1.txt").read_text() == "noise\n"
        assert not Path(str(seen["out_dir"])).exists()
        assert set(answered) == {1, 2, 3, 4}

    def test_scratch_that_cannot_be_removed_is_named_and_kept(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:  # fmt: skip
        # A failed removal was forgotten: the token copy stayed, and nothing said so.
        stuck = tmp_path / "stuck"
        stuck.mkdir()
        monkeypatch.setattr(reviewers, "_ROUND_SCRATCH", [stuck])
        monkeypatch.setattr(reviewers, "_remove_tree", lambda p: False)
        reviewers._release_scratch()
        assert reviewers._ROUND_SCRATCH == [stuck]
        assert f"could not remove {stuck}" in capsys.readouterr().out

    def test_a_read_only_scratch_tree_is_removed(self, tmp_path: Path) -> None:
        tree = tmp_path / "tree"
        (tree / "a" / "b").mkdir(parents=True)
        (tree / "a" / "b" / "token").write_text("t")
        for d in (tree / "a" / "b", tree / "a"):
            d.chmod(0o500)
        assert reviewers._remove_tree(tree) and not tree.exists()

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

        def launch(
            slot: int, lens: object, round_: object, register: object = None
        ) -> reviewers.ReviewerProc | None:
            if slot != 1:
                return None
            proc, out_f, err_f = reviewers._spawn(
                1,
                cmd,
                "sh",
                tmp_path,
                None,
                tmp_path,
                register,  # type: ignore[arg-type]
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
            "from scripts.review_loop import agy, agy_sandbox_check\n"
            "import tempfile; tempfile.tempdir = sys.argv[2]\n"
            "agy_sandbox_check._canary_parent = lambda: sys.argv[2]\n"
            # Nothing real runs (run is replaced), so bwrap need not be installed.
            "agy.shutil.which = lambda c: '/usr/bin/' + c\n"
            "def killed(*a, **k):\n"
            f"    os.kill(os.getpid(), signal.{sig})\n"
            "    time.sleep(30)\n"
            "agy.run = agy_sandbox_check.run = killed\n"
            f"{'agy_sandbox_check' if helper == 'agy_is_confined' else 'agy'}.{helper}()\n"
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
        monkeypatch.setattr(agy_sandbox, "agy_seed_state", seeded)
        monkeypatch.setattr(agy_sandbox_check, "run", nothing_runs)
        ok, reason = agy_sandbox_check.agy_is_confined()
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
        done, parts = self._run_prepare_agy(tmp_path, HTTPS_PROXY=proxy)
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
        assert len(parts["SCRATCH"]) == 2

    def test_an_unusable_schema_hands_the_slot_on(self, tmp_path: Path) -> None:
        # A root whose code is this checkout's but whose schema is not a schema.
        root = tmp_path / "root"
        (root / "docs").mkdir(parents=True)
        (root / "scripts").symlink_to(REPO_ROOT / "scripts")
        (root / "docs" / "ensemble_review_result_schema.json").write_text('{"type": 7}')
        done, parts = self._run_prepare_agy(tmp_path, root=root)
        assert done.returncode == 1
        assert "SKIPPED (agy result schema unusable)" in done.stdout
        assert parts["SCRATCH"] == []  # nothing made, nothing left behind

    def test_scratch_is_listed_the_moment_it_exists(self, tmp_path: Path) -> None:
        # A launch that fails after making its directories leaves them to the runner,
        # which removes whatever SCRATCH lists: no failure path cleans up alone.
        root = tmp_path / "root"
        (root / "docs").mkdir(parents=True)
        (root / "scripts").symlink_to(REPO_ROOT / "scripts")
        shutil.copy(SCHEMA_PATH, root / "docs" / "ensemble_review_result_schema.json")
        done, parts = self._run_prepare_agy(tmp_path, root=root, writable=False)
        assert done.returncode == 1, done.stdout
        assert "could not prepare agy's prompt" in done.stdout
        assert len(parts["SCRATCH"]) == 2

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
        assert reviewers._empty_workspace_outside_repo(target) is None
        assert (
            reviewers._empty_workspace_outside_repo(tmp_path / "elsewhere") is not None
        )

    def test_a_loop_started_from_root_still_gets_a_workspace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The cwd counted even with --repo given: from /, every workspace was inside it.
        monkeypatch.chdir("/")
        monkeypatch.setattr(reviewers.tempfile, "tempdir", str(tmp_path))
        assert reviewers._empty_workspace_outside_repo(tmp_path / "repo") is not None
        # Without --repo, the cwd is the repository, and is still kept out.
        monkeypatch.chdir(tmp_path)
        assert reviewers._empty_workspace_outside_repo() is None

    def test_the_check_passes_with_a_trailing_slash_home_and_a_spaced_tmpdir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """HOME with a trailing slash crashed detection; a space in TMPDIR failed it."""
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        spaced = tmp_path / "review scratch"
        spaced.mkdir()
        monkeypatch.setenv("HOME", str(Path.home()) + "/")
        monkeypatch.setattr(tempfile, "tempdir", str(spaced))
        self._quiet_host(monkeypatch)
        ok, reason = agy_sandbox_check.agy_is_confined()
        assert ok, reason

    def test_a_slot_whose_agy_cannot_be_set_up_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.setattr(
            reviewers, "_empty_workspace_outside_repo", lambda repo=None: None
        )
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

    def test_gemini_runs_outside_the_repository(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # From the repository under review, --skip-trust had it load the project's own
        # .gemini/settings.json, which the change being reviewed chooses.
        monkeypatch.setenv("REVIEW_USE_GEMINI", "1")
        monkeypatch.setattr(reviewers.tempfile, "tempdir", str(tmp_path))
        found = {**self.BACKENDS, "agy": False}
        repo = tmp_path / "repo"
        round_ = reviewers._Round("d", "{}", "", tmp_path, found, False, repo, tmp_path)
        picked = reviewers._pick_backend(3, round_)
        assert picked is not None and picked.backend == "gemini"
        assert picked.workspace is not None and picked.workspace.is_dir()
        assert not picked.workspace.is_relative_to(repo)
        # With nowhere outside the repository, the slot goes to its next backend.
        monkeypatch.setattr(reviewers.tempfile, "tempdir", str(repo))
        repo.mkdir()
        picked = reviewers._pick_backend(3, round_)
        assert picked is not None and picked.backend == "claude"
