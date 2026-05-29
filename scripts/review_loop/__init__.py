"""Convergence-loop package.

Re-exports the public API. The CLI entry point lives at
``scripts/review_until_converged.py``.
"""

from __future__ import annotations

from .backends import _run, detect_backends
from .config import (
    MERGE_TIMEOUT,
    REPO_ROOT,
    REVIEWER_TIMEOUT,
    SCRIPTS_DIR,
    TEST_TIMEOUT,
)
from .diff import collect_diff, run_preflight
from .findings import (
    FindingKey,
    collect_blocking_findings,
    fingerprint,
    validate_result,
)
from .gate import run_gate, run_tests
from .git_ops import commit_and_push
from .merge_agent import apply_fixes
from .results import extract_results
from .reviewers import build_reviewer_prompt, launch_reviewers

__all__ = [
    "FindingKey",
    "MERGE_TIMEOUT",
    "REPO_ROOT",
    "REVIEWER_TIMEOUT",
    "SCRIPTS_DIR",
    "TEST_TIMEOUT",
    "_run",
    "apply_fixes",
    "build_reviewer_prompt",
    "collect_blocking_findings",
    "collect_diff",
    "commit_and_push",
    "detect_backends",
    "extract_results",
    "fingerprint",
    "launch_reviewers",
    "run_gate",
    "run_preflight",
    "run_tests",
    "validate_result",
]
