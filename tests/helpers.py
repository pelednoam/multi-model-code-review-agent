"""What more than one test file needs: the repository's paths, and the full-mode runner."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "docs" / "ensemble_review_result_schema.json"
AGENT_DOC = REPO_ROOT / "docs" / "ensemble-review.md"


def runner_script() -> str:
    """The full-mode runner: the run-reviewers.sh heredoc in docs/ensemble-review.md."""
    m = re.search(
        r"cat > \"\$REVIEW_TMP/run-reviewers\.sh\" <<'RUNNER'\n(.*?)\nRUNNER\n",
        AGENT_DOC.read_text(),
        re.S,
    )
    assert m, "section 6 no longer defines a run-reviewers.sh heredoc"
    return m.group(1)


def shell_function(script: str, name: str) -> str:
    """The definition of ``name`` in ``script``, from ``name() {`` to its closing brace."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", script, re.S | re.M)
    assert m, f"the runner no longer defines {name}()"
    return m.group(0)


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


def liveness(runner: str) -> str:
    """The runner's live_group, and the /proc reader it falls back on."""
    return shell_function(runner, "live_group") + shell_function(
        runner, "session_groups"
    )
