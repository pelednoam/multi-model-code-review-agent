"""Tests for ensemble review infrastructure scripts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
import re
import subprocess
import sys
import tempfile
from unittest import mock
from pathlib import Path

import pytest
from types import SimpleNamespace

from scripts.review_loop.artifacts import describe_outputs

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRUB_SCRIPT = REPO_ROOT / "scripts" / "scrub_diff.py"
PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "review_preflight.py"
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_review_results.py"
SCHEMA_PATH = REPO_ROOT / "docs" / "ensemble_review_result_schema.json"

# Imported as well as run as a subprocess. Most of the scrubber's behaviour is
# about a whole diff and is tested through its real entry point below; the
# per-line decisions are clearer asserted one line at a time.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import scrub_diff  # noqa: E402


@pytest.fixture(autouse=True)
def _agy_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """agy is on by default wherever it is installed, and switching it on costs a model
    call. Tests that fake ``shutil.which`` would otherwise reach a real agy on any machine
    that has one; TestAgyReviewer switches it back on where that is the point."""
    monkeypatch.setenv("REVIEW_USE_AGY", "0")


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
        """The state machine, not the path rule.

        This used to use `k.pem`, which `_is_secret_file` now redacts whole --
        so the assertion would have passed or failed for the wrong reason. A
        key pasted into a document is where the block logic actually matters.
        """
        diff = (
            "diff --git a/docs/setup.md b/docs/setup.md\n"
            "+-----BEGIN PRIVATE KEY-----\n"
            "+AAAA\n"
            "+-----END PRIVATE KEY-----\n"
            "+after the key\n"
        )
        stdout, _, _ = self._run_scrub(diff)
        assert "+after the key" in stdout

    def test_a_pem_file_is_redacted_to_its_last_line(self) -> None:
        """Including whatever follows the END banner.

        The block logic stops at `END`; the path rule does not, and in a file
        that *is* key material there is nothing after it worth a reviewer's
        eyes.
        """
        diff = (
            "diff --git a/k.pem b/k.pem\n"
            "+-----BEGIN PRIVATE KEY-----\n"
            "+AAAA\n"
            "+-----END PRIVATE KEY-----\n"
            "+after the key\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert "after the key" not in stdout
        assert code == 1

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

    def test_a_file_that_is_entirely_a_credential_is_redacted_by_path(self) -> None:
        """No content pattern can catch this.

        Written after a review shipped one: a project keeping its server token
        in `data/token` added the file in a branch, the diff went to four model
        providers, and every pattern here matched nothing. There is no
        `token =` for a keyword pattern to see, and a 43-character base64url
        blob carries no vendor prefix for a shape pattern to see. The scrubber
        exited 0 and called it clean.
        """
        diff = (
            "diff --git a/data/token b/data/token\n"
            "new file mode 100600\n"
            "--- /dev/null\n"
            "+++ b/data/token\n"
            "@@ -0,0 +1 @@\n"
            "+ZbSkuZvQTHh3wnGcs7PDHkM6ccH3EIdcBji_vFNFCg\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert "ZbSkuZvQTHh3" not in stdout
        assert code == 1
        # The structure survives, so a reviewer still sees which file changed.
        assert "+++ b/data/token" in stdout

    def test_a_secret_file_in_a_combined_diff_is_redacted_too(self) -> None:
        """`diff --cc` carries bare paths with no `a/`/`b/` prefix, and the
        section still holds the file's contents. `_is_safe_file` reads only
        prefixed words, which is right for a bypass and wrong for a refusal.
        """
        diff = (
            "diff --cc data/token\n"
            "@@@ -1,1 -1,1 +1,1 @@@\n"
            "++a-live-token-value-0123456789abcd\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert "a-live-token-value" not in stdout
        assert code == 1

    def test_a_secret_file_renamed_to_an_innocent_name_is_still_redacted(self) -> None:
        """Either side naming key material is enough. A rename is a way to move
        a token's content into a file called `notes.txt`, and the content is the
        secret either way.
        """
        diff = (
            "diff --git a/data/token b/data/notes.txt\n"
            "similarity index 100%\n"
            "@@ -1 +1 @@\n"
            "+a-live-token-value-0123456789abcd\n"
        )
        stdout, _, code = self._run_scrub(diff)
        assert "a-live-token-value" not in stdout
        assert code == 1

    @pytest.mark.parametrize(
        "path",
        [
            "src/token.py",
            "src/tokens.json",
            "docs/token-design.md",
            ".env.example",
            "keys/server.pub",
        ],
    )
    def test_a_file_that_merely_sounds_like_a_credential_is_left_alone(self, path: str) -> None:
        """The cost of a false positive here is a reviewer losing a whole file.

        `token.py` is a parser in half the projects that have one, and a public
        key is public -- redacting it buys nothing and costs context.
        """
        diff = f"diff --git a/{path} b/{path}\n@@ -1 +1 @@\n+def parse(text):\n"
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


def _threaded_into_the_round(argument: str) -> bool:
    """Whether ``argument`` is passed in the `_run_one_round` call.

    Reads the call rather than looking for the argument immediately before the
    closing paren. The "is it last" form broke the first time an argument was
    added after it -- which is the one change it existed to survive.
    """
    source = (REPO_ROOT / "scripts" / "review_until_converged.py").read_text()
    # The call, not the definition -- both start with the function's name.
    start = source.index("= _run_one_round(")
    return argument in source[start : source.index("\n        )", start)]


class TestChosenBase:
    """`--base`: review a range that is already on the trunk.

    Without it the base is always `origin/main`, which is right while the work
    is on a branch and wrong the moment it is pushed -- the merge-base diff
    goes empty, the fallback picks `HEAD~1`, and "review everything I did
    today" silently becomes "review the last commit".
    """

    def _repo(self, tmp_path: Path) -> Path:
        """Three commits on main, so a base can name each of them."""
        repo = tmp_path / "host"
        (repo / "app").mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=_GIT_ENV
        )
        for n, line in enumerate(["base", "first", "second"]):
            (repo / "app" / f"{line}.py").write_text(f"value = {n}\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_GIT_ENV)
            subprocess.run(
                ["git", "commit", "-q", "-m", line], cwd=repo, check=True, env=_GIT_ENV
            )
        return repo

    def _collect(self, repo: Path, round_dir: Path, *args: str) -> str:
        from unittest.mock import patch

        from scripts.review_loop.diff import collect_diff

        with patch("scripts.review_loop.diff.SCRIPTS_DIR", REPO_ROOT / "scripts"):
            diff_path, _n = collect_diff(round_dir, repo, *args)
        return diff_path.read_text()

    def test_a_named_base_reviews_everything_since_it(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        round_dir = tmp_path / "round"
        round_dir.mkdir()
        diff = self._collect(repo, round_dir, "HEAD~2")
        assert "app/first.py" in diff
        assert "app/second.py" in diff

    def test_a_nearer_base_reviews_less(self, tmp_path: Path) -> None:
        """The point of naming one at all."""
        repo = self._repo(tmp_path)
        round_dir = tmp_path / "round"
        round_dir.mkdir()
        diff = self._collect(repo, round_dir, "HEAD~1")
        assert "app/second.py" in diff
        assert "app/first.py" not in diff

    def test_a_base_that_names_nothing_is_an_error(self, tmp_path: Path) -> None:
        """Not a fallback.

        A caller that named a base meant it, and quietly reviewing `HEAD~1`
        instead is how you end up believing something was reviewed that never
        was.
        """
        repo = self._repo(tmp_path)
        round_dir = tmp_path / "round"
        round_dir.mkdir()
        with pytest.raises(RuntimeError, match="does not name a commit"):
            self._collect(repo, round_dir, "no-such-ref")

    def test_an_empty_diff_against_a_named_base_stays_empty(self, tmp_path: Path) -> None:
        """"Nothing changed since there" is an answer, not a reason to review
        something else. The `HEAD~1` fallback is for the default base only.
        """
        repo = self._repo(tmp_path)
        round_dir = tmp_path / "round"
        round_dir.mkdir()
        assert self._collect(repo, round_dir, "HEAD").strip() == ""

    def test_the_default_is_unchanged(self, tmp_path: Path) -> None:
        """No `origin/main` here, so the default falls back to `HEAD~1` exactly
        as it did before this flag existed.
        """
        repo = self._repo(tmp_path)
        round_dir = tmp_path / "round"
        round_dir.mkdir()
        diff = self._collect(repo, round_dir)
        assert "app/second.py" in diff
        assert "app/first.py" not in diff

    def test_the_flag_reaches_the_round(self) -> None:
        """Threaded through by position; a missing argument would leave the
        default in place and review the wrong range without saying so.
        """
        source = (REPO_ROOT / "scripts" / "review_until_converged.py").read_text()
        assert '"--base"' in source
        assert "collect_diff(round_dir, repo, base)" in source
        assert _threaded_into_the_round("args.base")


class TestUntrackedFiles:
    """A file nobody has `git add`-ed yet is part of the working tree.

    `git diff` compares tracked files only, so a new module was absent from the
    review while the preflight counted it -- the reviewers were paid to read a
    branch with its most important file missing, and nothing said so.
    """

    def _repo(self, tmp_path: Path) -> Path:
        """Two commits on main, and a `.gitignore` that hides one name."""
        repo = tmp_path / "host"
        (repo / "app").mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=_GIT_ENV
        )
        (repo / ".gitignore").write_text("hidden.txt\n")
        for n, line in enumerate(["base", "last"]):
            (repo / "app" / f"{line}.py").write_text(f"value = {n}\n")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=_GIT_ENV)
            subprocess.run(
                ["git", "commit", "-q", "-m", line], cwd=repo, check=True, env=_GIT_ENV
            )
        return repo

    def _collect(self, repo: Path, tmp_path: Path, *args: str) -> str:
        from unittest.mock import patch

        from scripts.review_loop.diff import collect_diff

        round_dir = tmp_path / "round"
        round_dir.mkdir()
        with patch("scripts.review_loop.diff.SCRIPTS_DIR", REPO_ROOT / "scripts"):
            diff_path, _n = collect_diff(round_dir, repo, *args)
        return diff_path.read_text()

    def test_a_new_file_nobody_added_is_reviewed(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        (repo / "app" / "new.py").write_text("brand_new = True\n")
        diff = self._collect(repo, tmp_path, "HEAD")
        assert "app/new.py" in diff
        assert "+brand_new = True" in diff

    def test_an_ignored_file_stays_out(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        (repo / "hidden.txt").write_text("not for reviewers\n")
        assert self._collect(repo, tmp_path, "HEAD").strip() == ""

    def test_an_untracked_vendored_file_stays_out(self, tmp_path: Path) -> None:
        """The pathspec that keeps the agent out of a host's review applies to
        new files as well as changed ones.
        """
        repo = self._repo(tmp_path)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "review_preflight.py").write_text("# vendored\n")
        (repo / "app" / "mine.py").write_text("mine = 1\n")
        diff = self._collect(repo, tmp_path, "HEAD")
        assert "app/mine.py" in diff
        assert "review_preflight.py" not in diff

    def test_new_files_are_the_change_the_default_base_reviews(
        self, tmp_path: Path
    ) -> None:
        """With `origin/main` at HEAD the tracked diff is empty, which is when
        the default falls back to the last commit -- a pushed branch. New files
        mean there *is* work here, and it is what gets reviewed.
        """
        repo = self._repo(tmp_path)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
            cwd=repo,
            check=True,
            env=_GIT_ENV,
        )
        (repo / "app" / "new.py").write_text("brand_new = True\n")
        diff = self._collect(repo, tmp_path)
        assert "app/new.py" in diff
        assert "app/last.py" not in diff

    def test_with_nothing_new_the_default_still_falls_back(self, tmp_path: Path) -> None:
        """The other half, so the condition above is pinned both ways."""
        repo = self._repo(tmp_path)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
            cwd=repo,
            check=True,
            env=_GIT_ENV,
        )
        assert "app/last.py" in self._collect(repo, tmp_path)


class TestHostConfig:
    """A host project overriding SOURCE_DIRS without editing a vendored file.

    The comment on SOURCE_DIRS says "override this for monorepos" -- and
    `install.sh` copies the file, so any override is destroyed the next time
    the agent is re-vendored. Silently: the only symptom is a coverage gate
    that stops covering anything. A reviewer caught exactly that, on a
    monorepo whose code lives under `packages/*/src`.
    """

    def test_a_host_config_replaces_the_defaults(self, tmp_path: Path) -> None:
        import importlib
        import json

        from scripts.preflight import config as preflight_config

        (tmp_path / "review-config.json").write_text(
            json.dumps({"source_dirs": ["packages/", "services/"], "test_dirs": ["spec/"]}),
            encoding="utf-8",
        )
        with mock.patch.object(preflight_config, "HOST_CONFIG", tmp_path / "review-config.json"):
            assert preflight_config._dirs("source_dirs", ["src/"]) == [
                "packages/",
                "services/",
            ]
            assert preflight_config._dirs("test_dirs", ["tests/"]) == ["spec/"]
        importlib.reload(preflight_config)

    def test_no_host_config_keeps_the_defaults(self, tmp_path: Path) -> None:
        from scripts.preflight import config as preflight_config

        with mock.patch.object(preflight_config, "HOST_CONFIG", tmp_path / "absent.json"):
            assert preflight_config._dirs("source_dirs", ["src/"]) == ["src/"]

    @pytest.mark.parametrize(
        "body",
        [
            "{not json",
            "[]",
            '{"source_dirs": "packages/"}',
            '{"source_dirs": []}',
            '{"source_dirs": [7, "", null]}',
        ],
    )
    def test_an_unusable_host_config_keeps_the_defaults_and_says_so(
        self, tmp_path: Path, body: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A review that refuses to start over a stray comma is worse than one
        that runs with the defaults -- but silence is worse than either.

        A host writes this file precisely because the defaults do not cover its
        layout, so falling back to them without a word recreates the blind
        coverage gate the file exists to prevent.
        """
        from scripts.preflight import config as preflight_config

        where = tmp_path / "review-config.json"
        where.write_text(body, encoding="utf-8")
        with mock.patch.object(preflight_config, "HOST_CONFIG", where):
            assert preflight_config._dirs("source_dirs", ["src/"]) == ["src/"]
        assert "using the agent's defaults" in capsys.readouterr().err

    def test_an_absent_host_config_says_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Most projects do not have one, and that is not a problem."""
        from scripts.preflight import config as preflight_config

        with mock.patch.object(preflight_config, "HOST_CONFIG", tmp_path / "absent.json"):
            assert preflight_config._dirs("source_dirs", ["src/"]) == ["src/"]
        assert capsys.readouterr().err == ""

    def test_entries_that_are_not_strings_are_dropped(self, tmp_path: Path) -> None:
        from scripts.preflight import config as preflight_config

        where = tmp_path / "review-config.json"
        where.write_text('{"source_dirs": ["packages/", 7, "", null]}', encoding="utf-8")
        with mock.patch.object(preflight_config, "HOST_CONFIG", where):
            assert preflight_config._dirs("source_dirs", ["src/"]) == ["packages/"]


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
        # Every launcher (hermes, claude, codex, gemini, agy) recording both is what makes
        # progress observable from another shell.
        assert runner.count('echo $! > "$REVIEW_TMP/pid-$num"') == 5
        # agy's records its status after cleanup, so it holds it in $rc first.
        statuses = runner.count('echo $? > "$REVIEW_TMP/done-$num"') + runner.count(
            'echo "$rc" > "$REVIEW_TMP/done-$num"'
        )
        assert statuses == 5
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
        # One per launcher.
        assert runner.count('timeout -k 10 "$REVIEWER_TIMEOUT"') == 5

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


class TestReviewedFixtures:
    """A line a linter flagged and a person then marked as a fixture.

    The scrubber cannot tell `TOKEN = "token-for-tests"` from a live
    credential, and getting it wrong is expensive in one direction: a
    redaction does not scrub the review, it *aborts* it. So where ruff has
    already asked the question -- `S105`, `S106`, `S107`, "possible hardcoded
    password" -- and somebody has already answered it on that line, the answer
    is honoured.

    Only for the patterns that recognise a credential by the *name* beside it.
    A string shaped like a live key is not excusable, because a comment calling
    one a fixture is exactly what somebody would write to smuggle one past.
    """

    @pytest.mark.parametrize("code", ["S105", "S106", "S107"])
    def test_a_suppressed_keyword_match_is_kept(self, code: str) -> None:
        line = f'+TOKEN = "token-for-tests"  # noqa: {code} - a fixture\n'
        assert scrub_diff.scrub_line(line, False) == line

    def test_a_suppression_among_others_still_counts(self) -> None:
        line = '+TOKEN = "token-for-tests"  # noqa: E501, S105\n'
        assert scrub_diff.scrub_line(line, False) == line

    def test_the_same_line_without_the_suppression_is_redacted(self) -> None:
        """The suppression is the whole signal, so it has to be load-bearing."""
        line = '+TOKEN = "token-for-tests"\n'
        assert scrub_diff.scrub_line(line, False) != line

    def test_an_unrelated_suppression_does_not_count(self) -> None:
        line = '+TOKEN = "token-for-tests"  # noqa: E501\n'
        assert scrub_diff.scrub_line(line, False) != line

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "glpat-aaaaaaaaaaaaaaaaaaaaaa",
        ],
    )
    def test_a_credential_shape_is_redacted_whatever_the_comment_says(
        self, secret: str
    ) -> None:
        """The bypass this must not have.

        A live key with `# noqa: S105` on it is what somebody writes to get a
        key past a scrubber, deliberately or by copying a line that worked.
        """
        line = f'+api_key = "{secret}"  # noqa: S105 - honestly a fixture\n'
        assert scrub_diff.scrub_line(line, False) != line

    def test_a_private_key_banner_is_redacted_whatever_the_comment_says(self) -> None:
        line = "+-----BEGIN OPENSSH PRIVATE KEY-----  # noqa: S105\n"
        assert scrub_diff.scrub_line(line, False) != line

    def test_a_dotenv_path_is_redacted_whatever_the_comment_says(self) -> None:
        line = '+path = ".env.production"  # noqa: S105\n'
        assert scrub_diff.scrub_line(line, False) != line

    def test_the_two_pattern_lists_are_the_whole_of_the_old_one(self) -> None:
        """Nothing may be dropped by the split, in either direction."""
        assert set(scrub_diff.CREDENTIAL_PATTERNS) == set(
            scrub_diff.KEYWORD_PATTERNS
        ) | set(scrub_diff.SHAPE_PATTERNS)
        assert not set(scrub_diff.KEYWORD_PATTERNS) & set(scrub_diff.SHAPE_PATTERNS)

    def test_a_suppression_cannot_be_smuggled_in_the_diff_prefix(self) -> None:
        """The body is what is searched, not the `+` in front of it."""
        line = '+TOKEN = "token-for-tests"\n'
        assert scrub_diff.scrub_line(line, False, in_hunk=True) != line


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
        assert _threaded_into_the_round("args.report_only")


class TestAMissingReviewerIsNotConvergence:
    """A round in which a reviewer produced nothing is incomplete, not converged.

    Found in use: a reviewer's API key was revoked mid-session, its slot came back
    empty, and the three that answered had nothing blocking -- so the loop printed
    CONVERGED and exited 0, and a quarter of the review had silently not happened.
    `if not any(results)` only caught the case where *every* reviewer failed.
    """

    @staticmethod
    def _round(tmp_path: Path, results: list, report_only: bool = False):
        """One round with the reviewers stubbed out, returning (code, fingerprint, stdout)."""
        import io
        from contextlib import redirect_stdout

        import scripts.review_until_converged as loop

        diff = tmp_path / "diff.patch"
        diff.write_text("+ a line\n")
        audit = tmp_path / "audit.json"
        audit.write_text("{}")
        out = io.StringIO()
        with (
            mock.patch.object(loop, "collect_diff", return_value=(diff, 1)),
            mock.patch.object(loop, "run_preflight", return_value=audit),
            mock.patch.object(loop, "launch_reviewers"),
            mock.patch.object(loop, "extract_results", return_value=results),
            mock.patch.object(loop, "write_artifacts_key"),
            mock.patch.object(loop, "describe_outputs", return_value=""),
            redirect_stdout(out),
        ):
            code, fp = loop._run_one_round(
                1, tmp_path, tmp_path, {}, "", set(), False, report_only=report_only
            )
        return code, fp, out.getvalue()

    @staticmethod
    def _clean(name: str) -> dict:
        return {"reviewer": name, "findings": [{"severity": "suggestion", "issue": "x"}]}

    @staticmethod
    def _blocking(name: str) -> dict:
        return {"reviewer": name, "findings": [{"severity": "critical", "issue": "y"}]}

    def test_the_missing_slots_are_named(self) -> None:
        from scripts.review_loop import missing_reviewers

        assert missing_reviewers([self._clean("a"), None, self._clean("c"), None]) == [2, 4]
        assert missing_reviewers([self._clean("a")] * 4) == []

    def test_nothing_blocking_with_a_reviewer_missing_is_exit_11(self, tmp_path: Path) -> None:
        results = [self._clean("a"), None, self._clean("c"), self._clean("d")]
        code, _, said = self._round(tmp_path, results)
        assert code == 11
        assert "INCOMPLETE: R2 produced no result" in said
        assert "3 of 4 reviewers" in said
        assert "CONVERGED: only suggestions remain" not in said
        assert "NOT CONVERGED" in said

    def test_every_reviewer_answering_still_converges(self, tmp_path: Path) -> None:
        """The contrast: the new exit is about the hole, not about suggestions."""
        code, _, said = self._round(tmp_path, [self._clean(n) for n in "abcd"])
        assert code == 0
        assert "CONVERGED: only suggestions remain" in said
        assert "INCOMPLETE" not in said

    def test_blocking_findings_are_still_reported_and_the_hole_is_still_said(
        self, tmp_path: Path
    ) -> None:
        """With something blocking the round goes on as before -- and says it is partial."""
        results = [self._blocking("a"), None, self._clean("c"), self._clean("d")]
        code, _, said = self._round(tmp_path, results, report_only=True)
        assert code == 10
        assert "INCOMPLETE: R2 produced no result" in said
        assert "Blocking findings: 1" in said

    def test_the_new_exit_code_is_documented(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text()
        assert "| 11 | **Incomplete** |" in readme


class TestAgyReviewer:
    """agy, the Antigravity CLI, in slot 3.

    agy cannot make itself read-only -- measured, it wrote outside its workspace with
    nothing denied -- so it runs under a bubblewrap wrapper, and that wrapper is proven
    at startup without a model call. Where bwrap can run, the tests below run the real
    wrapper and the real check, including with each protection removed.
    """

    MODEL = "gemini-4-argon"
    BACKENDS = {
        "hermes": True,
        "claude": True,
        "codex": False,
        "gemini": True,
        "agy": True,
    }

    # What `agy models` listed on the machine this was written on, 2026-10-14.
    LISTED = [
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    ]

    @pytest.fixture(autouse=True)
    def _fresh_pick(self, monkeypatch: pytest.MonkeyPatch) -> object:
        # The pick is cached per run; each test is a run. And no test may reach the
        # real `agy models` -- it is installed on some machines that run this suite.
        from scripts.review_loop import backends

        def unreachable() -> list[str]:
            raise AssertionError("a test reached the real `agy models`")

        monkeypatch.setattr(backends, "agy_models", unreachable)
        # Nor the real agy state, which holds a sign-in token.
        fake_state = Path(tempfile.mkdtemp()) / ".gemini" / "antigravity-cli"
        (fake_state / "bin").mkdir(parents=True)
        (fake_state / "antigravity-oauth-token").write_text("not-a-real-token")
        monkeypatch.setattr(backends, "agy_state_dir", lambda: fake_state)
        backends._picked_agy_model.cache_clear()
        yield
        backends._picked_agy_model.cache_clear()

    def _listing(self, monkeypatch: pytest.MonkeyPatch, slugs: list[str]) -> None:
        from scripts.review_loop import backends

        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(backends, "agy_models", lambda: list(slugs))

    @pytest.fixture
    def on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.delenv("REVIEW_USE_GEMINI", raising=False)

    def _cmd(self, tmp_path: Path, slot: int = 3, prefer_hermes: bool = False) -> list[str]:
        from scripts.review_loop.reviewers import _choose_command

        choice = _choose_command(
            slot, tmp_path / "prompt.txt", tmp_path, self.BACKENDS, prefer_hermes
        )
        assert choice is not None
        return choice[0]

    # -- routing -----------------------------------------------------------------

    @pytest.mark.usefixtures("on")
    def test_slot_3_runs_agy_when_switched_on(self, tmp_path: Path) -> None:
        from scripts.review_loop.reviewers import _choose_command

        choice = _choose_command(3, tmp_path / "p.txt", tmp_path, self.BACKENDS)
        assert choice is not None
        cmd, label = choice
        assert cmd[0] == "agy"
        assert cmd[cmd.index("--model") + 1] == self.MODEL
        # The prompt names the model that actually answered, not the lens default.
        assert label == self.MODEL

    def test_on_by_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("REVIEW_USE_AGY", raising=False)
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.delenv("REVIEW_USE_GEMINI", raising=False)
        assert self._cmd(tmp_path)[0] == "agy"

    def test_zero_switches_it_off(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "0")
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.delenv("REVIEW_USE_GEMINI", raising=False)
        assert self._cmd(tmp_path)[0] == "claude"

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

    def test_unset_model_is_picked_from_what_this_machine_lists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.review_loop.reviewers import _choose_command

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
        from scripts.review_loop.backends import agy_model

        monkeypatch.setenv("AGY_MODEL", "claude-opus-4-6-thinking")
        # The autouse fixture makes any call to `agy models` fail the test.
        assert agy_model() == "claude-opus-4-6-thinking"

    def test_the_pick_is_made_once_per_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The confinement check and the reviewer must run the same model.
        from scripts.review_loop import backends

        calls: list[int] = []
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(
            backends, "agy_models", lambda: calls.append(1) or list(self.LISTED)
        )
        assert backends.agy_model() == backends.agy_model() == "gemini-3.8-flash-high"
        assert len(calls) == 1

    @pytest.mark.parametrize(
        ("listed", "best"),
        [
            # This machine today.
            (LISTED, "gemini-3.8-flash-high"),
            # The day the plan includes Argon: a newer version beats a higher effort.
            ([*LISTED, "gemini-4-argon"], "gemini-4-argon"),
            # Same version: Pro above Flash above Lite, whatever the effort.
            (
                ["gemini-3.8-flash-lite-high", "gemini-3.8-flash-low", "gemini-3.8-pro-low"],
                "gemini-3.8-pro-low",
            ),
            # Versions compare as numbers, not strings.
            (["gemini-3.10-flash-low", "gemini-3.9-flash-high"], "gemini-3.10-flash-low"),
            # Nothing Gemini: no pick, rather than a duplicate of another slot's provider.
            (["claude-opus-4-6-thinking", "gpt-oss-120b-medium"], None),
            ([], None),
        ],
    )
    def test_ranking(self, listed: list[str], best: str | None) -> None:
        from scripts.review_loop.backends import best_agy_model

        assert best_agy_model(listed) == best

    def test_the_listing_is_parsed_from_agys_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_loop import backends

        monkeypatch.undo()  # the real agy_models, with a fake agy behind it
        out = "Fetching available models...\n" + "".join(
            f"{slug}\tDisplay Name\n" for slug in self.LISTED
        )
        monkeypatch.setattr(
            backends, "_run", lambda *a, **k: SimpleNamespace(stdout=out, stderr="")
        )
        assert backends.agy_models() == self.LISTED

    def test_a_listing_that_fails_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop import backends

        monkeypatch.undo()

        def hang(cmd: list[str], **kw: object) -> None:
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(backends, "_run", hang)
        assert backends.agy_models() == []

    @pytest.mark.usefixtures("on")
    def test_only_slot_3(self, tmp_path: Path) -> None:
        for slot in (1, 2, 4):
            assert self._cmd(tmp_path, slot)[0] != "agy"

    @pytest.mark.usefixtures("on")
    def test_bedrock_opt_in_still_takes_slot_3(self, tmp_path: Path) -> None:
        assert self._cmd(tmp_path, prefer_hermes=True)[0] == "hermes"

    @pytest.mark.usefixtures("on")
    def test_an_unverified_agy_is_not_used(self, tmp_path: Path) -> None:
        # detect_backends sets agy False when the canary fails; that must stick.
        from scripts.review_loop.reviewers import _choose_command

        choice = _choose_command(
            3, tmp_path / "p.txt", tmp_path, {**self.BACKENDS, "agy": False}
        )
        assert choice is not None and choice[0][0] != "agy"

    # -- the command -------------------------------------------------------------

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

    # -- the launch --------------------------------------------------------------

    class _Pipe:
        text = ""

        def write(self, s: str) -> None:
            self.text += s

        def close(self) -> None:
            pass

    def _launch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> tuple[object, dict[str, object]]:
        from scripts.review_loop import reviewers

        seen: dict[str, object] = {}
        pipe = self._Pipe()

        class _Proc:
            pid = 1

            def __init__(self, cmd: list[str], **kw: object) -> None:
                seen.update(kw, cmd=cmd, stdin=pipe)
                self.stdin = pipe

        monkeypatch.setattr(reviewers.subprocess, "Popen", _Proc)
        tracked = reviewers._launch_one(
            3, reviewers._LENSES[2], "+DIFF-MARKER", "{}", "ctx", tmp_path, self.BACKENDS
        )
        if tracked is not None:
            tracked[3].close()
            tracked[4].close()
        return tracked, seen

    @pytest.mark.usefixtures("on")
    def test_launch_is_sandboxed_outside_the_repo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.review_loop.backends import AGY_PREAMBLE, agy_sandbox, agy_state_dir

        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        tracked, seen = self._launch(monkeypatch, tmp_path)
        assert tracked is not None
        assert tracked[2] == "agy"  # reported as agy, not as its wrapper

        workspace = Path(str(seen["cwd"]))
        assert workspace.is_dir() and not any(workspace.iterdir())
        assert REPO_ROOT not in workspace.parents
        cmd = seen["cmd"]
        assert isinstance(cmd, list)
        wrap = cmd[: cmd.index("--") + 1]
        state = Path(wrap[wrap.index(str(agy_state_dir())) - 1])
        assert wrap == agy_sandbox(workspace, state)
        assert cmd[len(wrap)] == "agy"
        # A throwaway copy of agy's state, holding the sign-in and nothing else.
        assert sorted(p.name for p in state.iterdir()) == ["antigravity-oauth-token"]
        assert REPO_ROOT not in state.parents

        stdin = seen["stdin"]
        assert isinstance(stdin, self._Pipe)
        (line,) = stdin.text.splitlines()
        content = json.loads(line)["message"]["content"]
        assert content.startswith(AGY_PREAMBLE)
        assert "+DIFF-MARKER" in content
        # The prompt file is the record of what agy was actually sent.
        assert (tmp_path / "prompt-3.txt").read_text() == content

    @pytest.mark.usefixtures("on")
    def test_a_host_that_is_already_a_sandbox_runs_agy_bare(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", "1")
        _, seen = self._launch(monkeypatch, tmp_path)
        cmd = seen["cmd"]
        assert isinstance(cmd, list) and cmd[0] == "agy"

    @pytest.mark.usefixtures("on")
    def test_a_workspace_inside_the_repo_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # TMPDIR pointed into the project puts the one writable directory there.
        from scripts.review_loop import reviewers

        repo = tmp_path / "repo"
        (repo / "tmp").mkdir(parents=True)
        monkeypatch.setattr(reviewers, "REPO_ROOT", repo)
        monkeypatch.setattr(reviewers.tempfile, "tempdir", str(repo / "tmp"))
        tracked, seen = self._launch(monkeypatch, tmp_path)
        # Not agy, and not an empty slot either: the next backend takes it.
        cmd = seen["cmd"]
        assert isinstance(cmd, list) and "agy" not in cmd and cmd[0] == "claude"
        assert not list((repo / "tmp").iterdir())  # and the refused workspace is gone

    def test_the_repo_under_review_is_refused_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # --repo can name a project that is neither this checkout nor the cwd.
        from scripts.review_loop import reviewers

        target = tmp_path / "target"
        (target / ".tmp").mkdir(parents=True)
        monkeypatch.setattr(reviewers.tempfile, "tempdir", str(target / ".tmp"))
        assert reviewers._agy_workspace(target) is None
        assert reviewers._agy_workspace(tmp_path / "elsewhere") is not None

    def test_strays_survive_odd_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HOME with a trailing slash crashed detection; a space in TMPDIR failed it."""
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        from scripts.review_loop import backends

        spaced = Path(tempfile.mkdtemp(prefix="review scratch "))
        monkeypatch.setenv("HOME", str(Path.home()) + "/")
        monkeypatch.setattr(backends.tempfile, "tempdir", str(spaced))
        ok, reason = backends.agy_is_confined()
        assert ok, reason

    def test_a_slot_whose_agy_cannot_be_set_up_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.review_loop import reviewers

        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.setenv("AGY_MODEL", self.MODEL)
        monkeypatch.setattr(reviewers, "_agy_workspace", lambda repo=None: None)
        _, seen = self._launch(monkeypatch, tmp_path)
        cmd = seen["cmd"]
        assert isinstance(cmd, list) and cmd[0] == "claude"

    @pytest.mark.parametrize("value", ["", "0"])
    def test_skip_flag_zero_keeps_the_sandbox(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        # REVIEW_USE_AGY=0 means off; MMCRA_SKIP_AGY_SANDBOX_CHECK=0 must not mean "skip".
        from scripts.review_loop.backends import agy_unsandboxed

        monkeypatch.setenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", value)
        assert agy_unsandboxed() is False

    # -- reading its answer ------------------------------------------------------

    @staticmethod
    def _stream(result: dict[str, object]) -> str:
        return "\n".join(
            json.dumps(event)
            for event in (
                {"event": "init", "cwd": "/tmp/mmcra-agy-x", "permission_mode": "plan"},
                {"event": "step_update", "state": "DONE", "text_delta": "{ not it"},
                {"event": "result", "result": result},
            )
        )

    def _review(self) -> dict[str, object]:
        return {
            "reviewer": "readability_performance",
            "model": self.MODEL,
            "findings": [{"severity": "warning", "issue": "dead code"}],
            "overall_assessment": "One dead branch.",
        }

    def test_structured_output_is_the_result(self, tmp_path: Path) -> None:
        from scripts.review_loop.results import extract_results

        (tmp_path / "raw-3.txt").write_text(
            self._stream(
                {"status": "SUCCESS", "response": "done", "structured_output": self._review()}
            )
        )
        result = extract_results(tmp_path)[2]
        assert result is not None
        assert result["findings"][0]["issue"] == "dead code"
        assert (tmp_path / "result-3.json").exists()

    def test_the_response_text_is_the_fallback(self, tmp_path: Path) -> None:
        from scripts.review_loop.results import extract_results

        (tmp_path / "raw-3.txt").write_text(
            self._stream({"status": "SUCCESS", "response": json.dumps(self._review())})
        )
        result = extract_results(tmp_path)[2]
        assert result is not None and result["model"] == self.MODEL

    def test_a_line_separator_inside_a_string_does_not_lose_the_review(
        self, tmp_path: Path
    ) -> None:
        # Valid JSON may carry U+0085 raw; str.splitlines() splits on it, NDJSON does not.
        from scripts.review_loop.results import extract_results

        review = {**self._review(), "overall_assessment": "one\u0085two"}
        event = {"event": "result", "result": {"status": "SUCCESS", "structured_output": review}}
        init = json.dumps({"event": "init"})
        (tmp_path / "raw-3.txt").write_text(
            init + "\n" + json.dumps(event, ensure_ascii=False), encoding="utf-8"
        )
        result = extract_results(tmp_path)[2]
        assert result is not None and result["overall_assessment"] == "one\u0085two"

    @pytest.mark.parametrize("status", ["ERROR", "CANCELED", "INTERRUPTED", "WAITING"])
    def test_an_unfinished_turn_is_not_a_review(self, tmp_path: Path, status: str) -> None:
        # However review-shaped its output, a turn that did not finish is a hole in
        # the round, not a clean bill -- or it would count towards convergence.
        from scripts.review_loop.results import extract_results

        (tmp_path / "raw-3.txt").write_text(
            self._stream({"status": status, "structured_output": self._review()})
        )
        assert extract_results(tmp_path)[2] is None

    def test_a_failed_turn_says_why(self, tmp_path: Path) -> None:
        (tmp_path / "raw-3.txt").write_text(
            self._stream({"status": "ERROR", "error": "quota exhausted for this model"})
        )
        assert "quota exhausted" in describe_outputs(tmp_path)

    def test_a_refused_tool_call_is_named_not_silent(self, tmp_path: Path) -> None:
        # The first real sweep: `ls -la`, denied, turn over, empty response, and the
        # round said only "NO OUTPUT".
        (tmp_path / "raw-3.txt").write_text(
            self._stream(
                {
                    "status": "SUCCESS",
                    "response": "",
                    "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
                }
            )
        )
        said = describe_outputs(tmp_path)
        assert "refused tool call" in said and "RunCommand" in said

    def test_claude_envelopes_are_unaffected(self) -> None:
        from scripts.review_loop.results import _unwrap_envelope

        claude = json.dumps({"type": "result", "result": '{"a": 1}'})
        assert _unwrap_envelope(claude) == '{"a": 1}'

    # -- the sandbox check -------------------------------------------------------

    @staticmethod
    def _bwrap_works() -> bool:
        """Real-sandbox tests need bwrap that runs, and agy (the check starts it)."""
        import shutil as _shutil

        if _shutil.which("bwrap") is None or _shutil.which("agy") is None:
            return False
        done = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
            capture_output=True,
            check=False,
        )
        return done.returncode == 0

    def test_the_real_sandbox_passes(self) -> None:
        """Not a mock: the wrapper agy runs under, against the real filesystem."""
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        from scripts.review_loop.backends import agy_is_confined

        ok, reason = agy_is_confined()
        assert ok, reason

    @pytest.mark.parametrize(
        ("drop", "caught"),
        [
            # Each protection, removed from the real wrapper, must fail the real check.
            (("--unshare-pid",), "host's processes"),
            (("--tmpfs", "HOME"), "HOME/"),
            (("--tmpfs", "/tmp"), "/tmp/"),
            (("--clearenv",), "environment"),
        ],
    )
    def test_the_real_check_catches_a_weakened_wrapper(
        self, monkeypatch: pytest.MonkeyPatch, drop: tuple[str, ...], caught: str
    ) -> None:
        if not self._bwrap_works():
            pytest.skip("bwrap or agy cannot run here")
        from scripts.review_loop import backends

        real = backends.agy_sandbox
        target = tuple(str(Path.home()) if a == "HOME" else a for a in drop)

        def weakened(ws: Path, st: Path) -> list[str]:
            wrap = real(ws, st)
            for i in range(len(wrap) - len(target) + 1):
                if tuple(wrap[i : i + len(target)]) == target:
                    return wrap[:i] + wrap[i + len(target) :]
            raise AssertionError(f"{target} is not in the wrapper")

        monkeypatch.setattr(backends, "agy_sandbox", weakened)
        ok, reason = backends.agy_is_confined()
        assert not ok and caught.replace("HOME", str(Path.home())) in reason

    @staticmethod
    def _unconfined(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        # A "sandbox" that runs the command with the wrapper stripped: no confinement.
        # agy need not be installed for this: its start-up step is answered here.
        bare = cmd[cmd.index("--") + 1 :]
        if "agy --version" in bare[-1]:
            return subprocess.CompletedProcess(bare, 0, "AGY_OK\n", "")
        return subprocess.run(bare, capture_output=True, text=True, check=False)

    def test_a_sandbox_that_lets_writes_through_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_loop import backends

        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(backends, "_run", self._unconfined)
        ok, reason = backends.agy_is_confined()
        assert not ok and "ALLOWED a write" in reason

    def test_a_sandbox_that_cannot_start_is_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The codex ambiguity again: an untouched canary is also what nothing looks like."""
        from scripts.review_loop import backends

        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(
            backends,
            "_run",
            lambda *a, **k: SimpleNamespace(
                stdout="", stderr="bwrap: setting up uid map: Permission denied"
            ),
        )
        ok, reason = backends.agy_is_confined()
        assert not ok and "could not start" in reason and "namespaces" in reason

    def test_no_bwrap_is_not_a_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.review_loop import backends

        monkeypatch.setattr(backends.shutil, "which", lambda c: None)
        ok, reason = backends.agy_is_confined()
        assert not ok and "bwrap" in reason

    @pytest.mark.parametrize(
        "failure", [OSError("exec failed"), subprocess.TimeoutExpired(["bwrap"], 30)]
    )
    def test_a_check_that_crashes_disables_rather_than_aborts(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        # Raised out of detect_backends, this stopped every other reviewer too.
        from scripts.review_loop import backends

        def boom(*a: object, **k: object) -> None:
            raise failure

        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(backends, "_run", boom)
        ok, reason = backends.agy_is_confined()
        assert not ok and "check failed" in reason

    def test_the_check_proves_the_wrapper_a_reviewer_runs_under(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_loop import backends

        calls: list[list[str]] = []

        def record(cmd: list[str], **kw: object) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(stdout="", stderr="")

        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(backends, "_run", record)
        backends.agy_is_confined()
        (first,) = calls  # it stops at "could not start"
        wrap = first[: first.index("--") + 1]
        state = Path(wrap[wrap.index(str(backends.agy_state_dir())) - 1])
        last_bind = len(wrap) - 1 - wrap[::-1].index("--bind")
        workspace = Path(wrap[last_bind + 1])
        assert wrap == backends.agy_sandbox(workspace, state)

    # -- detection ---------------------------------------------------------------

    def _detect(
        self, monkeypatch: pytest.MonkeyPatch, verdict: tuple[bool, str] | None
    ) -> dict[str, bool]:
        from scripts.review_loop import backends

        monkeypatch.setenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", "1")
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")

        def check() -> tuple[bool, str]:
            if verdict is None:
                raise AssertionError("the agy check ran when it should not have")
            return verdict

        monkeypatch.setattr(backends, "agy_is_confined", check)
        return backends.detect_backends()

    @pytest.mark.usefixtures("on")
    def test_an_unconfined_agy_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        found = self._detect(monkeypatch, (False, "write landed"))
        assert found["agy"] is False
        assert found["claude"] is True

    @pytest.mark.usefixtures("on")
    def test_a_confined_agy_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        assert self._detect(monkeypatch, (True, "verified"))["agy"] is True

    def test_nothing_runs_when_switched_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "0")
        # And the backend table says so, rather than reporting an unused agy as present.
        assert self._detect(monkeypatch, None)["agy"] is False

    def test_switched_on_without_a_model_is_dropped_without_a_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.delenv("AGY_MODEL", raising=False)
        self._listing(monkeypatch, ["claude-sonnet-4-6"])
        assert self._detect(monkeypatch, None)["agy"] is False

    def test_a_picked_model_still_has_to_pass_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REVIEW_USE_AGY", "1")
        monkeypatch.delenv("AGY_MODEL", raising=False)
        monkeypatch.delenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", raising=False)
        self._listing(monkeypatch, self.LISTED)
        assert self._detect(monkeypatch, (False, "no bwrap"))["agy"] is False

    @pytest.mark.usefixtures("on")
    def test_the_check_can_be_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MMCRA_SKIP_AGY_SANDBOX_CHECK", "1")
        assert self._detect(monkeypatch, None)["agy"] is True

    # -- the full-mode runner ----------------------------------------------------

    def test_the_bash_launcher_matches_the_python_one(self) -> None:
        """The doc is the program for full mode; it must not be the weaker path."""
        from scripts.review_loop.backends import AGY_SAFETY_ARGS

        runner = TestRunnerScript()._runner()
        for flag in AGY_SAFETY_ARGS:
            assert flag in runner
        assert "--print-timeout 0" in runner
        # One copy of the sandbox and the preamble: the runner calls the same functions.
        assert "agy_sandbox(ws, agy_seed_state(st))" in runner
        assert "mapfile -d '' -t wrap" in runner
        assert "agy_review_message" in runner
        # A failed mktemp must not fall back to running agy in the repository.
        assert 'ws="$(mktemp -d)" || return 1' in runner
        assert 'rm -rf "$ws" "$state"' in runner
        # =0 must not mean "skip the sandbox".
        assert '""|0)' in runner
