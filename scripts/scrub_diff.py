"""Scrub credential patterns from a diff on stdin, write clean diff to stdout.

Designed to be used in a pipe so raw secrets never touch disk:

    git diff --merge-base origin/main -- . \\
      | python scripts/scrub_diff.py > diff.patch

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
    re.compile(r"(?i)\b(token|bearer)\s*[:=]"),
    re.compile(r"(?i)BEGIN\s+(RSA\s+)?PRIVATE\s+KEY"),
    re.compile(r"(?i)(^|[\s'\"/])\.env(\.[a-z]+)?([\s'\"/]|$)"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[a-zA-Z0-9]{20,128}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36,}"),
    re.compile(r"gho_[a-zA-Z0-9]{36,}"),
    re.compile(r"glpat-[a-zA-Z0-9\-]{20,}"),
    re.compile(r"(?i)client[_-]?secret\s*[:=]"),
    re.compile(r"(?i)DefaultEndpointsProtocol=https;AccountName="),
    re.compile(r'"type"\s*:\s*"service_account"'),
]

REDACTED = "# [REDACTED: credential pattern detected]"
_REDACTED_LINE = REDACTED + "\n"

# Files whose diff sections are safe to pass through without
# scrubbing. These contain credential patterns as string literals
# (regex definitions, test fixtures), not real secrets. Only the
# basename after a/ or b/ is checked, so the match is path-exact.
_SCRUBBER_SAFE_FILES = frozenset(
    {
        "scripts/scrub_diff.py",
        "tests/test_review_scripts.py",
    }
)


def _is_safe_file(diff_header: str) -> bool:
    """Check if a diff --git header names a safe file.

    Parses both the a/ and b/ paths from the header and checks
    that BOTH (stripped of the a/ or b/ prefix) are in the
    safe-file set. This prevents rename-based bypass where one
    side is a safe file but the other is attacker-controlled.
    """
    parts = diff_header.split()
    ab_paths = []
    for part in parts:
        if part.startswith("a/") or part.startswith("b/"):
            ab_paths.append(part[2:])
    if not ab_paths:
        return False
    return all(p in _SCRUBBER_SAFE_FILES for p in ab_paths)


def _is_intentional_fixture_line(line: str) -> bool:
    """Lines containing regex patterns or test credential fixtures.

    Used to narrow the safe-file bypass so that only lines that look
    like intentional pattern definitions or fixture strings pass
    through unscrubbed. Any other line in a "safe" file is still
    scrubbed normally, so accidentally committed real secrets are
    still caught.
    """
    return (
        "re.compile(" in line
        or "+API_KEY" in line
        or "+password" in line
        or "+secret" in line
        or "+token" in line
        or "+sk-" in line
        or "+AKIA" in line
        or "+ghp_" in line
        or "+gho_" in line
        or "+glpat-" in line
        or "+-----BEGIN" in line
        or '"type": "service_account"' in line
    )


def _redact_preserving_prefix(line: str) -> str:
    """Return the redaction marker with the diff prefix preserved.

    The diff prefix (``+``, ``-``, or `` ``) is preserved so that the
    resulting patch remains syntactically valid. Lines that are not
    hunk body lines (e.g. headers like ``diff --git`` already filtered
    by the caller) pass through unchanged.
    """
    if not line:
        return _REDACTED_LINE
    prefix = line[0]
    if prefix in ("+", "-", " "):
        return f"{prefix}{REDACTED}\n"
    return _REDACTED_LINE


def scrub_line(line: str, in_safe_file: bool) -> str:
    """Replace a line with a redaction marker if it matches any pattern.

    Args:
        line: The diff line to check.
        in_safe_file: If True, this line is part of a safe file
            (the scrubber itself or its test suite) that contains
            credential patterns as string literals, not real secrets.
            Only lines matching :func:`_is_intentional_fixture_line`
            bypass scrubbing in safe files.

    Returns:
        The original line, or a redaction marker that preserves the
        diff prefix (``+``, ``-``, or `` ``) so the patch remains
        syntactically valid.
    """
    if in_safe_file and _is_intentional_fixture_line(line):
        return line
    for pattern in CREDENTIAL_PATTERNS:
        if pattern.search(line):
            return _redact_preserving_prefix(line)
    return line


def main() -> None:
    """Read stdin, scrub, write to stdout. Exit 1 if any redactions."""
    n_redacted = 0
    in_safe = False

    for line in sys.stdin:
        if line.startswith("diff --git "):
            in_safe = _is_safe_file(line)

        clean = scrub_line(line, in_safe)
        sys.stdout.write(clean)
        if clean != line and REDACTED in clean:
            n_redacted += 1

    if n_redacted > 0:
        print(
            f"# scrub_diff.py: {n_redacted} line(s) redacted",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
