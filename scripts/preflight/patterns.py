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

try:  # run as scripts/review_preflight.py, with scripts/ on the path
    from scrub_diff import REDACTED_TEXT, redacts
except ImportError:  # imported as the scripts package
    from scripts.scrub_diff import REDACTED_TEXT, redacts

#: What a quoted line becomes when it looks like it holds a credential: the diff's own
#: marker, without the "# " that keeps a redacted diff line a comment.
REDACTED = REDACTED_TEXT


def _quoted(line: str) -> str:
    """A source line as the audit quotes it: trimmed, and scrubbed as the diff is.

    The audit goes into every reviewer's prompt -- to Google too, with agy on -- and
    these lines come from whole changed files, not only the changed lines the diff
    holds, so the diff's scrubbing never saw them.
    """
    if redacts(line.rstrip("\n")):  # read as an added line of the diff is
        return REDACTED
    return line.strip()[:120]


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
            # Numbered at "\n" alone, as git and the loop's re-quote number them, and
            # read as UTF-8, which a non-ASCII file under LC_ALL=C raised on. As bytes:
            # read as text, a "\r" became a line break anyway, between a credential's
            # name and its value, and neither half was a credential to the scrub.
            lines = path.read_bytes().decode("utf-8", errors="replace").split("\n")
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
                            "text": _quoted(line),
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
