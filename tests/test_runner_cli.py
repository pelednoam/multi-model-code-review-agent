"""scripts/review_loop/runner_cli.py, as the full-mode runner calls it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from helpers import AGENT_DOC, REPO_ROOT, SCHEMA_PATH, runner_script
from scripts.review_loop.config import env_flag


class TestRunnerCli:
    """scripts/review_loop/runner_cli.py: the loop's own code, as the bash runner calls it."""

    ENTRY = "scripts/review_loop/runner_entry.py"

    def _cli(
        self, *args: str, env: dict[str, str] | None = None, root: Path = REPO_ROOT
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(root / self.ENTRY), *args],
            capture_output=True, text=True, timeout=60, cwd=root,
            env={**os.environ, **(env or {})},
        )  # fmt: skip

    def test_the_runner_calls_it_the_same_way(self) -> None:
        # One short line in each place, the detection snippet's and the runner's.
        doc = AGENT_DOC.read_text()
        assert doc.count('python3 -I "$PWD/' + self.ENTRY + '" "$@"') == 1
        # The runner's from a private copy taken before any reviewer starts.
        assert 'python3 -I "$HELPER_ROOT/' + self.ENTRY + '" "$@"' in runner_script()
        assert 'cp -RL "$REVIEW_ROOT/scripts/review_loop" "$HELPER_ROOT/scripts/"' in (
            runner_script()
        )

    def test_the_checkout_root_does_not_shadow_the_stdlib(self, tmp_path: Path) -> None:
        # With the root put first on sys.path, a json.py at its top replaced the
        # standard library's for every import the helper made.
        root = tmp_path / "root"
        shutil.copytree(
            REPO_ROOT / "scripts", root / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )  # fmt: skip
        (root / "json.py").write_text("raise SystemExit('shadowed')\n")
        done = self._cli("flag", "NOPE", root=root)
        assert done.returncode == 1 and "shadowed" not in done.stderr, done.stderr

    def test_a_wrong_number_of_arguments_is_a_usage_error(self) -> None:
        assert self._cli("extract").returncode == 2
        assert self._cli("no-such-command").returncode == 2

    def test_a_bare_object_is_not_agys_review(self, tmp_path: Path) -> None:
        # No result event at all: read as plain text, a bare object passed as agy's.
        raw = tmp_path / "stdout-3.txt"
        raw.write_text(json.dumps(self.REVIEW), encoding="utf-8")
        out = tmp_path / "result-3.json"
        done = self._cli("extract", str(raw), str(out), "3", "agy")
        assert done.returncode == 1 and "no result event" in done.stderr

    @staticmethod
    def _agy_stream(tmp_path: Path, **result: object) -> Path:
        raw = tmp_path / "stdout-3.txt"
        event = {"event": "result", "result": {"status": "SUCCESS", **result}}
        stream = '{"event": "init"}\n' + json.dumps(event) + "\n"
        raw.write_text(stream, encoding="utf-8")
        return raw

    REVIEW = {"reviewer": "r", "model": "m", "findings": [], "overall_assessment": "ok"}

    def test_a_result_key_in_agys_answer_does_not_replace_it(
        self, tmp_path: Path
    ) -> None:
        # The runner unwrapped Claude's {"result": "..."} envelope from agy's
        # schema-checked object too, so a key the diff talked agy into adding won.
        forged = json.dumps({**self.REVIEW, "findings": [{"forged": True}]})
        answer = {**self.REVIEW, "result": forged}
        raw = self._agy_stream(tmp_path, structured_output=answer)
        out = tmp_path / "result-3.json"
        done = self._cli("extract", str(raw), str(out), "3", "agy")
        assert done.returncode == 0, done.stderr
        assert json.loads(out.read_text())["findings"] == []

    def test_an_unfinished_agy_turn_is_no_review(self, tmp_path: Path) -> None:
        raw = self._agy_stream(
            tmp_path, status="ERROR", error="Individual quota reached",
            response=json.dumps(self.REVIEW),
        )  # fmt: skip
        out = tmp_path / "result-3.json"
        done = self._cli("extract", str(raw), str(out), "3", "agy")
        assert done.returncode == 1 and "quota" in done.stderr and not out.exists()

    def test_agys_shape_from_another_backend_is_not_read_as_agys(
        self, tmp_path: Path
    ) -> None:
        raw = self._agy_stream(tmp_path, status="ERROR")
        out = tmp_path / "result-3.json"
        assert self._cli("extract", str(raw), str(out), "3", "claude").returncode == 1
        assert not out.exists()

    def test_claudes_envelope_is_unwrapped(self, tmp_path: Path) -> None:
        raw = tmp_path / "claude-raw-1.json"
        raw.write_text(json.dumps({"result": "Here:\n" + json.dumps(self.REVIEW)}))
        out = tmp_path / "result-1.json"
        assert self._cli("extract", str(raw), str(out), "1", "claude").returncode == 0
        # The reviewer is named by the slot, as the loop names it, not by the model.
        assert json.loads(out.read_text()) == {**self.REVIEW, "reviewer": "security"}

    def test_findings_that_are_not_a_list_of_objects_are_no_review(
        self, tmp_path: Path
    ) -> None:
        # The runner checked only the keys; the loop's validate_result rejects these.
        raw = tmp_path / "claude-raw-1.json"
        out = tmp_path / "result-1.json"
        for findings in ("none", ["a string"]):
            raw.write_text(
                json.dumps(
                    {"result": json.dumps({**self.REVIEW, "findings": findings})}
                )
            )
            assert (
                self._cli("extract", str(raw), str(out), "1", "claude").returncode == 1
            )
            assert not out.exists()

    def test_a_broken_schema_does_not_cost_the_other_reviewers(
        self, tmp_path: Path
    ) -> None:
        # The required keys came from the schema file, so a broken schema -- which only
        # sends agy's slot elsewhere -- made every other review fail to extract.
        root = tmp_path / "root"
        shutil.copytree(
            REPO_ROOT / "scripts", root / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )  # fmt: skip
        (root / "docs").mkdir()
        (root / "docs" / "ensemble_review_result_schema.json").write_text("{not json")
        raw = tmp_path / "claude-raw-1.json"
        raw.write_text(json.dumps({"result": json.dumps(self.REVIEW)}))
        out = tmp_path / "result-1.json"
        assert (
            self._cli(
                "extract", str(raw), str(out), "1", "claude", root=root
            ).returncode
            == 0
        )

    def test_agys_error_reaches_the_err_file_without_terminal_escapes(
        self, tmp_path: Path
    ) -> None:
        raw = self._agy_stream(
            tmp_path, status="ERROR", error="quota\x1b]52;c;cGF5\x07"
        )
        done = self._cli("extract", str(raw), str(tmp_path / "r.json"), "3", "agy")
        assert (
            done.returncode == 1
            and "\x1b" not in done.stderr
            and "quota" in done.stderr
        )

    def test_reachable_is_not_asked_of_a_host_that_is_the_sandbox(
        self, tmp_path: Path
    ) -> None:
        env = {"MMCRA_SKIP_AGY_SANDBOX_CHECK": "1"}
        done = self._cli("agy-reachable", str(tmp_path), env=env)
        assert done.returncode == 0

    def test_reachable_checks_the_wrapper_it_is_handed(self, tmp_path: Path) -> None:
        # It built a wrapper of its own to check, which could differ from the one agy
        # was launched under. A "wrapper" that cannot start fails the check.
        env = {"MMCRA_SKIP_AGY_SANDBOX_CHECK": "0"}
        done = subprocess.run(
            [sys.executable, "-I", str(REPO_ROOT / self.ENTRY), "agy-reachable",
             str(tmp_path)],
            input=b"/nonexistent/wrapper\0--\0", capture_output=True, timeout=60,
            env={**os.environ, **env},
        )  # fmt: skip
        assert done.returncode == 1 and b"could not check" in done.stdout
        empty = subprocess.run(
            [sys.executable, "-I", str(REPO_ROOT / self.ENTRY), "agy-reachable",
             str(tmp_path)],
            input=b"", capture_output=True, timeout=60, env={**os.environ, **env},
        )  # fmt: skip
        assert empty.returncode == 1

    @pytest.mark.parametrize(
        ("value", "skipped"),
        [("1", True), ("true", True), ("YES", True), ("on", True),
         ("", False), ("0", False), ("false", False), ("no", False), ("off", False)],
    )  # fmt: skip
    def test_one_reading_of_every_skip_switch(self, value: str, skipped: bool) -> None:
        # MMCRA_SKIP_CODEX_SANDBOX_CHECK=0 skipped the codex check, and
        # MMCRA_SKIP_AGY_SANDBOX_CHECK=false ran agy with no sandbox.
        with mock.patch.dict(os.environ, {"MMCRA_SKIP_X": value}):
            assert env_flag("MMCRA_SKIP_X") is skipped

    def test_codex_check_is_not_skipped_by_zero(self, tmp_path: Path) -> None:
        # With no codex on PATH, a check that runs says so; a skipped one passes.
        env = {"PATH": str(tmp_path), "MMCRA_SKIP_CODEX_SANDBOX_CHECK": "0"}
        done = self._cli("codex-check", env=env)
        assert done.returncode == 1 and "not installed" in done.stderr
        env["MMCRA_SKIP_CODEX_SANDBOX_CHECK"] = "true"
        assert self._cli("codex-check", env=env).returncode == 0

    @pytest.mark.parametrize(("value", "bare"), [("false", False), ("yes", True)])
    def test_agy_sandbox_reads_the_switch_the_same_way(
        self, tmp_path: Path, value: str, bare: bool
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        env = {"HOME": str(home), "MMCRA_SKIP_AGY_SANDBOX_CHECK": value}
        done = self._cli(
            "agy-sandbox", str(tmp_path), str(tmp_path / "st"), str(home), env=env
        )
        parts = done.stdout.split("\0")
        assert (parts == ["0", ""]) is bare, done.stdout

    @pytest.mark.parametrize(
        "text", [None, "{not json", '{"type": "not-a-type"}'],
        ids=["missing", "not-json", "not-a-schema"],
    )  # fmt: skip
    def test_an_unusable_schema_fails(self, tmp_path: Path, text: str | None) -> None:
        schema = tmp_path / "schema.json"
        if text is not None:
            schema.write_text(text, encoding="utf-8")
        assert self._cli("agy-schema", str(schema)).returncode == 1

    def test_agys_input_and_its_record_are_written_together(
        self, tmp_path: Path
    ) -> None:
        # The preamble went into the prompt file first; if the input line then could
        # not be written, the slot fell through with agy's preamble in its prompt.
        prompt = tmp_path / "prompt-3.txt"
        prompt.write_text("Review this.\n", encoding="utf-8")
        blocked = tmp_path / "no-such-dir" / "agy-in-3.jsonl"
        assert self._cli("agy-input", str(prompt), str(blocked)).returncode != 0
        assert prompt.read_text(encoding="utf-8") == "Review this.\n"
        written = tmp_path / "agy-in-3.jsonl"
        assert self._cli("agy-input", str(prompt), str(written)).returncode == 0
        (line,) = written.read_text(encoding="utf-8").splitlines()
        sent = json.loads(line)["message"]["content"]
        assert sent == prompt.read_text(encoding="utf-8")
        assert sent.endswith("Review this.\n") and sent != "Review this.\n"

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "properties": {"a": "not a schema"}},
            {"type": "object", "properties": {"a": {}}, "required": ["b"]},
            {"type": "object", "properties": []},
            # Valid, but agy's answer need not be a review the loop can read.
            {
                "type": "object",
                "properties": {"reviewer": {}},
                "required": ["reviewer"],
            },
        ],
        ids=[
            "property-not-a-schema",
            "requires-undefined",
            "properties-not-a-map",
            "review-keys-not-required",
        ],
    )
    def test_a_broken_schema_fails_without_jsonschema(
        self, tmp_path: Path, schema: dict[str, object]
    ) -> None:
        # With no jsonschema installed, only the top-level type was looked at, and agy
        # rejected the rest only after launch.
        from scripts.review_loop.agy import _check_schema_shape

        with pytest.raises(ValueError, match="not a usable result schema"):
            _check_schema_shape(schema, tmp_path / "s.json")

    def test_a_valid_schema_without_the_shape_fails_with_jsonschema_too(
        self, tmp_path: Path
    ) -> None:
        # A valid JSON Schema can still lack what a result needs; with jsonschema
        # installed, the shape went unchecked.
        schema = tmp_path / "s.json"
        schema.write_text(json.dumps({"type": "object", "required": ["findings"]}))
        assert self._cli("agy-schema", str(schema)).returncode == 1

    def test_a_schema_nested_past_the_parsers_limit_is_unusable(
        self, tmp_path: Path
    ) -> None:
        # RecursionError went past the ValueError the slot's fallback catches.
        schema = tmp_path / "s.json"
        schema.write_text("[" * 100_000 + "]" * 100_000)
        done = self._cli("agy-schema", str(schema))
        assert done.returncode == 1 and "nested too deeply" in done.stderr

    def test_a_part_of_no_json_type_is_refused_without_jsonschema(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Without jsonschema, a schema with a broken part passed the check, for agy
        # to reject only after launch, when the slot could not go to another backend.
        from scripts.review_loop import agy

        monkeypatch.setitem(sys.modules, "jsonschema", None)
        schema = json.loads(SCHEMA_PATH.read_text())
        schema["properties"]["findings"]["items"] = {"type": 5}
        bad = tmp_path / "s.json"
        bad.write_text(json.dumps(schema))
        with pytest.raises(ValueError, match="not a JSON type"):
            agy.agy_schema(bad)
        # A property called "type" is a name, not the keyword.
        schema["properties"]["findings"]["items"] = {
            "type": "object",
            "properties": {"type": {"type": "string"}},
        }
        bad.write_text(json.dumps(schema))
        assert json.loads(agy.agy_schema(bad))["type"] == "object"

    def test_the_real_schema_is_usable(self) -> None:
        done = self._cli("agy-schema", str(SCHEMA_PATH))
        assert done.returncode == 0 and json.loads(done.stdout)["type"] == "object"


class TestRunnerCliInProcess:
    """The same commands called in this process, as main() dispatches them."""

    def test_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop import runner_cli

        monkeypatch.setenv("MMCRA_X_FLAG", "yes")
        assert runner_cli.main(["flag", "MMCRA_X_FLAG"]) == 0
        monkeypatch.setenv("MMCRA_X_FLAG", "0")
        assert runner_cli.main(["flag", "MMCRA_X_FLAG"]) == 1
        assert runner_cli.main(["flag"]) == 2 and runner_cli.main([]) == 2

    def test_extract_names_the_model_that_ran(self, tmp_path: Path) -> None:
        # The prompt names the lens's own model; a fallback backend is another, and
        # the result said the wrong one.
        from scripts.review_loop import runner_cli

        review = {"reviewer": "r", "model": "gpt-5.5", "findings": [],
                  "overall_assessment": "ok"}  # fmt: skip
        raw, out = tmp_path / "raw.txt", tmp_path / "result-2.json"
        raw.write_text(json.dumps(review))
        assert runner_cli.main(["extract", str(raw), str(out), "2", "claude",
                                "claude-haiku"]) == 0  # fmt: skip
        assert json.loads(out.read_text())["model"] == "claude-haiku"

    def test_placeholder_and_claude_deny(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from scripts.review_loop import runner_cli

        out = tmp_path / "result-3.json"
        assert runner_cli.main(["placeholder", str(out), "3", "timed out"]) == 0
        assert "timed out" in json.loads(out.read_text())["overall_assessment"]
        assert runner_cli.main(["claude-deny", "Read Bash(git log:*)"]) == 0
        said = capsys.readouterr()
        assert "Bash" not in said.out.split() and "granted whole" in said.err

    def test_reachable_reads_a_wrapper_that_is_not_utf8(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Decoded strictly, a path that is not UTF-8 failed; empty arguments were
        # dropped, so the wrapper checked was not the one that ran.
        from scripts.review_loop import runner_cli

        seen: dict[str, object] = {}

        def reachable(wrap: list[str], workspace: Path) -> None:
            seen["wrap"] = wrap

        monkeypatch.setattr(runner_cli, "agy_reachable", reachable)
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        data = b"bwrap\0--bind\0/srv/\xff\0\0--\0"
        monkeypatch.setattr(runner_cli.sys, "stdin", SimpleNamespace(
            buffer=SimpleNamespace(read=lambda: data)))  # fmt: skip
        assert runner_cli.main(["agy-reachable", str(tmp_path)]) == 0
        assert seen["wrap"] == ["bwrap", "--bind", os.fsdecode(b"/srv/\xff"), "", "--"]

    def test_the_entry_point_runs_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import runpy

        monkeypatch.setattr(sys, "argv", ["runner_entry.py", "flag", "MMCRA_UNSET_X"])
        # It puts a stand-in ``scripts`` package in place; the real one comes back.
        monkeypatch.setitem(sys.modules, "scripts", sys.modules["scripts"])
        monkeypatch.delenv("MMCRA_UNSET_X", raising=False)
        with pytest.raises(SystemExit) as done:
            runpy.run_path(str(REPO_ROOT / TestRunnerCli.ENTRY), run_name="__main__")
        assert done.value.code == 1
