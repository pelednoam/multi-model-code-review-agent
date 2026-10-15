"""scripts/review_loop/runner_cli.py, as the full-mode runner calls it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
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
        assert 'python3 -I "$REVIEW_ROOT/' + self.ENTRY + '" "$@"' in runner_script()

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
        assert self._cli("extract", str(raw), str(out), "1").returncode == 0
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
            assert self._cli("extract", str(raw), str(out), "1").returncode == 1
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
        assert self._cli("extract", str(raw), str(out), "1", root=root).returncode == 0

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
        done = self._cli("agy-sandbox", str(tmp_path), str(tmp_path / "st"), env=env)
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

    def test_the_real_schema_is_usable(self) -> None:
        done = self._cli("agy-schema", str(SCHEMA_PATH))
        assert done.returncode == 0 and json.loads(done.stdout)["type"] == "object"
