"""Measure per-file test coverage and emit per-file gap reports."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from .config import COVERAGE_TARGET, REPO_ROOT, SOURCE_DIRS


def _changed_impl_files(changed_files: list[str]) -> list[str]:
    return [
        f
        for f in changed_files
        if any(f.startswith(d) for d in SOURCE_DIRS)
        and f.endswith(".py")
        and "__init__" not in f
        and "/test" not in f
    ]


def _run_pytest_with_coverage(warnings: list[str]) -> dict[str, Any] | None:
    """Invoke pytest --cov over SOURCE_DIRS and parse the JSON report.

    Returns None on failure. Sources are the configured directories rather than
    the changed files: coverage.py cannot use a .py path as a source.
    """
    cov_json = REPO_ROOT / ".coverage.json"
    cov_json.unlink(missing_ok=True)
    # coverage.py resolves a --cov value as an importable package name or a
    # directory, never as an individual .py file. Passing changed files made
    # coverage report "module was never imported", collect nothing, and write
    # no JSON -- so measure_test_coverage() returned no gaps and the coverage
    # gate failed open, silently, on every multi-file diff.
    #
    # Measure the configured source directories instead. The per-file report is
    # still filtered down to the changed files below, so the audit output is
    # unchanged -- it just has data in it now.
    cov_sources = [
        "--cov=" + d.rstrip("/") for d in SOURCE_DIRS if (REPO_ROOT / d).exists()
    ]
    if not cov_sources:
        warnings.append(
            f"coverage gate inoperative: no SOURCE_DIRS exist on disk: {SOURCE_DIRS}"
        )
        return None
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/",
                *cov_sources,
                "--cov-report=json:" + str(cov_json),
                "--cov-branch",
                "-q",
                "--no-header",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        warnings.append(f"coverage measurement failed: {e}")
        return None
    if not cov_json.exists():
        if result.returncode != 0:
            warnings.append(
                f"pytest --cov failed (exit {result.returncode}); coverage skipped"
            )
        return None

    try:
        data: dict[str, Any] = json.loads(cov_json.read_text())
        return data
    except (json.JSONDecodeError, OSError):
        warnings.append("coverage report could not be parsed")
        return None
    finally:
        cov_json.unlink(missing_ok=True)


def measure_test_coverage(
    changed_files: list[str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Run pytest with coverage on changed source files.

    Returns one entry per changed source file with: coverage %,
    uncovered line numbers, uncovered branches. Skipped silently
    if pytest is not installed or no tests exist.

    Emits a warning when the diff contains Python files but none of
    them match the configured SOURCE_DIRS -- that's a signal the
    coverage gate is silently inoperative for this repo and the user
    should override SOURCE_DIRS for their layout (e.g. add "backend/",
    "app/", "lib/").
    """
    impl_files = _changed_impl_files(changed_files)
    if not impl_files:
        changed_python = [f for f in changed_files if f.endswith(".py")]
        if changed_python:
            warnings.append(
                "coverage gate inoperative: "
                f"{len(changed_python)} Python file(s) changed but none "
                f"matched SOURCE_DIRS={SOURCE_DIRS}. Override SOURCE_DIRS "
                "in scripts/preflight/config.py for this repo's layout."
            )
        return []
    cov_data = _run_pytest_with_coverage(warnings)
    if cov_data is None:
        return []

    gaps = []
    for f in impl_files:
        file_data = cov_data.get("files", {}).get(f)
        if not file_data:
            continue
        summary = file_data.get("summary", {})
        pct = summary.get("percent_covered", 0.0)
        if pct >= COVERAGE_TARGET:
            continue
        gaps.append(
            {
                "file": f,
                "percent_covered": round(pct, 1),
                "uncovered_lines": file_data.get("missing_lines", [])[:30],
                "uncovered_branches": file_data.get("missing_branches", [])[:20],
                "n_statements": summary.get("num_statements", 0),
                "n_missing": summary.get("missing_lines", 0),
            }
        )
    return gaps
