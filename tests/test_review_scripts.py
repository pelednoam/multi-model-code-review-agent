"""Tests for ensemble review infrastructure scripts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
import re
import subprocess
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace

from scripts.review_loop.artifacts import describe_outputs

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

    def test_redacts_the_whole_private_key_not_just_its_banner(self) -> None:
        """The body is the key. Redacting only the banner leaks it intact."""
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ"
        diff = (
            "diff --git a/k.pem b/k.pem\n"
            "+-----BEGIN RSA PRIVATE KEY-----\n"
            f"+{body}\n"
            "+-----END RSA PRIVATE KEY-----\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert body not in stdout
        assert stdout.count("REDACTED") == 3
        assert code == 1

    @pytest.mark.parametrize(
        "kind",
        ["RSA", "OPENSSH", "EC", "DSA", "ENCRYPTED", "PGP"],
    )
    def test_every_private_key_banner_opens_the_block(self, kind: str) -> None:
        """One narrow banner pattern and one broad one let OPENSSH keys through.

        The block opened only for a banner the credential patterns had already
        redacted, and `BEGIN OPENSSH PRIVATE KEY` matched only the broad one --
        so nothing was redacted, the block never opened, and the scrubber
        exited 0 calling the key clean.
        """
        body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB"
        diff = (
            "diff --git a/k b/k\n"
            f"+-----BEGIN {kind} PRIVATE KEY-----\n"
            f"+{body}\n"
            f"+-----END {kind} PRIVATE KEY-----\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert body not in stdout
        assert code == 1

    def test_a_pgp_key_block_banner_is_recognised(self) -> None:
        """Its banner says BLOCK after KEY, which the anchorless search allows."""
        body = "lQOYBGYAAAABCADQ1example"
        diff = (
            "diff --git a/k b/k\n"
            "+-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
            f"+{body}\n"
            "+-----END PGP PRIVATE KEY BLOCK-----\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert body not in stdout
        assert code == 1

    def test_a_key_block_does_not_swallow_the_rest_of_the_diff(self) -> None:
        diff = (
            "diff --git a/k.pem b/k.pem\n"
            "+-----BEGIN PRIVATE KEY-----\n"
            "+AAAA\n"
            "+-----END PRIVATE KEY-----\n"
            "+after the key\n"
        )
        stdout, _, _ = self._run_scrub(diff)
        assert "+after the key" in stdout

    def test_an_unterminated_key_block_ends_at_the_next_file(self) -> None:
        """A truncated key must not redact every following file wholesale."""
        diff = (
            "diff --git a/k.pem b/k.pem\n"
            "+-----BEGIN PRIVATE KEY-----\n"
            "+AAAA\n"
            "diff --git a/ok.py b/ok.py\n"
            "+def hello(): pass\n"
        )
        stdout, _, _ = self._run_scrub(diff)
        assert "+def hello(): pass" in stdout

    def test_a_fixture_key_banner_in_a_safe_file_opens_no_block(self) -> None:
        diff = (
            "diff --git a/scripts/scrub_diff.py b/scripts/scrub_diff.py\n"
            "+-----BEGIN PRIVATE KEY-----\n"
            '+re.compile(r"x")\n'
        )
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" not in stdout
        assert code == 0

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
        diff = '+secret_key = "abc123def456"\n'
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
        diff = (
            '+API_KEY = "sk-abc123def456ghi"\n'
            '+password = "hunter22"\n'
            "+token=ghp_abcdefghij1234567890\n"
        )
        _, stderr, _ = self._run_scrub(diff)
        assert "3 line(s) redacted" in stderr

    def test_does_not_redact_a_domain_word_called_token(self) -> None:
        """A token is a game object in Magic, a node in a parser, a colour in a
        design system. Flagging the word aborts those projects' reviews entirely.
        """
        diff = (
            "+        case CreateTokens(count=count, token=token):\n"
            "+def encode_token(token: TokenSpec) -> JsonObject:\n"
            "+    token: TokenSpec\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" not in stdout
        assert code == 0

    def test_does_not_redact_type_annotations(self) -> None:
        diff = "+    api_key: str\n+    password: str | None = None\n"
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" not in stdout
        assert code == 0

    def test_does_not_redact_reading_a_secret_from_the_environment(self) -> None:
        """Naming a secret is not leaking one."""
        diff = "+access_token = os.environ['TOKEN']\n"
        stdout, _, _code = self._run_scrub(diff)
        assert "REDACTED" not in stdout

    def test_still_redacts_a_quoted_credential(self) -> None:
        diff = '+token: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"\n'
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_still_redacts_an_unquoted_credential(self) -> None:
        diff = "+token=ghp_abcdefghij1234567890abcdef\n"
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout
        assert code == 1

    def test_does_not_match_dotenv_in_prose(self) -> None:
        diff = "+# See the .environment docs for details\n"
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" not in stdout
        assert code == 0

    def test_redacts_a_dotenv_path_added_at_column_zero(self) -> None:
        """The `+` prefix is not part of the file's text and must not hide it."""
        stdout, _, code = self._run_scrub("+.env.production\n")
        assert "REDACTED" in stdout
        assert code == 1

    def test_a_combined_diff_header_does_not_inherit_a_safe_file(self) -> None:
        """`in_safe` used to latch: only `diff --git` reset it."""
        diff = (
            "diff --git a/scripts/scrub_diff.py b/scripts/scrub_diff.py\n"
            '+re.compile(r"x")\n'
            "diff --cc other.py\n"
            '+re.compile(r"y")  # api_key = "supersecret1"\n'
        )
        stdout, _, code = self._run_scrub(diff)
        assert "supersecret1" not in stdout
        assert code == 1

    def test_a_non_utf8_byte_does_not_truncate_the_patch(self) -> None:
        """A crash mid-stream leaves a plausible patch whose tail is unscrubbed."""
        raw = b'diff --git a/x b/x\n+caf\xe9 api_key = "abcdefghij1"\n+trailing line\n'
        result = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT)],
            input=raw,
            capture_output=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C", "PYTHONIOENCODING": "ascii"},
        )
        stdout = result.stdout.decode("utf-8", "replace")
        assert "abcdefghij1" not in stdout
        assert "+trailing line" in stdout
        assert result.returncode == 1

    def test_a_dotenv_file_header_is_not_redacted(self) -> None:
        """`--- a/x` and `+++ b/x` start with `-` and `+`, but are structure.

        Redacting them cost the patch its file attribution *and* aborted the
        whole review, because someone committed a `.env.example`.
        """
        diff = (
            "diff --git a/.env.example b/.env.example\n"
            "index 0000000..1111111 100644\n"
            "--- a/.env.example\n"
            "+++ b/.env.example\n"
            "+API_HOST=localhost\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert stdout == diff
        assert code == 0

    def test_a_dotenv_value_inside_the_file_is_still_redacted(self) -> None:
        stdout, _, code = self._run_scrub("+source .env.production\n")
        assert "REDACTED" in stdout
        assert code == 1

    @pytest.mark.parametrize(
        "key",
        [
            "sk-ant-api03-AbCdEf1234567890GhIjKlMnOpQrStUvWxYz",
            "sk-proj-AbCdEf1234567890GhIjKlMnOpQrStUvWxYz",
            "sk-AbCdEf1234567890GhIjKlMnOpQrStUvWxYz",
        ],
    )
    def test_redacts_current_key_shapes(self, key: str) -> None:
        """A run of plain alphanumerics stops at the first hyphen."""
        stdout, _, code = self._run_scrub(f'+  "{key}"\n')
        assert key not in stdout
        assert code == 1

    def test_a_credential_beside_a_regex_mention_is_still_scrubbed(self) -> None:
        """The safe-file bypass used to fire on `re.compile(` anywhere."""
        diff = (
            "diff --git a/scripts/scrub_diff.py b/scripts/scrub_diff.py\n"
            '+AWS = "AKIAIOSFODNN7EXAMPLE"  # matched by re.compile(...)\n'
        )
        stdout, _, code = self._run_scrub(diff)
        assert "AKIAIOSFODNN7EXAMPLE" not in stdout
        assert code == 1

    def test_a_removed_comment_line_is_not_mistaken_for_a_header(self) -> None:
        """`-- password = ...` removed arrives as `--- password = ...`.

        Indistinguishable from a file header by prefix alone, and classifying it
        as metadata wrote the credential straight through with exit 0.
        """
        diff = (
            "diff --git a/q.sql b/q.sql\n"
            "--- a/q.sql\n"
            "+++ b/q.sql\n"
            "@@ -1 +1 @@\n"
            '-- password = "hunter2abc"\n'
            "+SELECT 1\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert "hunter2abc" not in stdout
        assert code == 1

    def test_an_added_comment_line_is_not_mistaken_for_a_header_either(self) -> None:
        diff = (
            "diff --git a/q.sql b/q.sql\n"
            "--- a/q.sql\n"
            "+++ b/q.sql\n"
            "@@ -1 +1 @@\n"
            '+++ password = "hunter2abc"\n'
        )
        stdout, _, code = self._run_scrub(diff)
        assert "hunter2abc" not in stdout
        assert code == 1

    def test_the_real_file_headers_are_still_left_alone(self) -> None:
        diff = (
            "diff --git a/.env.example b/.env.example\n"
            "--- a/.env.example\n"
            "+++ b/.env.example\n"
            "@@ -1 +1 @@\n"
            "+API_HOST=localhost\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert stdout == diff
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


def _two_commit_repo(repo: Path) -> Path:
    """A git repo with two commits, so HEAD~1 resolves."""
    repo.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
    for n, body in enumerate(("a = 1\n", "a = 2\n")):
        (repo / "x.py").write_text(body)
        subprocess.run(["git", "add", "x.py"], cwd=repo, check=True, env=env)
        subprocess.run(
            ["git", "commit", "-q", "-m", str(n)], cwd=repo, check=True, env=env
        )
    return repo


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

    def test_collect_diff_distinguishes_a_crash_from_a_clean_block(
        self, tmp_path: Path
    ) -> None:
        """Exit 2 means the patch is truncated, not that a secret leaked."""
        from unittest.mock import patch

        from scripts.review_loop.diff import ScrubberFailedError, collect_diff

        round_dir = tmp_path / "round"
        round_dir.mkdir()
        repo = _two_commit_repo(tmp_path / "repo")

        fake_scrubber = tmp_path / "scrub_diff.py"
        fake_scrubber.write_text(
            'import sys\nprint("aborted", file=sys.stderr)\nsys.exit(2)\n'
        )
        with patch("scripts.review_loop.diff.SCRIPTS_DIR", tmp_path):
            with pytest.raises(ScrubberFailedError) as exc_info:
                collect_diff(round_dir, repo)
        assert "truncated" in str(exc_info.value)
        assert "rotated" not in str(exc_info.value)


@dataclass(slots=True)
class _Dead:
    """A process id nothing can be read for."""

    pid: int = -1


@dataclass(slots=True)
class _Busy:
    """A process whose CPU time can be read: this one's."""

    pid: int = field(default_factory=os.getpid)


class TestReviewerProgress:
    """Knowing which reviewers are still working, without guessing."""

    def test_a_working_reviewer_is_shown_with_what_it_has_written(
        self, tmp_path: Path
    ) -> None:
        """Output size is the signal the streaming backends give."""
        from scripts.review_loop.reviewers import _progress

        (tmp_path / "stderr-2.txt").write_text("x" * 2048)
        procs = [(1, _Dead(), "claude", None, None), (2, _Dead(), "codex", None, None)]
        line = _progress(procs, {2: procs[1]}, tmp_path, 95.0, {})
        assert "R1(claude) done" in line
        assert "R2(codex) 2K" in line
        assert "1m35s" in line

    def test_a_reviewer_that_has_written_nothing_says_so(self, tmp_path: Path) -> None:
        from scripts.review_loop.reviewers import _progress

        procs = [(1, _Dead(), "gemini", None, None)]
        assert "R1(gemini) 0B" in _progress(procs, {1: procs[0]}, tmp_path, 5.0, {})

    def test_a_buffered_backend_is_shown_busy_by_its_cpu_time(
        self, tmp_path: Path
    ) -> None:
        """`claude -p` writes nothing until it finishes, so size says nothing.

        From start to end its output is 0 bytes, and a line that reports only
        size cannot tell "thinking" from "hung" -- which is most of the run.
        """
        from scripts.review_loop.reviewers import _progress

        procs = [(1, _Busy(), "claude", None, None)]
        line = _progress(procs, {1: procs[0]}, tmp_path, 30.0, {1: 0.0})
        assert "R1(claude) 0B busy" in line

    def test_a_quiet_process_is_not_called_busy(self, tmp_path: Path) -> None:
        """Waiting on a network reply is most of what these do."""
        from scripts.review_loop.reviewers import _progress

        procs = [(1, _Busy(), "claude", None, None)]
        line = _progress(procs, {1: procs[0]}, tmp_path, 30.0, {1: 999.0})
        assert "busy" not in line

    def test_cpu_time_is_optional(self, tmp_path: Path) -> None:
        """Linux only. Everywhere else the size signal still works."""
        from scripts.review_loop.reviewers import _cpu_seconds

        assert _cpu_seconds(-1) is None

    def test_codex_progress_is_counted_from_stderr(self, tmp_path: Path) -> None:
        """It reports progress on stderr and its result on stdout, so stdout
        stays empty until the very end -- and looked hung for fifteen minutes.
        """
        from scripts.review_loop.reviewers import _bytes_written

        (tmp_path / "raw-2.txt").write_text("")
        (tmp_path / "stderr-2.txt").write_text("y" * 500)
        assert _bytes_written(tmp_path, 2) == 500


class TestEmptyReviews:
    """A reviewer that found nothing has not reviewed anything."""

    def test_a_silent_reviewer_is_not_counted_as_a_clean_bill(self) -> None:
        from scripts.review_until_converged import _describe_results

        results = [
            {"findings": [{"severity": "warning"}]},
            None,
            {"findings": []},
            {"findings": [{"severity": "warning"}]},
        ]
        line = _describe_results(results, 5000)
        assert "3/4 reviewers returned output" in line
        assert "R3 found nothing" in line
        assert "not a clean bill" in line

    def test_a_small_diff_with_no_findings_is_unremarkable(self) -> None:
        from scripts.review_until_converged import _describe_results

        line = _describe_results([{"findings": []}], 12)
        assert "found nothing" in line
        assert "not a clean bill" not in line

    def test_everyone_speaking_needs_no_caveat(self) -> None:
        from scripts.review_until_converged import _describe_results

        results = [{"findings": [{"severity": "warning"}]}] * 4
        assert (
            _describe_results(results, 5000) == "Results: 4/4 reviewers returned output"
        )


class TestDiffBudget:
    """Naming a generated file that is crowding the review out."""

    def _diff(self, *files: tuple[str, int]) -> str:
        parts = [
            f"diff --git a/{name} b/{name}\n" + "x\n" * lines for name, lines in files
        ]
        return "".join(parts)

    def test_a_lockfile_dominating_the_diff_is_named(self) -> None:
        """It was 69% of a real review: four models paid to read a lockfile."""
        from scripts.review_loop.diff import dominant_files

        (note,) = dominant_files(
            self._diff(("package-lock.json", 9000), ("app.py", 4000))
        )
        assert "package-lock.json is 69% of this diff" in note
        assert "linguist-generated=true" in note

    def test_a_small_diff_is_left_alone(self) -> None:
        """Half of sixty lines is a small change, not a problem."""
        from scripts.review_loop.diff import dominant_files

        assert dominant_files(self._diff(("a.py", 30), ("b.py", 30))) == []

    def test_a_balanced_large_diff_is_left_alone(self) -> None:
        from scripts.review_loop.diff import dominant_files

        assert (
            dominant_files(self._diff(("a.py", 900), ("b.py", 900), ("c.py", 900)))
            == []
        )

    def test_an_empty_diff_says_nothing(self) -> None:
        from scripts.review_loop.diff import dominant_files

        assert dominant_files("") == []


class TestReviewerDeadline:
    """How long a reviewer gets, and what happens when it runs out."""

    def test_a_bigger_diff_earns_more_time(self) -> None:
        """The reviewer that runs out is the one with the most to say."""
        from scripts.review_loop.config import reviewer_timeout

        assert reviewer_timeout(5000) > reviewer_timeout(200)

    def test_there_is_a_ceiling(self) -> None:
        from scripts.review_loop.config import REVIEWER_TIMEOUT_MAX, reviewer_timeout

        assert reviewer_timeout(10_000_000) == REVIEWER_TIMEOUT_MAX

    def test_the_environment_can_override_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_loop.config import reviewer_timeout

        monkeypatch.setenv("REVIEWER_TIMEOUT", "42")
        assert reviewer_timeout(5000) == 42

    def test_a_result_that_lands_after_the_deadline_is_recognised(
        self, tmp_path: Path
    ) -> None:
        """A killed reviewer's helper can outlive it and write minutes later."""
        from scripts.review_loop.reviewers import _has_result

        assert not _has_result(tmp_path, 2)
        (tmp_path / "result-2.json").write_text("{}")
        assert _has_result(tmp_path, 2)

    def test_an_empty_result_file_is_not_a_result(self, tmp_path: Path) -> None:
        from scripts.review_loop.reviewers import _has_result

        (tmp_path / "result-2.json").write_text("")
        assert not _has_result(tmp_path, 2)


class TestSalvagingProse:
    """A reviewer that answers in prose has still reviewed something."""

    def test_a_prose_reply_is_put_in_front_of_the_operator(
        self, tmp_path: Path
    ) -> None:
        """This is not hypothetical: one was binned carrying a real finding.

        It noticed twelve duplicated tests, wrote it as a paragraph instead of
        the schema, and the round recorded it as having returned nothing.
        """
        (tmp_path / "raw-3.txt").write_text(
            json.dumps({"result": "test_app_sockets.py duplicates 12 tests verbatim."})
        )
        summary = describe_outputs(tmp_path)
        assert "NOT JSON" in summary
        assert "duplicates 12 tests verbatim" in summary
        assert "raw-3.txt" in summary

    def test_a_reviewer_that_wrote_nothing_is_reported_differently(
        self, tmp_path: Path
    ) -> None:
        """ "No output" and "unusable output" read as one thing and are not."""
        summary = describe_outputs(tmp_path)
        assert "NO OUTPUT" in summary
        assert "NOT JSON" not in summary

    def test_an_empty_raw_file_counts_as_nothing(self, tmp_path: Path) -> None:
        """codex writes straight to result-N.json, so its raw file is empty."""
        (tmp_path / "raw-2.txt").write_text("")
        assert "NO OUTPUT" in describe_outputs(tmp_path)

    def test_bare_prose_is_salvaged_too(self, tmp_path: Path) -> None:
        """Not every backend wraps its reply in an envelope."""
        (tmp_path / "raw-1.txt").write_text("The diff looks fine to me.")
        assert "looks fine to me" in describe_outputs(tmp_path)

    def test_a_long_reply_is_trimmed_not_dumped(self, tmp_path: Path) -> None:
        (tmp_path / "raw-1.txt").write_text("x" * 5000)
        assert len(describe_outputs(tmp_path)) < 2000

    def test_a_reviewer_with_findings_is_left_alone(self, tmp_path: Path) -> None:
        (tmp_path / "result-1.json").write_text("{}")
        summary = describe_outputs(tmp_path, n_slots=1)
        assert "result-{1}.json" in summary
        assert "NOT JSON" not in summary


class TestSchemaInstruction:
    """Telling a reviewer what to do when it has nothing to say."""

    def test_the_prompt_says_prose_is_discarded(self) -> None:
        """The commonest failure is not silence; it is an unparseable answer."""
        from scripts.review_loop.reviewers import build_reviewer_prompt

        prompt = build_reviewer_prompt(
            "security", "leaks", "R1", "opus", "+x", "{}", "c"
        )
        assert "empty list" in prompt
        assert "discarded unread" in prompt


class TestProjectGate:
    """Whose gate the loop runs, and with which Python."""

    def test_a_project_with_its_own_gate_script_gets_only_that(
        self, tmp_path: Path
    ) -> None:
        """The built-in `mypy scripts/` type-checks the agent's vendored source.

        Under a host project's own strict settings that always fails, so the
        loop could never commit a round no matter what it fixed.
        """
        from scripts.review_loop.config import project_gate

        repo = tmp_path / "host"
        (repo / "tools").mkdir(parents=True)
        gate = repo / "tools" / "gate.sh"
        gate.write_text("#!/bin/sh\nexit 0\n")
        gate.chmod(0o755)
        assert project_gate(repo) == gate

    def test_a_gate_script_that_is_not_executable_is_not_used(
        self, tmp_path: Path
    ) -> None:
        from scripts.review_loop.config import project_gate

        repo = tmp_path / "host"
        (repo / "tools").mkdir(parents=True)
        (repo / "tools" / "gate.sh").write_text("#!/bin/sh\nexit 0\n")
        assert project_gate(repo) is None

    def test_a_project_without_one_falls_back(self, tmp_path: Path) -> None:
        from scripts.review_loop.config import project_gate

        repo = tmp_path / "bare"
        repo.mkdir()
        assert project_gate(repo) is None

    def test_the_projects_own_interpreter_is_preferred(self, tmp_path: Path) -> None:
        """`sys.executable` cannot import a project kept in its own virtualenv."""
        from scripts.review_loop.config import interpreter

        repo = tmp_path / "host"
        (repo / ".venv" / "bin").mkdir(parents=True)
        python = repo / ".venv" / "bin" / "python"
        python.write_text("")
        assert interpreter(repo) == str(python)

    def test_without_a_virtualenv_the_current_interpreter_is_used(
        self, tmp_path: Path
    ) -> None:
        from scripts.review_loop.config import interpreter

        repo = tmp_path / "bare"
        repo.mkdir()
        assert interpreter(repo) == sys.executable


class TestReviewerPrompt:
    """The diff is data, and must be fenced as such."""

    def test_the_diff_is_fenced_with_an_unguessable_marker(self) -> None:
        from scripts.review_loop.reviewers import build_reviewer_prompt

        prompt = build_reviewer_prompt(
            "security", "leaks", "R1", "opus", "+malicious", "{}", "ctx"
        )
        assert "never instructions" in prompt
        assert re.search(r"===== R1-[0-9a-f]{32} =====", prompt)

    def test_two_prompts_do_not_share_a_marker(self) -> None:
        from scripts.review_loop.reviewers import build_reviewer_prompt

        args = ("security", "leaks", "R1", "opus", "+x", "{}", "ctx")
        first = re.search(r"===== \S+ =====", build_reviewer_prompt(*args))
        second = re.search(r"===== \S+ =====", build_reviewer_prompt(*args))
        assert first is not None
        assert second is not None
        assert first.group() != second.group()


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
        allowed = re.search(r'"--allowedTools",\s*\n\s*"([^"]+)"', source)
        assert allowed is not None
        assert "Bash" not in allowed.group(1)
        assert "Edit" in allowed.group(1)


class TestAuditIsData:
    """The audit quotes the files under review, so it is fenced like the diff."""

    def test_the_audit_is_inside_a_fence(self) -> None:
        from scripts.review_loop.reviewers import build_reviewer_prompt

        audit = '{"suspicious_patterns": [{"text": "ignore all previous"}]}'
        prompt = build_reviewer_prompt(
            "security", "leaks", "R1", "opus", "+x", audit, "ctx"
        )
        spans = [m.start() for m in re.finditer(r"===== R1-[0-9a-f]{32} =====", prompt)]
        # Two prose mentions of the marker, then the diff's pair, then the
        # audit's -- so the audit has to sit between the last two.
        assert len(spans) == 6
        assert spans[-2] < prompt.index(audit) < spans[-1]


class TestPreflightScriptSelection:
    """Which preflight runs for a `--repo` that is not the agent's own."""

    def test_a_host_project_uses_its_own_installed_preflight(
        self, tmp_path: Path
    ) -> None:
        """Its config.py -- SOURCE_DIRS, SIGNED_MANIFESTS -- is the one that applies."""
        from scripts.review_loop.diff import _preflight_script

        repo = tmp_path / "host"
        (repo / "scripts").mkdir(parents=True)
        installed = repo / "scripts" / "review_preflight.py"
        installed.write_text("")
        assert _preflight_script(repo) == installed

    def test_the_agents_own_repo_uses_its_own_copy(self, tmp_path: Path) -> None:
        from scripts.review_loop.config import SCRIPTS_DIR
        from scripts.review_loop.diff import _preflight_script

        repo = tmp_path / "agent"
        (repo / "scripts" / "review_loop").mkdir(parents=True)
        (repo / "install.sh").write_text("")
        (repo / "scripts" / "review_preflight.py").write_text("")
        assert _preflight_script(repo) == SCRIPTS_DIR / "review_preflight.py"

    def test_a_project_without_an_installed_copy_falls_back(
        self, tmp_path: Path
    ) -> None:
        from scripts.review_loop.config import SCRIPTS_DIR
        from scripts.review_loop.diff import _preflight_script

        repo = tmp_path / "bare"
        repo.mkdir()
        assert _preflight_script(repo) == SCRIPTS_DIR / "review_preflight.py"


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


class TestCoverageSourceSpec:
    """The coverage gate must actually collect data, not fail open.

    coverage.py resolves a --cov value as an importable package name or a
    directory. It cannot use an individual .py path. When changed files were
    passed as sources, coverage collected nothing and wrote no JSON report, so
    measure_test_coverage() returned [] -- indistinguishable from "no gaps".
    These tests pin both halves: the argv shape, and an end-to-end measurement
    that must find a real gap.
    """

    def test_cov_sources_are_directories_not_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.preflight import coverage as cov_mod

        (tmp_path / "src").mkdir()
        monkeypatch.setattr(cov_mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cov_mod, "SOURCE_DIRS", ["src/"])

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            captured.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cov_mod.subprocess, "run", fake_run)
        cov_mod._run_pytest_with_coverage([])

        assert captured, "pytest was never invoked"
        cov_args = [a for a in captured[0] if a.startswith("--cov=")]
        assert cov_args == ["--cov=src"]
        assert not any(a.endswith(".py") for a in cov_args), (
            "a .py path as a coverage source collects nothing"
        )

    def test_no_source_dirs_on_disk_warns_instead_of_failing_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.preflight import coverage as cov_mod

        monkeypatch.setattr(cov_mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cov_mod, "SOURCE_DIRS", ["nonexistent/"])

        warnings: list[str] = []
        assert cov_mod._run_pytest_with_coverage(warnings) is None
        assert len(warnings) == 1
        assert "coverage gate inoperative" in warnings[0]

    def test_end_to_end_gap_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The test that would have caught the original bug.

        A real project tree with a genuinely uncovered branch must produce a
        gap. Before the fix this returned [] and the branch went unreported.
        """
        from scripts.preflight import coverage as cov_mod

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mod.py").write_text(
            "def classify(n: int) -> str:\n"
            "    if n > 0:\n"
            "        return 'positive'\n"
            "    return 'other'\n"
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_mod.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))\n"
            "from mod import classify\n"
            "\n"
            "def test_positive() -> None:\n"
            "    assert classify(1) == 'positive'\n"
        )

        monkeypatch.setattr(cov_mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cov_mod, "SOURCE_DIRS", ["src/"])
        monkeypatch.setattr(cov_mod, "COVERAGE_TARGET", 100)

        warnings: list[str] = []
        gaps = cov_mod.measure_test_coverage(["src/mod.py"], warnings)

        assert warnings == []
        assert len(gaps) == 1, f"expected one gap, got {gaps}"
        assert gaps[0]["file"] == "src/mod.py"
        assert gaps[0]["percent_covered"] < 100
        assert gaps[0]["uncovered_lines"], "the untaken return was not reported"


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


class TestChangedFilesFallback:
    """The audit must not report an empty tree just because origin/main is absent."""

    def _repo(self, tmp_path: Path, n_commits: int) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=_GIT_ENV
        )
        for i in range(n_commits):
            (repo / "x.py").write_text(f"a = {i}\n")
            subprocess.run(["git", "add", "x.py"], cwd=repo, check=True, env=_GIT_ENV)
            subprocess.run(
                ["git", "commit", "-q", "-m", f"c{i}"],
                cwd=repo,
                check=True,
                env=_GIT_ENV,
            )
        return repo

    def test_falls_back_to_head_parent_without_origin_main(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.preflight import git_state

        monkeypatch.setattr(git_state, "REPO_ROOT", self._repo(tmp_path, 2))
        warnings: list[str] = []
        assert git_state._changed_vs_base(warnings) == "x.py"
        assert warnings == []

    def test_warns_when_no_base_resolves_at_all(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.preflight import git_state

        monkeypatch.setattr(git_state, "REPO_ROOT", self._repo(tmp_path, 1))
        warnings: list[str] = []
        assert git_state._changed_vs_base(warnings) == ""
        assert len(warnings) == 1
        assert "could not resolve changed files" in warnings[0]


class TestVendoredExclusion:
    """A host project's review must not be spent on the reviewer's own source."""

    def _host_repo(self, tmp_path: Path) -> Path:
        """A host project with one vendored file and one of its own."""
        repo = tmp_path / "host"
        (repo / "scripts").mkdir(parents=True)
        (repo / "app").mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=_GIT_ENV
        )
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_GIT_ENV)
        subprocess.run(
            ["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, env=_GIT_ENV
        )
        (repo / "scripts" / "review_preflight.py").write_text("# vendored\n")
        (repo / "app" / "main.py").write_text("value = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_GIT_ENV)
        subprocess.run(
            ["git", "commit", "-q", "-m", "work"], cwd=repo, check=True, env=_GIT_ENV
        )
        return repo

    def test_agent_repo_still_reviews_itself(self, tmp_path: Path) -> None:
        from scripts.review_loop.config import diff_pathspec, is_agent_repo

        repo = tmp_path / "agent"
        (repo / "scripts" / "review_loop").mkdir(parents=True)
        (repo / "install.sh").write_text("#!/bin/sh\n")

        assert is_agent_repo(repo)
        assert diff_pathspec(repo) == ["."]

    def test_host_project_excludes_the_vendored_paths(self, tmp_path: Path) -> None:
        from scripts.review_loop.config import (
            VENDORED_PATHS,
            diff_pathspec,
            is_agent_repo,
        )

        host = tmp_path / "host"
        host.mkdir()

        assert not is_agent_repo(host)
        spec = diff_pathspec(host)
        assert spec[0] == "."
        assert len(spec) == len(VENDORED_PATHS) + 1
        assert ":(exclude)scripts/review_loop" in spec

    def test_collect_diff_omits_vendored_but_keeps_project_code(
        self, tmp_path: Path
    ) -> None:
        """The end-to-end shape: the reviewer sees app/, never scripts/."""
        from unittest.mock import patch

        from scripts.review_loop.diff import collect_diff

        repo = self._host_repo(tmp_path)
        round_dir = tmp_path / "round"
        round_dir.mkdir()

        with patch("scripts.review_loop.diff.SCRIPTS_DIR", REPO_ROOT / "scripts"):
            diff_path, _n_lines = collect_diff(round_dir, repo)

        diff = diff_path.read_text()
        assert "app/main.py" in diff
        assert "scripts/review_preflight.py" not in diff


class TestVendoredPathsMatchInstaller:
    """VENDORED_PATHS is a hand-maintained mirror of install.sh. Pin it.

    If the installer starts copying a new file and this list is not updated,
    that file silently becomes part of every host project's review diff again.
    """

    def _installed_paths(self) -> set[str]:
        """The TARGET-relative paths install.sh writes."""
        installed: set[str] = set()
        for line in (REPO_ROOT / "install.sh").read_text().splitlines():
            line = line.strip()
            if not line.startswith(("cp ", "rsync ")):
                continue
            quoted = re.findall(r'"([^"]+)"', line)
            src = next((q for q in quoted if "$REPO_ROOT/" in q), None)
            dst = next((q for q in quoted if "$TARGET/" in q), None)
            if src is None or dst is None:
                continue
            rel_dst = dst.split("$TARGET/", 1)[1]
            if rel_dst.endswith("/") and not line.startswith("rsync "):
                rel_dst += src.rsplit("/", 1)[-1]
            installed.add(rel_dst.rstrip("/"))
        return installed

    def test_no_drift_between_installer_and_exclusion_list(self) -> None:
        from scripts.review_loop.config import VENDORED_PATHS

        assert self._installed_paths() == set(VENDORED_PATHS)


class TestCodexEnvOverrides:
    """CODEX_MODEL and CODEX_REASONING_EFFORT pass through to codex exec."""

    # The two safety flags are always present, whatever the env says. A reviewer that can
    # edit the code it reviews breaks this tool's central promise, and `-s read-only` alone
    # is not enough: a global `approvals_reviewer = "auto_review"` overrides it.
    SAFETY = ["-s", "read-only", "-c", 'approvals_reviewer="user"']

    def test_no_env_still_pins_the_safety_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop.reviewers import _codex_extra_args

        monkeypatch.delenv("CODEX_MODEL", raising=False)
        monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
        assert _codex_extra_args() == self.SAFETY

    def test_env_cannot_drop_the_read_only_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop.reviewers import _codex_extra_args

        # Even a user trying to widen the sandbox through the model knobs keeps read-only.
        monkeypatch.setenv("CODEX_MODEL", "gpt-5.5-codex")
        monkeypatch.setenv("CODEX_REASONING_EFFORT", "high")
        args = _codex_extra_args()
        assert args[:2] == ["-s", "read-only"]
        assert 'approvals_reviewer="user"' in args

    def test_model_env_adds_dash_m(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop.reviewers import _codex_extra_args

        monkeypatch.setenv("CODEX_MODEL", "gpt-5.5-codex")
        monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
        assert _codex_extra_args() == [*self.SAFETY, "-m", "gpt-5.5-codex"]

    def test_reasoning_high_is_pro_equivalent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_loop.reviewers import _codex_extra_args

        monkeypatch.delenv("CODEX_MODEL", raising=False)
        monkeypatch.setenv("CODEX_REASONING_EFFORT", "high")
        # The bash equivalent is `-c model_reasoning_effort=high` and
        # matches what the ChatGPT web UI labels as "GPT-5.5 Pro".
        assert _codex_extra_args() == [*self.SAFETY, "-c", "model_reasoning_effort=high"]

    def test_both_env_vars_compose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop.reviewers import _codex_extra_args

        monkeypatch.setenv("CODEX_MODEL", "gpt-5.5")
        monkeypatch.setenv("CODEX_REASONING_EFFORT", "high")
        args = _codex_extra_args()
        assert "-m" in args and "gpt-5.5" in args
        assert "-c" in args and "model_reasoning_effort=high" in args


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

    AGENT_DOC = REPO_ROOT / "docs" / "ensemble-review.md"

    def _runner(self) -> str:
        import re

        doc = self.AGENT_DOC.read_text()
        m = re.search(
            r"cat > \"\$REVIEW_TMP/run-reviewers\.sh\" <<'RUNNER'\n(.*?)\nRUNNER\n",
            doc,
            re.S,
        )
        assert m, "section 6 no longer defines a run-reviewers.sh heredoc"
        return m.group(1)

    def test_runner_is_valid_bash(self, tmp_path: Path) -> None:
        script = tmp_path / "run-reviewers.sh"
        script.write_text(self._runner() + "\n")
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"runner does not parse: {result.stderr}"

    def test_launch_and_wait_live_in_the_same_script(self) -> None:
        # Both halves in one heredoc is the whole fix: `wait` and `kill -0` only work on children
        # of the shell that started them, and shell state does not survive between tool calls.
        runner = self._runner()
        assert "launch_cli" in runner and "launch_hermes" in runner
        assert "ROUND_DEADLINE" in runner
        assert "ROUND COMPLETE" in runner

    def test_the_doc_tells_the_agent_to_run_it_in_the_background(self) -> None:
        doc = self.AGENT_DOC.read_text()
        assert "run_in_background: true" in doc
        # And says why, so the next person to touch it does not "simplify" it back.
        assert "120 second timeout" in doc

    def test_waiting_is_bounded(self) -> None:
        # A bare `wait` has no deadline of its own, so one wedged reviewer holds the round open
        # forever and the other three results are never reported.
        runner = self._runner()
        assert "\nwait\n" not in runner, (
            "bare `wait` is back: one wedged reviewer hangs the round"
        )
        assert "WATCHDOG" in runner

    def test_every_reviewer_records_a_pid_and_an_exit_status(self) -> None:
        runner = self._runner()
        # Four slots, each recording both, is what makes progress observable from another shell.
        assert runner.count('echo $! > "$REVIEW_TMP/pid-$num"') == 4
        assert runner.count('echo $? > "$REVIEW_TMP/done-$num"') == 4
        # The status must be written by the subshell that ran the reviewer. A separate waiter
        # cannot: `wait` refuses a pid that is not its own child, so it would record a bogus
        # status immediately while the reviewer was still running.
        assert "wait $!" not in runner

    def test_reviewers_are_killable_as_a_tree(self) -> None:
        runner = self._runner()
        # `set -m` gives each background job its own process group; without it a reviewer CLI's
        # helper processes survive the round, still burning provider quota and still writing into
        # $REVIEW_TMP after the report is written.
        assert "\nset -m" in runner
        assert 'kill -- "-$(cat "$REVIEW_TMP/pid-$num")"' in runner
        # SIGKILL escalation, because a CLI that traps SIGTERM otherwise outlives the round.
        assert runner.count('timeout -k 10 "$REVIEWER_TIMEOUT"') == 4

    def test_a_failed_slot_still_produces_a_result_with_a_true_reason(self) -> None:
        runner = self._runner()
        # Every slot must yield a result-N.json so the report can be honest about what is missing,
        # and the reason must distinguish the cases a reader would act on differently.
        assert "124|137" in runner, (
            "a reviewer killed by its own timeout is reported as a parse error"
        )
        assert "never launched" in runner
        assert "killed by the watchdog" in runner


class TestCodexSandboxVerification:
    """An unconfined codex must be DROPPED, not used.

    The failure this guards against is specific: a reviewer that silently has write access
    to the repo it is reviewing. It has happened, from two unrelated causes (a kernel that
    blocks bubblewrap's user namespaces, and a global approvals_reviewer that escalates
    past -s read-only), so confinement is proven per-run rather than assumed.
    """

    def test_unconfined_codex_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop import backends

        monkeypatch.delenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", raising=False)
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(backends, "codex_is_confined", lambda: (False, "sandbox allowed a write"))
        assert backends.detect_backends()["codex"] is False
        # The other backends are unaffected by codex's verdict.
        assert backends.detect_backends()["claude"] is True

    def test_confined_codex_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop import backends

        monkeypatch.delenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", raising=False)
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(backends, "codex_is_confined", lambda: (True, "verified"))
        assert backends.detect_backends()["codex"] is True

    def test_check_can_be_skipped_for_externally_sandboxed_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_loop import backends

        monkeypatch.setenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", "1")
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")

        def _boom() -> tuple[bool, str]:  # must not even be called
            raise AssertionError("confinement check ran despite the skip flag")

        monkeypatch.setattr(backends, "codex_is_confined", _boom)
        assert backends.detect_backends()["codex"] is True

    def test_a_sandbox_that_cannot_start_is_not_treated_as_confined(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ambiguity at the heart of this bug.

        If the sandbox never launches, the canary survives untouched -- which looks exactly
        like a correctly refused write. Reporting that as "confined" is how an unconfined
        reviewer gets trusted, so a sandbox that cannot start must fail the check.
        """
        from scripts.review_loop import backends

        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(
            backends,
            "_run",
            lambda *a, **k: SimpleNamespace(stdout="", stderr="bwrap: setting up uid map: Permission denied"),
        )
        ok, reason = backends.codex_is_confined()
        assert ok is False
        assert "could not start" in reason
        assert "user namespace" in reason  # points at the actual remedy

class TestReportOnly:
    """`--report-only`: run the reviewers, write the findings, change nothing.

    The workflow this serves is the one the loop was not built for. Somebody
    reads the findings and fixes them by hand -- because the merge agent's
    judgement is not trusted on that codebase, or because the fixes need a
    person. Left to itself the loop launches the merge agent the moment there
    is anything blocking, and the reviewer's findings then arrive tangled up
    with a diff nobody asked for.
    """

    @staticmethod
    def _source() -> str:
        return (REPO_ROOT / "scripts" / "review_until_converged.py").read_text()

    def test_the_flag_exists_and_says_what_it_does(self) -> None:
        source = self._source()
        assert '"--report-only"' in source
        assert "does not touch the working tree" in source

    def test_the_merge_agent_is_skipped(self) -> None:
        """The whole point. The return must come before `apply_fixes`."""
        source = self._source()
        skip = source.index("if report_only:")
        merge = source.index("if not apply_fixes(")
        assert skip < merge, "report_only must return before the merge agent runs"

    def test_the_exit_code_is_not_one_the_loop_already_uses(self) -> None:
        """A caller has to tell "findings, untouched" from every other stop.

        0 is converged, 3 is a failed merge agent, 6 is max rounds without
        convergence. Reusing any of those makes the flag unscriptable.
        """
        source = self._source()
        assert "return 10, current_fp" in source

    def test_the_reviewers_are_told_nobody_will_fix_it_for_them(self) -> None:
        """A reviewer asked to "find and fix" writes different findings from
        one asked to report: the first drifts towards what is easy to patch."""
        source = self._source()
        assert "a person will fix them" in source

    def test_the_flag_reaches_the_round(self) -> None:
        """It is threaded through by position; a missing argument would leave
        the default in place and silently run the merge agent anyway."""
        source = self._source()
        assert "args.report_only,\n        )" in source
