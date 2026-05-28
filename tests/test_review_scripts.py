"""Tests for ensemble review infrastructure scripts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "+def hello():\n"
            "+    pass\n"
        )
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

    def test_output_has_required_sections(
        self, tmp_path: Path
    ) -> None:
        audit, _ = self._run_preflight(tmp_path)
        required = {
            "timestamp",
            "git",
            "git_command_warnings",
            "signed_manifests",
            "changed_artifacts",
            "suspicious_patterns",
            "test_coverage_alignment",
            "n_warnings",
        }
        assert required <= set(audit.keys())

    def test_git_command_warnings_populated_on_bad_remote(
        self, tmp_path: Path
    ) -> None:
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
            missing = [
                r for r in results if r["manifest"] == "test_missing"
            ]
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

    def test_rejects_extra_top_level_field(
        self, tmp_path: Path
    ) -> None:
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

    def test_rejects_extra_finding_field(
        self, tmp_path: Path
    ) -> None:
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

    def test_allows_null_optional_fields(
        self, tmp_path: Path
    ) -> None:
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

    def test_allows_custom_reviewer_name(
        self, tmp_path: Path
    ) -> None:
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

    def test_rejects_missing_required_finding_field(
        self, tmp_path: Path
    ) -> None:
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

    def test_omitted_optional_fields_passes(
        self, tmp_path: Path
    ) -> None:
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
