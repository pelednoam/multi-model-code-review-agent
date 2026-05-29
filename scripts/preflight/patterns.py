"""Scan changed files for suspicious patterns and test/impl alignment."""

from __future__ import annotations

from .config import (
    REPO_ROOT,
    SELF_SKIP_PREFIXES,
    SOURCE_DIRS,
    SUSPICIOUS_COMPILED,
    TEST_DIRS,
)
from .git_state import is_safe_relpath


def _should_skip(file: str) -> bool:
    return any(file == p or file.startswith(p) for p in SELF_SKIP_PREFIXES)


def scan_suspicious_patterns(
    changed_files: list[str],
) -> list[dict[str, str | int]]:
    """Scan changed files for suspicious code patterns."""
    findings: list[dict[str, str | int]] = []
    for f in changed_files:
        if _should_skip(f) or not is_safe_relpath(f):
            continue
        path = REPO_ROOT / f
        if not path.exists() or path.suffix != ".py":
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue

        for i, line in enumerate(lines, 1):
            for compiled, description in SUSPICIOUS_COMPILED:
                if compiled.search(line):
                    findings.append(
                        {
                            "file": f,
                            "line": i,
                            "pattern": description,
                            "text": line.strip()[:120],
                        }
                    )
                    break
    return findings


def check_test_coverage_alignment(
    changed_files: list[str],
) -> list[str]:
    """Check for implementation changes without test changes.

    Uses ``SOURCE_DIRS`` and ``TEST_DIRS`` from config to identify
    impl vs test files. Override those constants for non-conventional
    repo layouts (e.g. ``backend/``, ``frontend/__tests__/``).
    """
    warnings = []
    impl_files = {
        f
        for f in changed_files
        if any(f.startswith(d) for d in SOURCE_DIRS)
        and f.endswith(".py")
        and not f.split("/")[-1].startswith("test_")
        and "__init__" not in f
    }
    test_files = {
        f
        for f in changed_files
        if any(f.startswith(d) for d in TEST_DIRS) and f.endswith(".py")
    }

    if impl_files and not test_files:
        warnings.append(
            f"{len(impl_files)} implementation files changed but no test files"
        )
    if test_files and not impl_files:
        warnings.append(
            f"{len(test_files)} test files changed but no implementation files"
        )

    return warnings
