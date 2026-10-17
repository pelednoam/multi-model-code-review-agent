"""A reviewer's process: launched, waited for, stopped."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agy_case import AgyCase
from helpers import REPO_ROOT

from scripts.review_loop import reviewers
from scripts.review_loop.results import extract_results


class TestReviewerProcesses(AgyCase):
    """A reviewer's process: launched, waited for, stopped. On AgyCase for its helpers
    and fixtures; a class of its own so that these run once."""

    @pytest.mark.usefixtures("on")
    def test_what_appeared_since_startup_is_checked_before_each_launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The startup check describes the host as it was; a bus or socket that appears
        # during a half-hour loop was reachable from every later round.
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        self._launch(monkeypatch, tmp_path)  # installs the fake Popen
        monkeypatch.setattr(reviewers, "agy_reachable", lambda *a: "a session bus")
        round_ = reviewers._Round(
            "+DIFF", "{}", "ctx", tmp_path, self.BACKENDS, out_dir=tmp_path
        )
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

    @pytest.mark.skipif(not hasattr(os, "waitid"), reason="needs os.waitid")
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
        monkeypatch.delattr(reviewers.os, "waitid", raising=False)
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

    def test_without_waitid_a_finished_reviewers_helpers_are_killed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Reaped with no group kill, its helper went on writing while the outputs
        # were published.
        monkeypatch.delattr(reviewers.os, "waitid", raising=False)
        cmd = ["sh", "-c", "sleep 300 & echo $! > helper; exit 0"]
        proc, out_f, err_f = reviewers._spawn(1, cmd, "sh", tmp_path, None, tmp_path)
        try:
            deadline = time.monotonic() + 10
            while reviewers._exit_status(proc) is None:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            helper = int((tmp_path / "helper").read_text())
            time.sleep(0.2)
            stat = Path(f"/proc/{helper}/stat")
            assert not stat.exists() or stat.read_text().split()[2] == "Z"
        finally:
            reviewers._stop(proc)
            out_f.close()
            err_f.close()

    def test_without_waitid_the_group_is_killed_before_the_reap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Reaped first, the group's number was free for another session before the
        # kill reached it.
        monkeypatch.delattr(reviewers.os, "waitid", raising=False)
        proc, out_f, err_f = reviewers._spawn(
            1, ["true"], "true", tmp_path, None, tmp_path
        )
        killed: list[int | None] = []
        real_killpg = os.killpg

        def killpg(pgid: int, sig: int) -> None:
            killed.append(proc.returncode)
            real_killpg(pgid, sig)

        monkeypatch.setattr(reviewers.os, "killpg", killpg)
        try:
            deadline = time.monotonic() + 10
            while reviewers._exit_status(proc) is None:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            assert killed == [None]
        finally:
            reviewers._stop(proc)
            out_f.close()
            err_f.close()

    def test_a_zombie_is_told_from_a_live_process(self, tmp_path: Path) -> None:
        live = subprocess.Popen(["sleep", "30"])
        try:
            assert reviewers._is_zombie(live.pid) is False
        finally:
            live.kill()
        deadline = time.monotonic() + 10
        while not reviewers._is_zombie(live.pid):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        live.wait()

    def test_without_ps_a_zombie_is_not_claimed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr(reviewers, "_NO_PS", False)
        assert reviewers._is_zombie(os.getpid()) is None
        # Not looked for again, every poll of every reviewer.
        assert reviewers._NO_PS is True

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

    def test_a_round_whose_outputs_were_not_published_reads_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Kept privately after a failed copy, the outputs were still looked for in the
        # round directory -- where anything under their names was not this round's.
        self._launch_stub(monkeypatch, ["sh", "-c", "echo answer"])
        monkeypatch.setattr(reviewers, "_await_reviewers", lambda *a: None)
        monkeypatch.setattr(reviewers, "_publish_or_keep", lambda out, rd: False)
        assert (
            reviewers.launch_reviewers(tmp_path, "diff", "{}", "", self.BACKENDS) == {}
        )

    def test_a_planted_symlink_is_not_published(self, tmp_path: Path) -> None:
        # Copied by name, a symlink under a reviewer's output name published whatever
        # it pointed to.
        out, round_dir = tmp_path / "out", tmp_path / "round"
        out.mkdir()
        round_dir.mkdir()
        (tmp_path / "secret").write_text("not a review")
        (out / "raw-1.txt").symlink_to(tmp_path / "secret")
        reviewers._publish_outputs(out, round_dir)
        assert not (round_dir / "raw-1.txt").exists()

    def test_a_planted_directory_does_not_stop_the_publishing(
        self, tmp_path: Path
    ) -> None:
        # rm/unlink of a directory under an output's name failed, and the round read
        # no reviewer's output at all.
        out, round_dir = tmp_path / "out", tmp_path / "round"
        out.mkdir()
        (round_dir / "raw-1.txt").mkdir(parents=True)
        (round_dir / "raw-1.txt" / "x").write_text("planted")
        (out / "raw-1.txt").write_text("review")
        reviewers._publish_outputs(out, round_dir)
        assert (round_dir / "raw-1.txt").read_text() == "review"

    def test_a_directory_planted_at_a_result_costs_only_its_slot(
        self, tmp_path: Path
    ) -> None:
        # Its unlink raised IsADirectoryError, and the round ended for every reviewer.
        (tmp_path / "result-1.json" / "x").mkdir(parents=True)
        review = {
            "reviewer": "correctness", "model": "m", "findings": [],
            "overall_assessment": "a",
        }  # fmt: skip
        (tmp_path / "raw-2.txt").write_text(json.dumps(review))
        results = extract_results(tmp_path, {1: "claude", 2: "hermes"})
        assert results[1] is not None and not (tmp_path / "result-1.json").exists()

    @pytest.mark.skipif(sys.platform != "linux", reason="reads /proc/<pid>/status")
    def test_a_signal_while_a_reviewer_starts_still_stops_it(
        self, tmp_path: Path
    ) -> None:
        # Between starting the process and registering its stop, a SIGTERM left it
        # running. Held, it is raised once the stop is registered -- and the child is
        # not left with SIGTERM blocked, as holding it in the signal mask would.
        script = (
            "import os, signal, subprocess, sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from scripts.review_loop.proc import signals_held, signals_unwind\n"
            "with signals_unwind():\n"
            "    try:\n"
            "        with signals_held():\n"
            "            child = subprocess.Popen(['sleep', '60'])\n"
            "            os.kill(os.getpid(), signal.SIGTERM)\n"
            "            blocked = open(f'/proc/{child.pid}/status').read()\n"
            "    finally:\n"
            "        line = [l for l in blocked.splitlines() if l.startswith('SigBlk')]\n"
            "        print(line[0].split()[1], flush=True)\n"
            "        child.kill()\n"
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", script, str(REPO_ROOT)],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        assert done.returncode == 128 + signal.SIGTERM, done.stderr
        assert int(done.stdout.strip(), 16) & (1 << (signal.SIGTERM - 1)) == 0

    def test_a_symlink_put_in_place_of_scratch_is_removed_not_followed(
        self, tmp_path: Path
    ) -> None:
        # Followed, its target's permissions were changed and its contents walked.
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "keep").write_text("x")
        target.chmod(0o755)
        link = tmp_path / "scratch"
        link.symlink_to(target)
        assert reviewers._remove_tree(link)
        assert not link.is_symlink() and (target / "keep").exists()
        assert target.stat().st_mode & 0o777 == 0o755
