"""Top-level preflight runner that assembles the audit dict."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .artifacts import collect_changed_artifacts
from .config import COVERAGE_TARGET
from .coverage import measure_test_coverage
from .git_state import collect_git_state
from .manifests import audit_signed_manifests
from .patterns import check_test_coverage_alignment, scan_suspicious_patterns


def run_preflight() -> dict[str, Any]:
    """Run all preflight checks and return the audit dict."""
    git_warnings: list[str] = []
    git = collect_git_state(git_warnings)
    all_changed = list(dict.fromkeys(git["changed_vs_main"] + git["untracked_files"]))

    coverage_warnings: list[str] = []
    audit: dict[str, Any] = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "git": git,
        "git_command_warnings": git_warnings,
        "signed_manifests": audit_signed_manifests(),
        "changed_artifacts": collect_changed_artifacts(all_changed),
        "suspicious_patterns": scan_suspicious_patterns(all_changed),
        "test_coverage_alignment": check_test_coverage_alignment(all_changed),
        "coverage_gaps": measure_test_coverage(all_changed, coverage_warnings),
        "coverage_warnings": coverage_warnings,
        "coverage_target_pct": COVERAGE_TARGET,
    }

    audit["n_warnings"] = (
        sum(1 for m in audit["signed_manifests"] if "warning" in m)
        + len(audit["suspicious_patterns"])
        + len(audit["test_coverage_alignment"])
        + len(audit["git_command_warnings"])
        + len(audit["coverage_warnings"])
        + len(audit["coverage_gaps"])
    )

    return audit
