"""Scrub credential patterns from a diff on stdin, write clean diff to stdout.

Designed to be used in a pipe so raw secrets never touch disk:

    git diff --merge-base origin/main -- . \\
      | python scripts/scrub_diff.py > /tmp/ensemble-review-diff.patch

Lines matching credential patterns are replaced with a redaction marker.
The diff structure (headers, hunks) is preserved so reviewers can still
see file/line context.

Exits non-zero if any lines were redacted -- the review pipeline should
block until the secrets are removed from the branch and rotated.
"""

from __future__ import annotations

import re
import sys

CREDENTIAL_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?key)\s*[:=]"),
    re.compile(r"(?i)(password|passwd|pwd)\s*[:=]"),
    re.compile(r"(?i)(token|bearer)\s*[:=]"),
    re.compile(r"(?i)BEGIN\s+(RSA\s+)?PRIVATE\s+KEY"),
    re.compile(r"(?i)\.env\b"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36,}"),
    re.compile(r"gho_[a-zA-Z0-9]{36,}"),
    re.compile(r"glpat-[a-zA-Z0-9\-]{20,}"),
    re.compile(r"(?i)client[_-]?secret\s*[:=]"),
    re.compile(r"(?i)DefaultEndpointsProtocol=https;AccountName="),
    re.compile(r'"type"\s*:\s*"service_account"'),
]

REDACTED = "+++ [REDACTED: credential pattern detected] +++"

_SCRUBBER_SAFE_FILES = frozenset(
    {
        "scripts/scrub_diff.py",
        "a/scripts/scrub_diff.py",
        "b/scripts/scrub_diff.py",
        "tests/test_review_scripts.py",
        "a/tests/test_review_scripts.py",
        "b/tests/test_review_scripts.py",
    }
)


def scrub_line(line: str, in_scrubber_file: bool) -> str:
    """Replace a line with a redaction marker if it matches any pattern.

    Args:
        line: The diff line to check.
        in_scrubber_file: If True, this line is part of a safe file
            (the scrubber itself or its test suite) that contains
            credential patterns as string literals, not real secrets.

    Returns:
        The original line, or the REDACTED marker.
    """
    if in_scrubber_file:
        return line
    for pattern in CREDENTIAL_PATTERNS:
        if pattern.search(line):
            return REDACTED + "\n"
    return line


def main() -> None:
    """Read stdin, scrub, write to stdout. Exit 1 if any redactions."""
    n_redacted = 0
    in_scrubber = False

    for line in sys.stdin:
        if line.startswith("diff --git "):
            parts = line.split()
            in_scrubber = any(p in _SCRUBBER_SAFE_FILES for p in parts)

        clean = scrub_line(line, in_scrubber)
        sys.stdout.write(clean)
        if clean == REDACTED + "\n":
            n_redacted += 1

    if n_redacted > 0:
        print(
            f"# scrub_diff.py: {n_redacted} line(s) redacted",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
