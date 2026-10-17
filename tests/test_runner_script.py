"""Tests for the full-mode runner, the run-reviewers.sh heredoc in docs/ensemble-review.md."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest
from helpers import (
    AGENT_DOC,
    GROUP_CHECKS,
    REPO_ROOT,
    liveness,
    runner_script,
    shell_function,
)

from scripts.review_loop import backends, claude_tools
from scripts.review_loop import reviewers as reviewers_mod
from scripts.review_loop.claude_tools import SECRET_READS, claude_denied


class TestRunnerScript:
    """The runner in docs/ensemble-review.md section 6.

    THE DOC IS THE PROGRAM for full mode: the orchestrating agent reads that markdown and executes
    the bash out of it, so a regression there is a regression in the product with nothing to catch
    it. These tests extract the runner heredoc and check the properties that failed in the field.

    The failure they exist for: launch-with-& followed by `wait` used to sit in one foreground Bash
    tool call. That call is killed at its 120 second timeout while the reviewers, wrapped in
    `timeout 600`, survive detached. The orchestrator then had no results, no `wait` to return, and
    nothing that would ever wake it - it went idle and the round only moved when a human asked what
    had happened. It cost two rounds on a downstream project before the cause was found.
    """

    def test_the_runner_is_read_whole_before_it_runs(self, tmp_path: Path) -> None:
        # bash reads a script as it runs it: a reviewer that rewrote the file while it
        # ran had its commands run (measured). One group, closed by an exit, is read
        # whole first.
        runner = runner_script()
        code = [line for line in runner.splitlines() if not line.startswith("#")]
        assert code[0] == "{" and code[-2:] == ["exit 0", "}"]
        script = tmp_path / "s.sh"
        script.write_text(
            "{\nsleep 1\necho ran\nexit 0\n}\n" + "#" * 64 + "\n", encoding="utf-8"
        )
        proc = subprocess.Popen(
            ["bash", str(script)], stdout=subprocess.PIPE, text=True
        )
        time.sleep(0.3)
        with script.open("r+", encoding="utf-8") as f:
            f.write("{\nsleep 1\necho bad\nexit 0\n}\n")
        out, _ = proc.communicate(timeout=10)
        assert out == "ran\n"

    @pytest.mark.parametrize(
        ("files", "stops"),
        [
            ({"done-1": "0", "done-2": "0", "done-3": "0", "done-4": "0"}, False),
            ({"round.log": "x\nROUND COMPLETE\n"}, True),
            ({"runner-exit": "143"}, True),
        ],
    )
    def test_the_poll_waits_for_the_runner_not_the_reviewers(
        self, tmp_path: Path, files: dict[str, str], stops: bool
    ) -> None:
        # Four done-markers come before the outputs are published and extracted: a
        # poll that stopped on them read a half-made round.
        doc = AGENT_DOC.read_text()
        start = doc.index("deadline=$((SECONDS + 570))")
        poll = doc[start : doc.index("```", start)]
        poll = poll.replace("SECONDS + 570", "SECONDS + 2")
        poll = poll.replace("sleep 10", "sleep 0.2")
        for name, text in files.items():
            (tmp_path / name).write_text(text)
        started = time.monotonic()
        subprocess.run(
            ["bash", "-c", poll], env={**os.environ, "REVIEW_TMP": str(tmp_path)},
            capture_output=True, timeout=30,
        )  # fmt: skip
        assert (time.monotonic() - started < 1) == stops

    def test_launchers_read_private_copies_of_the_prompts(self) -> None:
        # From $REVIEW_TMP, a hermes reviewer already running with a file tool could
        # rewrite a later slot's prompt before it was read.
        runner = runner_script()
        copied = runner.index('cp -- "$REVIEW_TMP"/prompt-[1-4].txt "$PROMPT_DIR/"')
        launches = runner[runner.index("# R1: Security") :]
        assert copied < runner.index("# R1: Security")
        assert '"$REVIEW_TMP/prompt-' not in launches
        assert launches.count('"$PROMPT_DIR/prompt-') == 15

    def test_the_runners_private_directories_are_named_for_the_deny_rules(self) -> None:
        # Plain mktemp names (tmp.XXXX) were outside claude's mmcra-* deny rules.
        runner = runner_script()
        assert "mmcra-out-XXXXXXXX" in runner
        assert "make_scratch PROMPT_DIR mmcra-prompt-" in runner
        assert "//**/mmcra-out-*/**" in SECRET_READS
        assert "//**/mmcra-prompt-*/**" in SECRET_READS

    def test_cdpath_is_cleared_before_any_cd(self) -> None:
        # Set, it made "$(cd DIR && pwd -P)" capture cd's own line in the path.
        runner = runner_script()
        assert runner.index("unset CDPATH") < runner.index('(cd "')

    def test_runner_is_valid_bash(self, tmp_path: Path) -> None:
        script = tmp_path / "run-reviewers.sh"
        script.write_text(runner_script() + "\n")
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"runner does not parse: {result.stderr}"

    def test_launch_and_wait_live_in_the_same_script(self) -> None:
        # Both halves in one heredoc is the whole fix: `wait` and `kill -0` only work on children
        # of the shell that started them, and shell state does not survive between tool calls.
        runner = runner_script()
        assert "launch_cli" in runner and "launch_hermes" in runner
        assert "ROUND_DEADLINE" in runner
        assert "ROUND COMPLETE" in runner

    def test_the_doc_tells_the_agent_to_run_it_in_the_background(self) -> None:
        doc = AGENT_DOC.read_text()
        assert "run_in_background: true" in doc
        # And says why, so the next person to touch it does not "simplify" it back.
        assert "120 second timeout" in doc

    def test_waiting_is_bounded(self) -> None:
        # A bare `wait` has no deadline of its own, so one wedged reviewer holds the round open
        # forever and the other three results are never reported.
        runner = runner_script()
        assert "\nwait\n" not in runner, (
            "bare `wait` is back: one wedged reviewer hangs the round"
        )
        assert "WATCHDOG" in runner

    def test_every_reviewer_records_a_pid_and_an_exit_status(self) -> None:
        runner = runner_script()
        # Every launcher (hermes, claude, codex, gemini, agy) recording both is what makes
        # progress observable from another shell.
        assert runner.count('echo $! > "$REVIEW_TMP/pid-$num"') == 5
        # agy's records its status after cleanup, so it holds it in $rc first. In the
        # runner's private directory, and in $REVIEW_TMP for watching (mark_done).
        statuses = runner.count('mark_done "$num" $?; } &') + runner.count(
            'mark_done "$num" "$rc"; } &'
        )
        assert statuses == 5
        mark = shell_function(runner, "mark_done")
        assert '"$OUT_DIR/done-$1"' in mark and '"$REVIEW_TMP/done-$1"' in mark
        # The status must be written by the subshell that ran the reviewer. A separate waiter
        # cannot: `wait` refuses a pid that is not its own child, so it would record a bogus
        # status immediately while the reviewer was still running.
        # (The watchdog's `sleep 5 & wait $!` waits on its own sleep, not on a reviewer.)
        assert "wait $!" not in runner.replace("sleep 5 & wait $!", "")

    def test_reviewers_are_killable_as_a_tree(self) -> None:
        runner = runner_script()
        # `set -m` gives each background job its own process group; without it a reviewer CLI's
        # helper processes survive the round, still burning provider quota and still writing into
        # $REVIEW_TMP after the report is written.
        assert "\nset -m" in runner
        assert runner.count("kill_jobs 1 2 3 4") == 2  # interrupt, sweep
        assert runner.count('kill_jobs "$num"') == 1  # watchdog
        # SIGKILL escalation, because a CLI that traps SIGTERM otherwise outlives the round.
        # One per launcher, each staying in its job's process group (--foreground).
        assert runner.count('timeout --foreground -k 10 "$REVIEWER_TIMEOUT"') == 5
        assert "timeout -k" not in runner

    def test_what_is_killed_is_held_in_memory_not_read_from_a_file(self) -> None:
        # A reviewer can write in $REVIEW_TMP: a planted pid-N of 1 had the runner signal
        # every process the user owns.
        runner = runner_script()
        assert runner.count("JOB_PID[$num]=$!") == 5
        assert 'cat "$REVIEW_TMP/pid-' not in runner
        # pgrep only to ask whether a group is still this run's (live_group), and once
        # before that, whether it can.
        assert runner.count("pgrep -s") == 2
        assert 'pgrep -s 0 -g "$1"' in liveness(runner)
        assert "if pgrep -s 0 -g 0 >/dev/null 2>&1 || [ $? -eq 1 ]; then" in runner

    @pytest.mark.parametrize("exits", [False, True], ids=["running", "exited"])
    @pytest.mark.parametrize("check", GROUP_CHECKS)
    def test_kill_jobs_reaches_everything_a_reviewer_started(
        self, tmp_path: Path, exits: bool, check: str
    ) -> None:
        # Plain timeout put itself in a process group of its own: a kill of the job's group
        # left the reviewer under it running, and a helper the reviewer left behind was out
        # of reach once timeout had exited. And with no pgrep to ask, every group read as
        # gone, and nothing was killed at all.
        runner = runner_script()
        kill_jobs = shell_function(runner, "kill_jobs") + liveness(runner)
        then = "exit 0" if exits else "wait"
        script = tmp_path / "t.sh"
        script.write_text(
            f"set -m\nJOB_PID=()\nGROUP_CHECK={check}\n"
            + kill_jobs
            + "{ timeout --foreground -k 10 600 sh -c 'sleep 300 & echo $! > helper; "
            + then
            + "'; } &\n"
            "JOB_PID[1]=$!\n"
            "sleep 1\n"
            "kill_jobs 1\n"
            'kill -0 "$(cat helper)" 2>/dev/null && echo SURVIVED || echo GONE\n'
        )
        done = subprocess.run(
            ["bash", str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert done.stdout.strip() == "GONE", done.stdout + done.stderr

    def test_a_bedrock_pin_read_any_other_way_still_keeps_agy_out(self) -> None:
        # Only the string "true" pinned: "1" or "yes" let agy run for such a project.
        from scripts.review_loop import runner_cli

        seen: dict[str, bool] = {}

        def detect(verify_codex: bool, prefer_hermes: bool) -> dict[str, bool]:
            seen["pinned"] = prefer_hermes
            return {"agy": False}

        with mock.patch.object(runner_cli, "detect_backends", detect):
            for value in ("true", "1", "yes", "TRUE"):
                runner_cli._agy_detect(value)
                assert seen["pinned"], value
            runner_cli._agy_detect("false")
            assert not seen["pinned"]

    def test_agys_scratch_is_removed_or_named(self, tmp_path: Path) -> None:
        # The rm's failure said nothing, and a copy of agy's token could stay.
        runner = runner_script()
        locked = tmp_path / "locked"
        (locked / "inner").mkdir(parents=True)
        (locked / "inner").chmod(0o500)
        script = tmp_path / "t.sh"
        script.write_text(
            f"SCRATCH=({shlex.quote(str(locked))})\n"
            + shell_function(runner, "remove_scratch")
            + "remove_scratch\n"
            + 'rm() { :; }\nmkdir -p "${SCRATCH[0]}"\nremove_scratch\n'
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        # Made writable and removed; then, with rm made to fail, named.
        assert done.stdout.count("WARNING: could not remove") == 1, done.stdout
        assert "  remove_scratch\n" in shell_function(runner, "finish_round")

    @pytest.mark.parametrize("check", GROUP_CHECKS)
    def test_the_cleanup_kills_from_inside_its_trap(
        self, tmp_path: Path, check: str
    ) -> None:
        # live_group ended in a bare `return`, which in a trap returns the status from
        # before it -- the `exit 143` -- so an interrupted runner killed nothing.
        runner = runner_script()
        script = tmp_path / "t.sh"
        script.write_text(
            f"set -m\nJOB_PID=()\nGROUP_CHECK={check}\n"
            + shell_function(runner, "kill_jobs")
            + liveness(runner)
            + "{ sleep 300; } &\nJOB_PID[1]=$!\necho $! > job\nsleep 0.3\n"
            + "trap 'kill_jobs 1' EXIT\ntrap 'exit 143' TERM\nkill -TERM $$\nsleep 5\n"
        )
        subprocess.run(["bash", str(script)], cwd=tmp_path, timeout=30)
        job = int((tmp_path / "job").read_text())
        time.sleep(0.3)
        alive = subprocess.run(["kill", "-0", str(job)], capture_output=True)
        if alive.returncode == 0:
            subprocess.run(["kill", "-KILL", "--", f"-{job}"])
        assert alive.returncode != 0, "the job outlived the runner's cleanup"

    @pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
    def test_a_group_whose_name_holds_a_newline_is_found(self, tmp_path: Path) -> None:
        # Read a line at a time, its stat ended inside the name, and the group was
        # taken for gone: the cleanup left it running.
        runner = runner_script()
        named = (
            "import ctypes, time; "
            "ctypes.CDLL(None).prctl(15, b'help\\nname', 0, 0, 0); time.sleep(30)"
        )
        script = tmp_path / "t.sh"
        script.write_text(
            "set -m\n"
            + shell_function(runner, "session_groups")
            + f"exec {sys.executable} -c {shlex.quote(named)} &\nchild=$!\nsleep 0.5\n"
            + 'case "$(session_groups)" in *" $child "*) echo found ;; *) echo lost ;; esac\n'
            + "kill $child\n"
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert done.stdout.strip() == "found", done.stdout + done.stderr

    def test_a_second_signal_does_not_cut_the_cleanup_short(
        self, tmp_path: Path
    ) -> None:
        # The TERM trap's `exit` ran again inside the EXIT trap, ending the cleanup with
        # reviewers still running.
        runner = runner_script()
        script = tmp_path / "t.sh"
        script.write_text(
            "SCRATCH=()\n"
            # Stands in for the real sweep, with a second TERM arriving halfway.
            + "kill_jobs() { kill -TERM $$; sleep 0.3; echo CLEANED; }\n"
            + shell_function(runner, "finish_round")
            + "trap finish_round EXIT\ntrap 'exit 143' TERM\n"
            + "kill -TERM $$\nsleep 5\n"
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert done.stdout.strip() == "CLEANED", done.stdout + done.stderr
        assert done.returncode == 143

    def test_the_runner_says_it_needs_bash_4_4(self) -> None:
        # `mapfile -d` is 4.4's, and before it `set -u` failed on an empty array.
        runner = runner_script()
        assert "mapfile -d" in runner
        assert "the runner needs bash 4.4 or later" in runner

    def test_providers_are_counted_from_what_was_launched(self) -> None:
        # Predicted from what was installed, the count took in codex and Google for a
        # project pinned to Bedrock, where Hermes takes slots 2 and 3.
        runner = runner_script()
        # One per launcher, agy's by the family of the model it runs (three cases).
        assert runner.count("JOB_PROVIDER[$num]=") == 7
        assert "claude-*) JOB_PROVIDER[$num]=anthropic" in runner
        count = runner.index("N_PROVIDERS=$(printf '%s\\n' \"${JOB_PROVIDER[@]}\"")
        assert runner.index("# WATCHDOG") > count > runner.index("# R4: Spec-contract")
        assert "$HAS_CODEX && N_PROVIDERS" not in runner

    def test_codexs_answer_is_checked_like_the_others(self) -> None:
        # Written straight to result-N.json, it skipped the validation every other
        # reviewer's output gets -- which the loop gives it.
        runner = runner_script()
        assert '-o "$OUT_DIR/codex-out-$num.txt"' in runner
        assert '[ -f "$result" ] && continue' not in runner
        assert 'JOB_OUT[$num]="$REVIEW_TMP/codex-out-$num.txt"' in runner

    def test_reviewers_write_where_no_other_reviewer_writes(
        self, tmp_path: Path
    ) -> None:
        # Writing into $REVIEW_TMP, a reviewer with a file tool could overwrite another
        # slot's output while both ran. They write to OUT_DIR, published once all stop.
        runner = runner_script()
        start, end = runner.index("launch_hermes() {"), runner.index("# Pre-check")
        launchers = runner[start:end]
        assert '>"$REVIEW_TMP/' not in launchers and '-o "$REVIEW_TMP/' not in launchers
        sweep = runner[runner.index("# SWEEP.") : runner.index("# Post-process")]
        assert sweep.index("kill_jobs 1 2 3 4") < sweep.index("publish_outputs")
        script = tmp_path / "t.sh"
        tmp, out = tmp_path / "review", tmp_path / "out"
        tmp.mkdir()
        out.mkdir()
        (tmp / "codex-out-2.txt").write_text("planted")
        (tmp / "stdout-1.txt").write_text("planted")
        (out / "stdout-1.txt").write_text("real")
        script.write_text(
            f"REVIEW_TMP={shlex.quote(str(tmp))}\nOUT_DIR={shlex.quote(str(out))}\n"
            + shell_function(runner, "publish_outputs")
            + "publish_outputs\n"
        )
        subprocess.run(["bash", str(script)], check=True, timeout=30)
        assert (tmp / "stdout-1.txt").read_text() == "real"
        assert not (tmp / "codex-out-2.txt").exists()

    @pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid")
    def test_the_runner_has_a_session_of_its_own(self, tmp_path: Path) -> None:
        # In the caller's session, a group number reused by another of its processes
        # passed live_group, and was signalled.
        runner = runner_script()
        start = runner.index('if [ -z "${MMCRA_OWN_SESSION:-}" ]')
        wrapper = runner[start : runner.index("\nfi\n", start) + 4]
        script = tmp_path / "t.sh"
        script.write_text(
            wrapper
            # The trap before the line the test waits for: after it, a TERM sent on
            # reading the line could come first, and end the shell with no word said.
            + "trap 'echo got-TERM; exit 143' TERM\n"
            + 'echo "leader=$([ "$(ps -o sid= -p $$ | tr -d " ")" = "$$" ] && echo yes)"\n'
            + "sleep 20 >/dev/null & wait $!\n"
        )
        proc = subprocess.Popen(
            ["bash", str(script)], stdout=subprocess.PIPE, text=True
        )
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "leader=yes"
        proc.terminate()  # to the outer runner, which forwards it
        rest, _ = proc.communicate(timeout=20)
        assert "got-TERM" in rest and proc.returncode == 143

    def test_the_runner_runs_a_copy_of_its_helpers(self) -> None:
        # A reviewer with a file tool can write the checkout, whose code the runner
        # ran after the reviewers had started.
        runner = runner_script()
        assert runner.index("HELPER_ROOT=") < runner.index("launch_cli 1")
        assert 'rm -rf -- "$HELPER_ROOT"' in shell_function(runner, "finish_round")

    def test_the_private_directory_is_absolute(self) -> None:
        # The agy job changes into its workspace before its redirections are opened: a
        # relative TMPDIR put its output, and its done marker, somewhere else.
        runner = runner_script()
        assert 'OUT_DIR="$(cd "$OUT_DIR" && pwd -P)"' in runner

    def test_outputs_that_could_not_be_published_are_kept_and_not_read(
        self, tmp_path: Path
    ) -> None:
        # A failed copy, and the cleanup removed the only copies; the extraction read
        # whatever was in $REVIEW_TMP under their names.
        runner = runner_script()
        tmp, out = tmp_path / "review", tmp_path / "out"
        tmp.mkdir()
        out.mkdir()
        (out / "stdout-1.txt").write_text("the only copy")
        start = runner.index("PUBLISHED=false")
        flags = runner[start : runner.index("publish_outputs() {")]
        script = tmp_path / "t.sh"
        script.write_text(
            f"REVIEW_TMP={shlex.quote(str(tmp))}\nOUT_DIR={shlex.quote(str(out))}\n"
            "SCRATCH=()\nkill_jobs() { :; }\n"
            + shell_function(runner, "remove_scratch")
            + flags
            + shell_function(runner, "publish_outputs")
            + shell_function(runner, "finish_round")
            + "cp() { return 1; }\n"
            + "publish_outputs || echo FAILED\nfinish_round\n"
            + 'echo "published=$PUBLISHED"\n'
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert "FAILED" in done.stdout and "published=false" in done.stdout
        assert f"kept in {out}" in done.stdout
        assert (out / "stdout-1.txt").read_text() == "the only copy"
        post = runner[runner.index("# Post-process") :]
        assert '$PUBLISHED && [ -n "$src" ]' in post

    def test_a_slot_is_read_only_from_its_own_clis_file(self, tmp_path: Path) -> None:
        # Every slot was read from codex-out-N.txt, then claude-raw-N.json, then
        # stdout, whichever existed first, and a result-N.json already there was kept:
        # a reviewer with a file tool could stand in for another slot.
        runner = runner_script()
        post = runner[runner.index("# Post-process") :]
        assert 'rm -rf -- "$result"' in post
        assert 'src="${JOB_OUT[$num]:-}"' in post
        assert "for src in" not in post
        assert runner.count("JOB_OUT[$num]=") == 5

    @pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
    def test_without_pgrep_groups_are_asked_of_proc(self, tmp_path: Path) -> None:
        # The check is decided once, before anything is launched.
        runner = runner_script()
        start = runner.index("if pgrep -s 0 -g 0")
        decide = runner[start : runner.index("\nfi\n", start) + 4]
        script = tmp_path / "t.sh"
        script.write_text("pgrep() { return 127; }\n" + decide + "echo $GROUP_CHECK\n")
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert done.stdout.strip() == "proc", done.stderr

    @pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid")
    @pytest.mark.parametrize("check", GROUP_CHECKS)
    def test_a_group_that_is_no_longer_this_runs_is_left_alone(
        self, tmp_path: Path, check: str
    ) -> None:
        # bash reaps a finished job at once; once its group empties, the number can go
        # to someone else's new session -- a shell, an ssh login -- and was killed. The
        # first fallback for a missing pgrep asked only whether the number existed.
        runner = runner_script()
        script = tmp_path / "t.sh"
        script.write_text(
            f"GROUP_CHECK={check}\n"
            + shell_function(runner, "kill_jobs")
            + liveness(runner)
            # Another session's group leader standing in for a reused number.
            + "setsid sleep 30 & other=$!\nsleep 0.3\n"
            + "JOB_PID=([1]=$other)\nkill_jobs 1\n"
            + 'kill -0 "$other" 2>/dev/null && echo ALIVE || echo KILLED\nkill "$other"\n'
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert done.stdout.strip() == "ALIVE", done.stdout + done.stderr

    def test_a_bedrock_project_gets_neither_google_cli_by_default(self) -> None:
        # The gemini branch had no gate: with Hermes unavailable, a project pinned to
        # Bedrock sent slot 3 to Google through it.
        runner = runner_script()
        # Nor any other: gemini is opt-in in both modes now.
        gated = "elif $HAS_GEMINI && review_helper flag REVIEW_USE_GEMINI \\\n"
        assert gated in runner
        # And the switches reach the runner, a separate script that inherits only exports.
        doc = AGENT_DOC.read_text()
        export = doc.split("export REVIEW_TMP", 1)[1].split('\n"', 1)[0]
        assert "REVIEW_USE_AGY" in export and "REVIEW_USE_GEMINI" in export
        # agy is not even detected for such a project.
        detection = doc[doc.index("PREFER_HERMES=false") : doc.index("HAS_AGY=true")]
        assert 'review_helper agy-detect "$PREFER_HERMES"' in detection

    @pytest.mark.parametrize(
        ("tools", "denied", "scoped"),
        [
            # The default list: everything that writes, runs a command, or goes online.
            ("Read Grep Glob", "Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings", []),
            # Tools given on purpose come off the list, and only those.
            ("Read Grep Glob Bash Edit Write", "MultiEdit NotebookEdit WebFetch WebSearch ReportFindings", []),
            ("Read,Bash", "Edit Write MultiEdit NotebookEdit WebFetch WebSearch ReportFindings", []),
            ("Read Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings", "", []),
            # A scoped grant names the tool too -- and only the tool, not its scope --
            # and is said to be granted whole.
            ("Read Grep Bash(git log:*)", "Edit Write MultiEdit NotebookEdit WebFetch WebSearch ReportFindings", ["Bash"]),
            (
                "Read Bash(git log --grep Edit --all:*)",
                "Edit Write MultiEdit NotebookEdit WebFetch WebSearch ReportFindings", ["Bash"],
            ),
            # Unbalanced: nothing is granted.
            ("Read Edit Bash(git", "Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings", []),
            ("Read Edit) Bash", "Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings", []),
        ],
    )  # fmt: skip
    def test_an_override_lifts_only_the_tools_it_names(
        self, tools: str, denied: str, scoped: list[str]
    ) -> None:
        # Any REVIEWER_TOOLS_N used to drop the deny list whole, read-only lists included.
        assert claude_denied(tools) == (denied.split(), scoped)

    def test_the_runner_denies_what_the_loop_denies(self, tmp_path: Path) -> None:
        # Two parsers, one in bash and one implied by the loop's fixed list, drifted:
        # the runner's list had no network tools. Now both are claude_denied's.
        runner = runner_script()
        assert 'local tools="${!tools_var:-Read Grep Glob}"' in runner
        assert '--allowedTools "$tools" "${CLAUDE_DENY[@]}" \\\n' in runner
        flags = "--strict-mcp-config --setting-sources user --permission-mode dontAsk"
        assert flags + " \\\n" in runner
        script = tmp_path / "t.sh"
        script.write_text(
            f"HELPER_ROOT={shlex.quote(str(REPO_ROOT))}\n"
            f"OUT_DIR={shlex.quote(str(tmp_path))}\n"
            + shell_function(runner, "review_helper")
            + shell_function(runner, "claude_deny")
            + 'claude_deny 2 "$1"\necho "${#CLAUDE_DENY[@]}|${CLAUDE_DENY[*]}"\n'
        )
        # The credential reads are denied whatever is granted.
        reads = " ".join(f"Read({path})" for path in claude_tools._secret_reads())
        for tools, expected in [
            ("Read Grep Glob", "2|--disallowedTools Edit Write MultiEdit NotebookEdit"
             f" Bash WebFetch WebSearch ReportFindings {reads}"),
            ("Read Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings",
             f"2|--disallowedTools {reads}"),
        ]:  # fmt: skip
            done = subprocess.run(
                ["bash", str(script), tools], capture_output=True, text=True, timeout=30
            )
            assert done.stdout.strip() == expected, done.stderr
        done = subprocess.run(
            ["bash", str(script), "Read Bash(git log:*)"],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        # Said, and not taken for part of the deny list: it was, once.
        note, denied = done.stdout.strip().split("\n")
        assert note.startswith("R2: note: Bash is granted whole"), done.stdout
        assert denied == (
            "2|--disallowedTools Edit Write MultiEdit NotebookEdit WebFetch WebSearch"
            f" ReportFindings {reads}"
        )

    def test_a_claude_reviewer_cannot_read_credentials(self) -> None:
        # Its reads are not confined to the repository: measured, it read ~/.bashrc.
        cmd = reviewers_mod._claude_cmd(1)
        denied = cmd[cmd.index("--disallowedTools") + 1].split()
        assert "Read(~/.ssh/**)" in denied and "Read(~/.aws/**)" in denied
        assert claude_denied("Read Grep Glob")[0] == [
            d for d in denied if not d.startswith("Read(")
        ]

    def test_the_audit_does_not_quote_a_credential(self, tmp_path: Path) -> None:
        # Its quoted lines come from whole changed files, which the diff's scrubbing
        # never saw, and the audit goes into every reviewer's prompt.
        from scripts.preflight import patterns

        # Built from parts: written out, this line would be redacted from the diff.
        secret = "pass" + "word = " + repr("hunter2" * 3) + "  # except Exception"
        assert patterns._quoted(secret) == patterns.REDACTED
        assert patterns._quoted("    except Exception:  ") == "except Exception:"

    def test_a_codex_that_hangs_drops_its_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With no deadline, the check held up every reviewer: it runs before any.
        def hangs() -> tuple[bool, str]:
            raise subprocess.TimeoutExpired(["codex", "sandbox"], 30)

        monkeypatch.setattr(backends, "codex_is_confined", hangs)
        monkeypatch.delenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", raising=False)
        ok, reason = backends.codex_usable()
        assert not ok and "did not finish" in reason

    def test_an_inherited_out_dir_is_not_removed(self) -> None:
        # Cargo and Android builds export OUT_DIR; the runner's early cleanup removed
        # whatever it named if the runner stopped before setting its own.
        runner = runner_script()
        early_trap = runner.index("trap 'rm -rf -- \"${HELPER_ROOT")
        assert runner.index('OUT_DIR=""') < early_trap

    def test_the_loops_claude_reviewer_has_no_mcp_servers(self) -> None:
        cmd = reviewers_mod._claude_cmd(1)
        assert "--strict-mcp-config" in cmd
        # Nor the project's settings: measured, a project SessionStart hook ran.
        assert cmd[cmd.index("--setting-sources") + 1] == "user"
        # Nor the mode the user's settings choose: bypassPermissions runs anything.
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
        denied = cmd[cmd.index("--disallowedTools") + 1].split()
        assert {"Bash", "Write", "WebFetch", "WebSearch"} <= set(denied)

    def test_no_reviewer_reads_the_branchs_own_instructions(self) -> None:
        # The repository is the reviewers' working directory, and its AGENTS.md (codex,
        # hermes) or CLAUDE.md (claude) would be read as instructions, outside the diff's
        # fence. Measured: codex followed an AGENTS.md until project_doc_max_bytes=0;
        # claude ignored a CLAUDE.md under --setting-sources user.
        from scripts.review_loop import reviewers

        assert "project_doc_max_bytes=0" in reviewers._codex_extra_args()
        assert "--ignore-rules" in reviewers._hermes_cmd(Path("p"), "m")
        claude = reviewers._claude_cmd(1)
        assert claude[claude.index("--setting-sources") + 1] == "user"
        runner = runner_script()
        assert "--provider bedrock -t file --yolo --ignore-rules" in runner
        assert "-c project_doc_max_bytes=0" in runner

    def test_codex_is_pinned_and_checked_as_in_the_loop(self) -> None:
        # The doc said codex was sandboxed in full mode; the runner passed no flag.
        runner = runner_script()
        codex = runner[runner.index("    codex)") : runner.index("    gemini)")]
        assert "-s read-only -c 'approvals_reviewer=\"user\"'" in codex
        assert "-c project_doc_max_bytes=0" in codex
        doc = AGENT_DOC.read_text()
        detection = doc[doc.index("HAS_CODEX=false") : doc.index("HAS_AGY=false")]
        assert "review_helper codex-check || HAS_CODEX=false" in detection

    def test_a_failed_slot_still_produces_a_result_with_a_true_reason(self) -> None:
        runner = runner_script()
        # Every slot must yield a result-N.json so the report can be honest about what is missing,
        # and the reason must distinguish the cases a reader would act on differently.
        assert "124|137" in runner, (
            "a reviewer killed by its own timeout is reported as a parse error"
        )
        assert "never launched" in runner
        assert "killed by the watchdog" in runner
