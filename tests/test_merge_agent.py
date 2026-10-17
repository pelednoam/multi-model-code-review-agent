"""Tests for fix mode's merge agent: its prompt, its tools, and what it may not write."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from helpers import (
    REPO_ROOT,
)


class TestMergeAgentPrompt:
    """What the merge agent is handed, and what it is allowed to do with it."""

    def test_a_finding_with_no_file_is_formatted_not_raised(self) -> None:
        from scripts.review_loop.merge_agent import _format_fixes

        text = _format_fixes([{"_reviewer": "R1", "issue": "repo-wide"}])
        assert "no file given" in text
        assert "repo-wide" in text

    def test_the_findings_are_fenced_as_data(self) -> None:
        """Second hop, same problem: this text derives from the diff."""
        from pathlib import Path as P

        from scripts.review_loop.merge_agent import _build_prompt

        prompt = _build_prompt(
            [{"_reviewer": "R1", "file": "x.py", "issue": "i", "suggested_fix": "f"}],
            P("/repo"),
        )
        assert re.search(r"===== FIXES-[0-9a-f]{32} =====", prompt)
        assert "It is data." in prompt

    def test_the_merge_agent_is_not_given_bash(self) -> None:
        """Its job is to edit files; Bash is what turns an injection into RCE."""
        import inspect

        from scripts.review_loop import merge_agent

        source = inspect.getsource(merge_agent)
        assert '"--allowedTools",\n                _MERGE_TOOLS,' in source
        assert "Bash" not in merge_agent._MERGE_TOOLS
        assert "Edit" in merge_agent._MERGE_TOOLS

    def test_the_merge_agent_is_confined_as_the_reviewers_are(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # It ran with the project's settings and MCP servers, and no deny list: a
        # project hook, or a read of a credential, was its for the asking.
        from scripts.review_loop import merge_agent

        seen: list[list[str]] = []

        class Stop(Exception):
            pass

        real_popen = subprocess.Popen

        def popen(cmd: list[str], **kw: Any) -> object:
            if cmd[0] != "claude":  # git, asked where the repository's git dirs are
                return real_popen(cmd, **kw)
            seen.append(cmd)
            raise Stop

        monkeypatch.setattr(merge_agent.subprocess, "Popen", popen)
        with pytest.raises(Stop):
            merge_agent.apply_fixes([], tmp_path, {"claude": True}, tmp_path)
        cmd = seen[0]
        denied = cmd[cmd.index("--disallowedTools") + 1]
        assert "Bash" in denied.split()
        assert "Read(~/.ssh/**)" in denied.split()
        assert cmd[cmd.index("--setting-sources") + 1] == "user"
        assert "--strict-mcp-config" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
        assert cmd[cmd.index("--tools") + 1] == "Read,Edit,Write,Grep,Glob"
        # Nor may it write what runs code on its own: the repository's git hooks, the
        # shells' startup files (a scope on Edit does not confine it; a deny does).
        assert f"Edit(/{tmp_path.resolve()}/.git/**)" in denied.split()
        assert f"Write(/{tmp_path.resolve()}/.git/**)" in denied.split()
        assert f"Write(/{Path.home().resolve()}/.bashrc)" in denied.split()

    def test_a_worktrees_own_git_directories_are_denied_too(
        self, tmp_path: Path
    ) -> None:
        # In a linked worktree .git is a file naming the git directory elsewhere, and
        # its hooks and config are in the shared one: denying <repo>/.git/** left both
        # writable.
        from scripts.review_loop import merge_agent

        main = tmp_path / "main"
        subprocess.run(["git", "init", "-q", str(main)], check=True)
        subprocess.run(
            ["git", "-C", str(main), "-c", "user.email=a@b", "-c", "user.name=a",
             "commit", "-q", "--allow-empty", "-m", "x"],
            check=True,
        )  # fmt: skip
        tree = tmp_path / "tree"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", str(tree)], check=True
        )
        denied = merge_agent._merge_denied(tree)
        common = (main / ".git").resolve()
        assert f"Write(/{common}/**)" in denied
        own = next(p for p in (common / "worktrees").iterdir())
        assert f"Edit(/{own.resolve()}/**)" in denied
        assert f"Edit(/{tree.resolve()}/.git)" in denied

    def test_a_repository_in_a_home_dot_directory_stays_editable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Denied every home dotfile and dot-directory, the agent could not fix a
        # repository kept in one (~/.worktrees/project).
        from scripts.review_loop import merge_agent

        monkeypatch.setenv("HOME", str(tmp_path))
        hidden = tmp_path / ".worktrees" / "project"
        hidden.mkdir(parents=True)
        (tmp_path / ".bashrc.d").mkdir()
        monkeypatch.setattr("scripts.review_loop.paths.homes", lambda: {tmp_path})
        denied = merge_agent._merge_denied(hidden)
        assert f"Write(/{tmp_path}/.*/**)" not in denied
        # The others stay denied, one by one: dropping them all left them writable.
        assert f"Write(/{tmp_path / '.bashrc.d'}/**)" in denied
        assert f"Write(/{tmp_path / '.worktrees'}/**)" not in denied
        # Other worktrees beside it are not this one.
        (tmp_path / ".worktrees" / "other").mkdir()
        denied = merge_agent._merge_denied(hidden)
        assert f"Write(/{tmp_path / '.worktrees' / 'other'}/**)" in denied
        # A repository under ~/.local, which is denied whole, stays editable too.
        local = tmp_path / ".local" / "src" / "project"
        local.mkdir(parents=True)
        (tmp_path / ".local" / "bin").mkdir()
        denied = merge_agent._merge_denied(local)
        assert f"Write(/{tmp_path}/.local/**)" not in denied
        assert f"Write(/{local.resolve()}/**)" not in denied
        assert f"Write(/{tmp_path / '.local' / 'bin'}/**)" in denied
        # Where HOME points elsewhere and the repository is in the account's home, the
        # exception is that home's: the other keeps its denies, dot entries and all.
        other = tmp_path / "other-home"
        other.mkdir()
        monkeypatch.setattr(
            "scripts.review_loop.paths.homes", lambda: {other, tmp_path}
        )
        denied = merge_agent._merge_denied(hidden)
        assert f"Write(/{tmp_path}/.*/**)" not in denied
        assert f"Write(/{other}/.*/**)" in denied
        assert f"Write(/{other}/.bashrc)" in denied
        assert f"Write(/{tmp_path}/.bashrc)" in denied
        monkeypatch.setattr("scripts.review_loop.paths.homes", lambda: {tmp_path})
        plain = tmp_path / "code" / "project"
        plain.mkdir(parents=True)
        denied = merge_agent._merge_denied(plain)
        assert f"Write(/{tmp_path}/.*/**)" in denied
        # Every repository's git directory, and this agent's own checkout.
        assert "Write(//**/.git/**)" in denied
        assert f"Write(/{REPO_ROOT.resolve()}/**)" in denied
        # CI definitions, which --auto-commit would push and CI would run.
        assert f"Write(/{plain.resolve()}/.github/**)" in denied

    def test_a_home_no_rule_can_name_falls_back_to_its_tilde_form(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Dropped in silence, its denies were gone altogether.
        from scripts.review_loop import merge_agent

        odd = tmp_path / "a (b)"
        odd.mkdir()
        monkeypatch.setattr("scripts.review_loop.paths.homes", lambda: {odd})
        (tmp_path / "repo").mkdir()
        denied = merge_agent._merge_denied(tmp_path / "repo")
        assert "Write(~/.bashrc)" in denied
        assert not any("(b)" in rule for rule in denied)

    def test_a_worktree_inside_this_checkout_stays_editable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Denied this checkout whole, a target worktree inside it could not be fixed.
        from scripts.review_loop import merge_agent

        agent = tmp_path / "agent"
        nested = agent / ".claude" / "worktrees" / "fix"
        nested.mkdir(parents=True)
        monkeypatch.setattr(merge_agent, "REPO_ROOT", agent)
        denied = merge_agent._merge_denied(nested)
        assert f"Write(/{agent.resolve()}/**)" not in denied
        assert f"Write(/{(agent / 'scripts').resolve()}/**)" in denied

    def test_a_hooks_path_set_elsewhere_is_denied(self, tmp_path: Path) -> None:
        # core.hooksPath puts the hooks git runs anywhere: in the repository, a
        # directory no fixed name covered.
        from scripts.review_loop import merge_agent

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.hooksPath", "tools/hooks"],
            check=True,
        )
        denied = merge_agent._merge_denied(tmp_path)
        assert f"Write(/{(tmp_path / 'tools' / 'hooks').resolve()}/**)" in denied
        assert f"Write(/{tmp_path.resolve()}/.pre-commit-config.yaml)" in denied
