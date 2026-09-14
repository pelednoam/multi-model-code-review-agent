"""Scrub credential patterns from a diff on stdin, write clean diff to stdout.

Designed to be used in a pipe so raw secrets never touch disk:

    git diff --merge-base origin/main -- . \\
      | python scripts/scrub_diff.py > diff.patch

Lines matching credential patterns are replaced with a redaction marker.
The diff structure (headers, hunks) is preserved so reviewers can still
see file/line context.

Exit codes:

* ``0`` -- nothing redacted, the diff is clean.
* ``1`` -- at least one line was redacted. The review pipeline should block
  until the secrets are removed from the branch and rotated.
* ``2`` -- the scrubber itself failed. The output on stdout is truncated and
  must not be treated as a scrubbed diff. This is deliberately distinct from
  ``1``: a crash halfway through writing ``diff.patch`` leaves a plausible
  looking patch whose tail was never scrubbed, and a caller that cannot tell
  the two apart will happily ship it to a reviewer.
"""

from __future__ import annotations

import re
import sys

# A credential is a *value*, not a word. Matching `token\s*[:=]` alone flags
# `token: TokenSpec` and `token=token` -- ordinary code in any project where a
# token is a domain object rather than a secret: a parser, a design system, a
# game. Those diffs are not scrubbed, they are *aborted*, so an over-broad
# pattern does not cost a little noise, it costs the whole review.
#
# So the word must be followed by something that could actually be a secret:
# a quoted string of any real length, or an unquoted run long enough to be a
# key and containing a digit -- which every generated credential has and
# `TokenSpec`, `str` and `os.environ` do not.
_SECRET_VALUE = r"""(?:['"][^'"]{4,}['"]|(?=[\w.\-]*\d)[\w.\-]{8,})"""

# A private key is a *block*, not a line. A banner matches one line and one
# line only; every base64 body line after it matches nothing (it has no
# `key:`-style prefix, and it is not an `sk-`/`AKIA`/`ghp_` shape), so a
# line-at-a-time scrubber redacts the banner and writes the usable key straight
# through. These two bound the block so the body can be redacted as well.
#
# `_PEM_BEGIN` is also the banner pattern in CREDENTIAL_PATTERNS below, and has
# to be: an earlier version paired a narrow `BEGIN (RSA )?PRIVATE KEY` there
# with this broad one here, and opened the block only when the banner had been
# redacted. `-----BEGIN OPENSSH PRIVATE KEY-----` matched the broad pattern but
# not the narrow one, so nothing was redacted, the block never opened, and the
# scrubber exited 0 calling the key clean. One regex, used for both jobs.
_PEM_BEGIN = re.compile(r"(?i)BEGIN\s+[A-Z0-9 ]*PRIVATE\s+KEY")
_PEM_END = re.compile(r"(?i)END\s+[A-Z0-9 ]*PRIVATE\s+KEY")

