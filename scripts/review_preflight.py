"""Deterministic pre-review checklist and runtime artifact audit.

Runs cheap, non-LLM checks before the ensemble review pipeline.
Collects git state, artifact metadata, and manifest comparisons
into a machine-readable JSON file that every reviewer consumes.

Usage:
    python scripts/review_preflight.py --output audit.json

The audit logic lives in ``scripts/preflight/``. This file is the
CLI entry point and a back-compat re-export shim so existing imports
like ``from scripts.review_preflight import _run_git`` keep working.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# When invoked as `python scripts/review_preflight.py`, the repo root
# is not yet on sys.path -- but the sub-package import needs it. Add
# it before any `from scripts.preflight import ...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Re-export the public API so existing imports keep working.
from scripts.preflight import (  # noqa: E402
    ARTIFACT_DIRS,
    COVERAGE_TARGET,
    REPO_ROOT,
    SIGNED_MANIFESTS,
    _run_git,
    audit_signed_manifests,
    check_test_coverage_alignment,
    collect_changed_artifacts,
    collect_git_state,
    is_safe_relpath,
    measure_test_coverage,
    run_preflight,
    scan_suspicious_patterns,
    sha256_file,
)

__all__ = [
    "ARTIFACT_DIRS",
    "COVERAGE_TARGET",
    "REPO_ROOT",
    "SIGNED_MANIFESTS",
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


def _print_summary(audit: dict[str, Any]) -> None:
    print(f"  Branch: {audit['git']['branch']}")
    print(f"  Commit: {audit['git']['commit']}")
    print(f"  Changed files: {len(audit['git']['changed_vs_main'])}")
    print(f"  Untracked: {len(audit['git']['untracked_files'])}")
    print(f"  Artifacts: {len(audit['changed_artifacts'])}")
    print(f"  Suspicious: {len(audit['suspicious_patterns'])}")
    n_gaps = len(audit["coverage_gaps"])
    if n_gaps:
        print(f"  Coverage gaps ({COVERAGE_TARGET}% target): {n_gaps} file(s)")
        for gap in audit["coverage_gaps"][:5]:
            print(
                f"    {gap['file']}: {gap['percent_covered']}% "
                f"({gap['n_missing']}/{gap['n_statements']} lines missing)"
            )
    print(f"  Warnings: {audit['n_warnings']}")


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write the audit JSON output",
    )
    args = parser.parse_args()

    audit = run_preflight()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(audit, f, indent=2, default=str)

    print(f"Audit written to {args.output}")
    _print_summary(audit)

    sys.exit(1 if audit["n_warnings"] > 0 else 0)


if __name__ == "__main__":
    main()
