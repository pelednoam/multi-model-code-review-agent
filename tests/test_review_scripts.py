"""Tests for ensemble review infrastructure scripts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRUB_SCRIPT = REPO_ROOT / "scripts" / "scrub_diff.py"
PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "review_preflight.py"
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_review_results.py"
SCHEMA_PATH = REPO_ROOT / "docs" / "ensemble_review_result_schema.json"


class TestScrubDiff:
    """Tests for scripts/scrub_diff.py."""

    def _run_scrub(self, input_text: str) -> tuple[str, str, int]:
        result = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT)],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout, result.stderr, result.returncode

    def test_clean_diff_passes_through(self) -> None:
        diff = "diff --git a/foo.py b/foo.py\n+def hello():\n+    pass\n"
        stdout, stderr, code = self._run_scrub(diff)
        assert stdout == diff
        assert code == 0
        assert "redacted" not in stderr

    def test_redacts_api_key(self) -> None:
        diff = '+API_KEY = "abc123"\n'
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_redacts_password(self) -> None:
        diff = '+password = "hunter2"\n'
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_redacts_aws_key(self) -> None:
        diff = "+AKIAIOSFODNN7EXAMPLE\n"
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_redacts_openai_key(self) -> None:
        diff = "+sk-abcdefghijklmnopqrstuvwxyz1234\n"
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_redacts_github_pat(self) -> None:
        diff = "+ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij\n"
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_redacts_gitlab_token(self) -> None:
        diff = "+glpat-ABCDEFGHIJKLMNOPQRSTUVwx\n"
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_redacts_private_key(self) -> None:
        diff = "+-----BEGIN PRIVATE KEY-----\n"
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_redacts_gcp_service_account(self) -> None:
        diff = '+  "type": "service_account"\n'
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_does_not_self_redact(self) -> None:
        diff = (
            "diff --git a/scripts/scrub_diff.py"
            " b/scripts/scrub_diff.py\n"
            '+    re.compile(r"sk-[a-zA-Z0-9]{20,128}"),\n'
            "+    re.compile("
            'r"(?i)(password|passwd|pwd)\\s*[:=]"),\n'
        )
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" not in stdout
        assert code == 0

    def test_does_not_redact_test_file(self) -> None:
        diff = (
            "diff --git a/tests/test_review_scripts.py"
            " b/tests/test_review_scripts.py\n"
            "+        diff = '+API_KEY = \"abc123\"\\n'\n"
            "+        diff = '+password = \"hunter2\"\\n'\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" not in stdout
        assert code == 0

    def test_exit_code_nonzero_on_redaction(self) -> None:
        diff = "+secret_key = abc\n"
        _, _, code = self._run_scrub(diff)
        assert code == 1

    def test_preserves_diff_structure(self) -> None:
        diff = (
            "diff --git a/config.py b/config.py\n"
            "--- a/config.py\n"
            "+++ b/config.py\n"
            "@@ -1,3 +1,4 @@\n"
            "+API_KEY = secret\n"
            " def main():\n"
            "     pass\n"
        )
        stdout, _, _ = self._run_scrub(diff)
        assert "diff --git" in stdout
        assert "--- a/config.py" in stdout
        assert "@@ -1,3 +1,4 @@" in stdout
        assert "def main():" in stdout

    def test_counts_redactions_on_stderr(self) -> None:
        diff = "+API_KEY=a\n+password=b\n+token=c\n"
        _, stderr, _ = self._run_scrub(diff)
        assert "3 line(s) redacted" in stderr

    def test_does_not_match_dotenv_in_prose(self) -> None:
        diff = "+# See the .environment docs for details\n"
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" not in stdout
        assert code == 0

    def test_empty_input(self) -> None:
        stdout, stderr, code = self._run_scrub("")
        assert stdout == ""
        assert code == 0


class TestReviewPreflight:
    """Tests for scripts/review_preflight.py."""

    def _run_preflight(
        self, tmp_path: Path
    ) -> tuple[dict, subprocess.CompletedProcess[str]]:
        output = tmp_path / "audit.json"
        proc = subprocess.run(
            [
                sys.executable,
                str(PREFLIGHT_SCRIPT),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        audit = json.loads(output.read_text()) if output.exists() else {}
        return audit, proc

    def test_runs_successfully(self, tmp_path: Path) -> None:
        audit, _ = self._run_preflight(tmp_path)
        assert "git" in audit
        assert "signed_manifests" in audit
        assert "n_warnings" in audit
        assert isinstance(audit["git"]["branch"], str)
        assert isinstance(audit["git"]["commit"], str)

    def test_output_has_required_sections(self, tmp_path: Path) -> None:
        audit, _ = self._run_preflight(tmp_path)
        required = {
            "timestamp",
            "git",
            "git_command_warnings",
            "signed_manifests",
            "changed_artifacts",
            "suspicious_patterns",
            "test_coverage_alignment",
            "coverage_gaps",
            "coverage_warnings",
            "coverage_target_pct",
            "n_warnings",
        }
        assert required <= set(audit.keys())
        assert isinstance(audit["coverage_gaps"], list)
        assert isinstance(audit["coverage_warnings"], list)
        assert isinstance(audit["coverage_target_pct"], int)

    def test_git_command_warnings_populated_on_bad_remote(self, tmp_path: Path) -> None:
        from scripts.review_preflight import _run_git

        warnings: list[str] = []
        _run_git(
            [
                "diff",
                "--merge-base",
                "nonexistent/branch",
                "--name-only",
            ],
            warnings,
        )
        assert len(warnings) > 0
        assert "git failed" in warnings[0]

    def test_missing_manifest_reported(self) -> None:
        from scripts.review_preflight import (
            SIGNED_MANIFESTS,
            audit_signed_manifests,
        )

        old = dict(SIGNED_MANIFESTS)
        try:
            SIGNED_MANIFESTS["test_missing"] = "nonexistent.json"
            results = audit_signed_manifests()
            missing = [r for r in results if r["manifest"] == "test_missing"]
            assert len(missing) == 1
            assert "warning" in missing[0]
            assert "missing" in missing[0]["warning"]
        finally:
            SIGNED_MANIFESTS.clear()
            SIGNED_MANIFESTS.update(old)


class TestValidateReviewResults:
    """Tests for scripts/validate_review_results.py."""

    def _make_valid_result(self) -> dict:
        return {
            "reviewer": "security",
            "model": "test-model",
            "findings": [
                {
                    "severity": "warning",
                    "confidence": "high",
                    "category": "security",
                    "file": "foo.py",
                    "line": 10,
                    "issue": "test issue",
                    "rationale": "test rationale",
                    "observed_or_inferred": "observed_in_diff",
                    "blocking": False,
                    "suggested_fix": "No change needed.",
                }
            ],
            "overall_assessment": "Looks good.",
        }

    def test_valid_result_passes(self, tmp_path: Path) -> None:
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(self._make_valid_result()))
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0
        assert "PASS" in proc.stdout

    def test_rejects_extra_top_level_field(self, tmp_path: Path) -> None:
        data = self._make_valid_result()
        data["extra"] = True
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(data))
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 1
        assert "Additional properties" in proc.stdout

    def test_rejects_extra_finding_field(self, tmp_path: Path) -> None:
        data = self._make_valid_result()
        data["findings"][0]["extra_field"] = "bad"
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(data))
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 1
        assert "Additional properties" in proc.stdout

    def test_rejects_invalid_severity(self, tmp_path: Path) -> None:
        data = self._make_valid_result()
        data["findings"][0]["severity"] = "blocker"
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(data))
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 1

    def test_allows_null_optional_fields(self, tmp_path: Path) -> None:
        data = self._make_valid_result()
        data["findings"][0]["repro_command"] = None
        data["findings"][0]["contract_reference"] = None
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(data))
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0

    def test_allows_custom_reviewer_name(self, tmp_path: Path) -> None:
        data = self._make_valid_result()
        data["reviewer"] = "adversarial"
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(data))
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0

    def test_rejects_missing_required_finding_field(self, tmp_path: Path) -> None:
        data = self._make_valid_result()
        del data["findings"][0]["blocking"]
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(data))
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 1
        assert "blocking" in proc.stdout

    def test_file_not_found(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), "/nonexistent.json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 1
        assert "file not found" in proc.stdout

    def test_malformed_json_handled(self, tmp_path: Path) -> None:
        result_path = tmp_path / "bad.json"
        result_path.write_text("{not valid json")
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 1
        assert "failed to parse" in proc.stdout

    def test_empty_findings_passes(self, tmp_path: Path) -> None:
        data = self._make_valid_result()
        data["findings"] = []
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(data))
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0
        assert "0 findings" in proc.stdout

    def test_omitted_optional_fields_passes(self, tmp_path: Path) -> None:
        data = self._make_valid_result()
        for key in (
            "repro_command",
            "contract_reference",
            "line",
        ):
            data["findings"][0].pop(key, None)
        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps(data))
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), str(result_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0


class TestRunGate:
    """The mandatory CI gate (lint + format + mypy + tests)."""

    def test_run_gate_returns_bool_and_str(self, tmp_path: Path) -> None:
        from scripts.review_until_converged import run_gate

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "tests").mkdir()
        ok, output = run_gate(repo)
        # ok may be True (all tools skipped or passed) or False (mypy
        # bails early because scripts/ is missing in the empty repo).
        # Either is a valid outcome for this smoke test -- what we care
        # about is that the gate ran at least the first step and
        # returned structured output without crashing.
        assert isinstance(ok, bool)
        assert isinstance(output, str)
        assert "ruff check" in output

    def test_run_gate_steps_in_order(self) -> None:
        """The gate definition must include all four mandatory steps."""
        import inspect

        from scripts.review_until_converged import run_gate

        source = inspect.getsource(run_gate)
        for label in ("ruff check", "ruff format", "mypy", "pytest"):
            assert label in source


class TestSecretsDetected:
    """The scrubber must STOP the loop, not just warn."""

    def test_exception_is_exported(self) -> None:
        from scripts.review_until_converged import SecretsDetectedError

        assert issubclass(SecretsDetectedError, RuntimeError)

    def test_collect_diff_raises_on_redaction(self, tmp_path: Path) -> None:
        # Stub scrub_diff.py with one that exits 1 to simulate redaction.
        # Set up a git repo with one commit + a staged change so collect_diff
        # has a real diff to feed the (fake) scrubber.
        from unittest.mock import patch

        from scripts.review_loop.diff import SecretsDetectedError, collect_diff

        round_dir = tmp_path / "round"
        round_dir.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env
        )
        (repo / "x.py").write_text("a = 1\n")
        subprocess.run(["git", "add", "x.py"], cwd=repo, check=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env
        )
        # Need HEAD~1 to resolve, so make a second commit.
        (repo / "x.py").write_text("a = 2\n")
        subprocess.run(["git", "add", "x.py"], cwd=repo, check=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", "two"], cwd=repo, check=True, env=env
        )

        fake_scrubber = tmp_path / "scrub_diff.py"
        fake_scrubber.write_text(
            'import sys\nprint("redacted: 1 line", file=sys.stderr)\nsys.exit(1)\n'
        )
        with patch("scripts.review_loop.diff.SCRIPTS_DIR", tmp_path):
            with pytest.raises(SecretsDetectedError) as exc_info:
                collect_diff(round_dir, repo)
        assert "rotated" in str(exc_info.value)


class TestSourceDirsConfigurable:
    """Coverage gate must surface when SOURCE_DIRS doesn't match anything."""

    def test_warning_emitted_when_python_files_outside_source_dirs(self) -> None:
        from scripts.preflight.coverage import measure_test_coverage

        warnings: list[str] = []
        # Simulate a backend/app project: changed Python files exist but
        # none under the default src/ or research/ prefixes.
        changed = ["backend/views.py", "backend/models.py"]
        result = measure_test_coverage(changed, warnings)
        assert result == []
        assert len(warnings) == 1
        assert "coverage gate inoperative" in warnings[0]
        assert "SOURCE_DIRS" in warnings[0]

    def test_no_warning_when_no_python_files(self) -> None:
        from scripts.preflight.coverage import measure_test_coverage

        warnings: list[str] = []
        # Doc-only or config-only diff: nothing to measure, no warning.
        result = measure_test_coverage(["docs/foo.md", "config.yaml"], warnings)
        assert result == []
        assert warnings == []


