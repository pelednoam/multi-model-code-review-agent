"""agy in slot 3: which backend a slot gets, and the command agy runs as."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from agy_case import AgyCase
from helpers import REPO_ROOT, SCHEMA_PATH

from scripts.review_loop import agy, agy_sandbox, reviewers

# agy_models itself, kept before AgyCase's autouse fixture replaces it.
from scripts.review_loop.agy import agy_models as real_agy_models
from scripts.review_loop.reviewers import _choose_command


class TestAgyRouting(AgyCase):
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
        assert agy.agy_model_choice()[0] == "claude-opus-4-6-thinking"

    def test_the_pick_is_made_once_per_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The confinement check and the reviewer must run the same model.
        calls: list[int] = []
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(
            agy, "agy_models", lambda *a: calls.append(1) or list(self.LISTED)
        )
        assert (
            agy.agy_model_choice()[0]
            == agy.agy_model_choice()[0]
            == "gemini-3.8-flash-high"
        )
        assert len(calls) == 1

    @pytest.mark.parametrize(
        ("listed", "best"),
        [
            # This machine today.
            (AgyCase.LISTED, "gemini-3.8-flash-high"),
            # The day the plan includes Argon: a newer version beats a higher effort.
            ([*AgyCase.LISTED, "gemini-4-argon"], "gemini-4-argon"),
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
            # 4 and 4.0 are one version; the tier and effort decide between them.
            (["gemini-4.0-flash-low", "gemini-4-pro-high"], "gemini-4-pro-high"),
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
        state = Path(wrap[wrap.index(str(agy_sandbox.agy_state_dir())) - 1])
        hidden = (Path.cwd(), REPO_ROOT)  # the repository, and this checkout
        assert wrap == agy_sandbox.agy_wrapper(Path(str(seen["cwd"])), state, hidden)

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
        [(None, True), ("1", True), ("yes", True), ("ON", True),
         ("0", False), ("false", False), ("No", False), (" off ", False), ("", False),
         # A typo was read as on, and sent the diff to Google.
         ("of", False), ("disabled", False)],
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


class TestAgyCommand(AgyCase):
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

    def test_slot_2_on_claude_is_named_haiku(self, tmp_path: Path) -> None:
        # The lens names codex's model; on claude, the prompt and result said gpt-5.5.
        found = {**self.BACKENDS, "codex": False}
        choice = _choose_command(2, tmp_path / "p.txt", tmp_path, found)
        assert choice is not None and choice[0][0] == "claude"
        assert choice[1] == "claude-haiku-4.5"

    @pytest.mark.parametrize(
        ("opted_in", "first"), [(False, "claude"), (True, "codex")]
    )
    def test_a_bedrock_project_does_not_fall_to_openai_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        opted_in: bool,
        first: str,
    ) -> None:
        # With Hermes unavailable, a project pinned to Bedrock sent slot 2 to codex.
        if opted_in:
            monkeypatch.setenv("REVIEW_USE_CODEX", "1")
        found = {**self.BACKENDS, "hermes": False, "codex": True}
        choice = _choose_command(2, tmp_path / "p.txt", tmp_path, found, True)
        assert choice is not None and choice[0][0] == first

    @pytest.mark.parametrize(
        "slug", ["gemini-x\x1b]0;owned\x07", "a b", "-flag", "x" * 200]
    )
    def test_a_listed_model_that_is_not_a_name_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch, slug: str
    ) -> None:
        # `agy models` is a network answer, printed and passed as --model unchecked.
        monkeypatch.setattr(agy, "agy_models", real_agy_models)
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        out = f"{slug}\tDisplay\ngemini-3.8-flash-high\tGemini\n"
        monkeypatch.setattr(agy, "run", lambda *a, **k: SimpleNamespace(stdout=out))
        monkeypatch.setenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", "1")
        assert agy.agy_models() == ["gemini-3.8-flash-high"]

    def test_an_explicit_model_that_is_not_a_name_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGY_MODEL", "gemini; rm -rf /")
        model, how = agy.agy_model_choice()
        assert model is None and "not a model name" in how

    def test_a_listing_that_failed_is_asked_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cached, one empty listing -- offline for a moment -- kept agy off all loop.
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.setattr(agy.shutil, "which", lambda c: f"/usr/bin/{c}")
        answers = [[], ["gemini-3.8-flash-high"]]
        monkeypatch.setattr(agy, "agy_models", lambda *a: answers.pop(0))
        first, how = agy.agy_model_choice()
        assert first is None and "listed no models" in how
        assert agy.agy_model_choice()[0] == "gemini-3.8-flash-high"

    def test_codex_names_the_model_it_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        found = {**self.BACKENDS, "codex": True}
        choice = _choose_command(2, tmp_path / "p.txt", tmp_path, found)
        assert choice is not None and choice[0][0] == "codex" and choice[1] is None
        monkeypatch.setenv("CODEX_MODEL", "gpt-5.5-codex")
        choice = _choose_command(2, tmp_path / "p.txt", tmp_path, found)
        assert choice is not None and choice[1] == "gpt-5.5-codex"
