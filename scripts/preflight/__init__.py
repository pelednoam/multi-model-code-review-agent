"""Preflight audit package.

Re-exports the public API. The CLI entry point lives at
``scripts/review_preflight.py``.
"""

from __future__ import annotations

from .artifacts import collect_changed_artifacts
from .config import (
    ARTIFACT_DIRS,
    COVERAGE_TARGET,
    REPO_ROOT,
    SIGNED_MANIFESTS,
    SOURCE_DIRS,
    TEST_DIRS,
)
from .coverage import measure_test_coverage
from .git_state import _run_git, collect_git_state, is_safe_relpath, sha256_file
from .manifests import audit_signed_manifests
from .patterns import check_test_coverage_alignment, scan_suspicious_patterns
from .runner import run_preflight

__all__ = [
    "ARTIFACT_DIRS",
    "COVERAGE_TARGET",
    "REPO_ROOT",
    "SIGNED_MANIFESTS",
    "SOURCE_DIRS",
    "TEST_DIRS",
    "_run_git",
    "audit_signed_manifests",
    "check_test_coverage_alignment",
    "collect_changed_artifacts",
    "collect_git_state",
    "is_safe_relpath",
    "measure_test_coverage",
    "run_preflight",
    "scan_suspicious_patterns",
    "sha256_file",
]