CREDENTIAL_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?key)\s*[:=]\s*" + _SECRET_VALUE
    ),
    re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*" + _SECRET_VALUE),
    re.compile(r"(?i)\b(token|bearer)\s*[:=]\s*" + _SECRET_VALUE),
    _PEM_BEGIN,
    re.compile(r"(?i)(^|[\s'\"/])\.env(\.[a-z]+)?([\s'\"/]|$)"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Hyphens included: a current key is `sk-ant-api03-...` or `sk-proj-...`,
    # and a run of plain alphanumerics stops at the first hyphen -- so the
    # pattern matched only legacy keys, and missed every key this tool is most
    # likely to meet.
    re.compile(r"sk-[a-zA-Z0-9_-]{20,192}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36,}"),
    re.compile(r"gho_[a-zA-Z0-9]{36,}"),
    re.compile(r"glpat-[a-zA-Z0-9\-]{20,}"),
    re.compile(r"(?i)client[_-]?secret\s*[:=]\s*" + _SECRET_VALUE),
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
    body = line[1:].lstrip() if line[:1] in ("+", "-", " ") else line.lstrip()
    return (
        body.startswith(
            ("re.compile(", "_SECRET_VALUE", "_PEM_", "CREDENTIAL_PATTERNS")
        )
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


#: Metadata lines that begin with a body-line character. `--- a/.env.example`
#: and `+++ b/.env.example` start with `-` and `+`, so treating them as content
#: redacted the file headers themselves: the patch lost its file attribution,
#: became structurally invalid, and the review aborted claiming a leaked secret
#: because someone committed a `.env.example`.
_HEADER_PREFIXES = ("--- ", "+++ ", "---\n", "+++\n")


def _is_body_line(line: str, *, in_hunk: bool) -> bool:
    """Whether this is a hunk body line rather than diff metadata.

    ``in_hunk`` is what separates the two, and it has to: a removed line of SQL,
    Lua, Haskell or Ada whose content is a `--` comment arrives as
    `--- password = "hunter2abc"`, which is indistinguishable from a file header
    by prefix alone. Treating it as metadata wrote the credential straight
    through and exited 0. Headers appear before the first `@@` of a file; after
    one, everything is content until the next `diff` line.
    """
    if not in_hunk and line.startswith(_HEADER_PREFIXES):
        return False
    return line[:1] in ("+", "-", " ")


def _is_metadata(line: str, *, in_hunk: bool) -> bool:
    """Whether this line is diff structure rather than file content."""
    return not _is_body_line(line, in_hunk=in_hunk)


def _body(line: str, *, in_hunk: bool) -> str:
    """The line without the diff format's one-character prefix."""
    return line[1:] if _is_body_line(line, in_hunk=in_hunk) else line


def scrub_line(line: str, in_safe_file: bool, *, in_hunk: bool = True) -> str:
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
    if _is_metadata(line, in_hunk=in_hunk):
        # Headers are structure, not content. A file *named* `.env.example`
        # is not a secret, and redacting its header breaks the patch.
        return line
    if in_safe_file and _is_intentional_fixture_line(line):
        return line
    # Match the body, not the `+`/`-`/` ` the diff format puts in front of it.
    # `+` is neither a line start nor one of the delimiters the anchored
    # patterns accept, so `^`-anchored patterns could never fire on an added
    # line -- `+.env.production` went through untouched while the same text in
    # prose was redacted.
    for pattern in CREDENTIAL_PATTERNS:
        if pattern.search(_body(line, in_hunk=in_hunk)):
            return _redact_preserving_prefix(line)
    return line


def _force_utf8(stream: object) -> None:
    """Pin a standard stream to UTF-8 with replacement.

    Diffs carry whatever bytes the repository holds -- latin-1 sources,
    `--text` output of near-binary files -- and CI often runs under `LC_ALL=C`,
    which makes stdout ASCII. Under the ambient codec either one raises
    mid-stream, and the exception escapes after an unknown number of lines have
    already been written: a truncated patch whose tail was never scrubbed.
    Replacement characters in a reviewer's diff are a far better outcome.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:  # pragma: no branch - always a TextIOWrapper
        reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    """Read stdin, scrub, write to stdout. Exit 1 if any redactions."""
    _force_utf8(sys.stdin)
    _force_utf8(sys.stdout)

    n_redacted = 0
    in_safe = False
    in_key = False
    in_hunk = False

    for line in sys.stdin:
        # Fail closed on every header form, not just `diff --git`. `in_safe`
        # used to latch: a `diff --cc` section from a merge commit following a
        # safe file inherited its bypass, and any line in it containing
        # `re.compile(` -- which is to say any Python file defining a regex --
        # passed through unscrubbed.
        if line.startswith("diff "):
            in_safe = line.startswith("diff --git ") and _is_safe_file(line)
            in_key = False
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        if in_key and not _is_body_line(line, in_hunk=in_hunk):
            in_key = False

        if in_key:
            clean = _redact_preserving_prefix(line)
            if _PEM_END.search(line):
                in_key = False
        else:
            clean = scrub_line(line, in_safe, in_hunk=in_hunk)
            # Only a *redacted* BEGIN line opens a block. In a safe file the
            # banner is an intentional fixture and passes through, and the
            # fixture's body must not then be swallowed.
            if clean != line and _PEM_BEGIN.search(line):
                in_key = True

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
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - any failure truncates the output
        # Anything already on stdout is a partial, unscrubbed patch. Exit 2 so
        # the caller can say so rather than reporting a clean block. Catching
        # only OSError left MemoryError, RecursionError and re.error exiting 1,
        # which the caller reads as "rotate your credentials" -- sending an
        # operator to hunt a secret that was never there.
        print(f"# scrub_diff.py: aborted, output is truncated: {exc}", file=sys.stderr)
        sys.exit(2)