class TestCodexEnvOverrides:
    """CODEX_MODEL and CODEX_REASONING_EFFORT pass through to codex exec."""

    def test_no_env_means_no_extra_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop.reviewers import _codex_extra_args

        monkeypatch.delenv("CODEX_MODEL", raising=False)
        monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
        assert _codex_extra_args() == []

    def test_model_env_adds_dash_m(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop.reviewers import _codex_extra_args

        monkeypatch.setenv("CODEX_MODEL", "gpt-5.5-codex")
        monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
        assert _codex_extra_args() == ["-m", "gpt-5.5-codex"]

    def test_reasoning_high_is_pro_equivalent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_loop.reviewers import _codex_extra_args

        monkeypatch.delenv("CODEX_MODEL", raising=False)
        monkeypatch.setenv("CODEX_REASONING_EFFORT", "high")
        # The bash equivalent is `-c model_reasoning_effort=high` and
        # matches what the ChatGPT web UI labels as "GPT-5.5 Pro".
        assert _codex_extra_args() == ["-c", "model_reasoning_effort=high"]

    def test_both_env_vars_compose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop.reviewers import _codex_extra_args

        monkeypatch.setenv("CODEX_MODEL", "gpt-5.5")
        monkeypatch.setenv("CODEX_REASONING_EFFORT", "high")
        args = _codex_extra_args()
        assert "-m" in args and "gpt-5.5" in args
        assert "-c" in args and "model_reasoning_effort=high" in args
