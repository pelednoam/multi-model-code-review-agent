"""A reviewer's process: launched, waited for, stopped."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agy_case import AgyCase
from helpers import REPO_ROOT

from scripts.review_loop import reviewers


class TestReviewerProcesses(AgyCase):
    """A reviewer's process: launched, waited for, stopped -- once, not per subclass."""

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

    def test_a_reviewer_is_seen_to_finish_without_waitid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # macOS has no os.waitid before Python 3.13: every round raised AttributeError.
        monkeypatch.delattr(reviewers.os, "waitid")
        proc, out_f, err_f = reviewers._spawn(
            1, ["true"], "true", tmp_path, None, tmp_path
        )
        try:
            deadline = time.monotonic() + 10
            while reviewers._exit_status(proc) is None:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            assert reviewers._exit_status(proc) == 0
        finally:
            reviewers._stop(proc)
            out_f.close()
            err_f.close()

    def test_a_second_signal_waits_for_the_cleanup(self, tmp_path: Path) -> None:
        # A second Ctrl-C or SIGTERM raised in the middle of the cleanup the first had
        # started, and the reviewers not yet stopped kept running.
        done_file = tmp_path / "cleaned"
        script = (
            "import os, signal, sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from scripts.review_loop.proc import signals_deferred, signals_unwind\n"
            "with signals_unwind():\n"
            "    try:\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "        time.sleep(30)\n"
            "    finally:\n"
            "        with signals_deferred():\n"
            "            os.kill(os.getpid(), signal.SIGINT)\n"
            "            time.sleep(0.2)\n"
            "            open(sys.argv[2], 'w').write('done')\n"
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", script, str(REPO_ROOT), str(done_file)],
            capture_output=True, text=True, timeout=20,
        )  # fmt: skip
        assert done_file.read_text() == "done", done.stderr
        # The second signal still ends the run, once the cleanup is through.
        assert done.returncode != 0

    def test_a_reviewer_is_tracked_before_spawn_returns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Tracked only once _spawn had returned, a reviewer interrupted between that and
        # the caller's append was left running.
        tracked: list[reviewers.ReviewerProc] = []

        def register(entry: reviewers.ReviewerProc) -> None:
            tracked.append(entry)
            raise SystemExit(143)  # the interrupt, the moment it is handed over

        with pytest.raises(SystemExit):
            reviewers._spawn(
                1, ["sleep", "60"], "sleep", tmp_path, None, tmp_path, register
            )
        (entry,) = tracked
        # Registered, and still stopped by _spawn's own guard.
        assert entry[1].poll() is not None

    def test_a_planted_codex_answer_does_not_survive_publication(
        self, tmp_path: Path
    ) -> None:
        # codex wrote no answer of its own; one planted under its name stayed, and was
        # read as codex's.
        out, round_dir = tmp_path / "out", tmp_path / "round"
        out.mkdir()
        round_dir.mkdir()
        (round_dir / "codex-out-2.txt").write_text("planted")
        (out / "raw-2.txt").write_text("its stdout")
        reviewers._publish_outputs(out, round_dir)
        assert not (round_dir / "codex-out-2.txt").exists()
        assert (round_dir / "raw-2.txt").read_text() == "its stdout"

    def test_outputs_that_cannot_be_published_are_kept(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:  # fmt: skip
        # A failed copy ended with the private outputs -- the only copies of the
        # round's reviews -- removed as scratch.
        out = tmp_path / "out"
        out.mkdir()
        (out / "raw-1.txt").write_text("the only copy")

        def full_disk(out_dir: Path, round_dir: Path) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(reviewers, "_ROUND_SCRATCH", [out])
        monkeypatch.setattr(reviewers, "_publish_outputs", full_disk)
        reviewers._publish_or_keep(out, tmp_path / "round")
        reviewers._release_scratch()
        assert (out / "raw-1.txt").read_text() == "the only copy"
        assert f"kept in {out}" in capsys.readouterr().out

    def test_a_reviewer_without_a_prompt_on_stdin_gets_no_terminal(
        self, tmp_path: Path
    ) -> None:
        # hermes takes its prompt as a file, and inherited the loop's stdin -- usually
        # the operator's terminal.
        proc, out_f, err_f = reviewers._spawn(
            1, ["sh", "-c", "cat; echo done"], "sh", tmp_path, None, tmp_path
        )
        try:
            assert proc.wait(timeout=10) == 0
        finally:
            out_f.close()
            err_f.close()
        assert (tmp_path / "raw-1.txt").read_text() == "done\n"
