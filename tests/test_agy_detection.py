"""agy detected, and the full-mode runner's agy path."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest
from agy_case import AgyCase
from helpers import REPO_ROOT, runner_script, shell_function

from scripts.review_loop import agy, agy_sandbox, backends


class TestAgyDetection(AgyCase):
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
        decided = entry.index("prefer_hermes = (")
        assert entry.index(
            "detect_backends(prefer_hermes=prefer_hermes, repo=repo)"
        ) > (decided)

    def test_the_bedrock_pin_is_read_from_the_base_too(self) -> None:
        # Read from the tree under review only, a branch that deleted .use-hermes sent
        # its own code to OpenAI and Google by default.
        entry = (REPO_ROOT / "scripts" / "review_until_converged.py").read_text()
        assert '"git", "cat-file", "-e", f"{ref}:.use-hermes"' in entry
        # Every base the diff may be taken against, its HEAD~1 fallback among them.
        assert 'for ref in (args.base, merge_base, "HEAD~1", "HEAD")' in entry
        assert "args.prefer_hermes or pinned_in_base or" in entry

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
        # Asked for by name, still not where Hermes answers its slot anyway.
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        assert backends.detect_backends(prefer_hermes=True)["agy"] is False
        # With no Hermes, it is the slot's fallback.
        monkeypatch.setattr(
            backends.shutil,
            "which",
            lambda c: None if c == "hermes" else f"/usr/bin/{c}",
        )
        monkeypatch.setattr(backends, "agy_is_confined", lambda *a: (True, "verified"))
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
        _, bwrap = agy_sandbox.agy_wrapper_parts(tmp_path / "ws", tmp_path / "st")
        hidden = [Path(bwrap[i + 1]) for i, a in enumerate(bwrap) if a == "--tmpfs"]
        assert tmp_path in hidden
        if account != Path("/"):
            assert account in hidden
            # A home inside the other is mounted after it, on top, not hidden under it.
            if tmp_path.is_relative_to(account):
                assert hidden.index(account) < hidden.index(tmp_path)


class TestAgyRunner(AgyCase):
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
        assert (
            'review_helper agy-sandbox "$AGY_WS" "$AGY_STATE" "$REVIEW_ROOT"' in prepare
        )
        assert "review_helper agy-schema" in prepare
        # The preamble written into the prompt file, which then goes in as one line.
        assert 'review_helper agy-input "$prompt_file" "$AGY_IN"' in prepare
        assert '< "$AGY_IN"' in runner and "agy-message" not in runner
        assert runner.count("python3") == 1  # review_helper's own definition
        assert 'prepare_agy "$num" "$prompt_file" || return 1' in runner
        # Its output parsed as agy's only because agy ran it -- as the runner itself
        # recorded, not as a file a reviewer could rewrite says.
        assert "JOB_BACKEND[$num]=agy" in runner
        assert 'extract_json "$src" "$result" "$num" "${JOB_BACKEND[$num]:-}"' in runner
        assert 'echo agy > "$REVIEW_TMP/backend-$num.txt"' in runner  # for watching
        assert 'cat "$REVIEW_TMP/backend-' not in runner
        # Scratch listed in the runner's own shell, not in a file a reviewer could write.
        assert "make_scratch AGY_WS mmcra-ws- && make_scratch AGY_STATE" in prepare
        assert 'SCRATCH+=("$dir")' in shell_function(runner, "make_scratch")
        assert "scratch-" not in runner
        assert "trap finish_round EXIT" in runner
        # A failed mktemp must not fall back to running agy in the repository.
        assert '&& [ -n "$dir" ] || return 1' in shell_function(runner, "make_scratch")
        assert 'rm -rf "$AGY_WS" "$AGY_STATE"' in runner
        # Absolute, or the job's cd into the workspace sends its output there.
        assert 'REVIEW_TMP="$(cd "$REVIEW_TMP" && pwd -P)" || exit 1' in runner

    def test_a_bedrock_project_gets_agy_only_by_name_in_the_runner(self) -> None:
        runner = runner_script()
        gate = "{ ! $PREFER_HERMES || review_helper flag REVIEW_USE_AGY; }"
        assert 'elif ${HAS_AGY:-false} && [ -n "${AGY_SELECTED_MODEL:-}" ]' in runner
        assert f"    && {gate}" in runner
