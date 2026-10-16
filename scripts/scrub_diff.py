"""Scrub credential patterns from a diff on stdin, write clean diff to stdout.

Designed to be used in a pipe so raw secrets never touch disk:

    git diff --merge-base origin/main -- . \\
      | python scripts/scrub_diff.py > diff.patch

Lines matching credential patterns are replaced with a redaction marker.
The diff structure (headers, hunks) is preserved so reviewers can still
see file/line context.

Modes:

* (no argument) -- stdin is a unified diff.
* ``--text`` -- stdin is plain text, every line content: full mode's context
  bundle.
* ``--json`` -- stdin is a JSON document, whose keys and strings are scrubbed
  and which is written back as JSON: the preflight audit.

Any other argument is exit 2.

Exit codes:

* ``0`` -- nothing redacted, the diff is clean.
* ``1`` -- at least one line was redacted. For a diff, the review pipeline
  should block until the secrets are removed from the branch and rotated; for
  ``--text`` and ``--json``, the redacted output is safe to send and the
  review goes on.
* ``2`` -- the scrubber itself failed. The output on stdout is truncated and
  must not be treated as a scrubbed diff. This is deliberately distinct from
  ``1``: a crash halfway through writing ``diff.patch`` leaves a plausible
  looking patch whose tail was never scrubbed, and a caller that cannot tell
  the two apart will happily ship it to a reviewer.
"""

from __future__ import annotations

import functools
import json
import re
import sys
from pathlib import PurePath

# Files whose *entire content* is a credential, recognised by path rather than
# by what is in them. No content pattern can do this job: a 43-character
# base64url blob is indistinguishable from a hash, a build id or a test
# fixture, so the only thing that knows it is a secret is where it lives.
#
# Written after a review shipped one. A project keeping its server token in
# `data/token` added the file in a branch, the diff went to four model
# providers, and every pattern here matched nothing -- there was no `token =`
# for a keyword pattern to see, and the value carried no vendor prefix for a
# shape pattern to see. The scrubber exited 0 and called it clean.
#
# Exact paths are a project's own business and this tool is generic, so the
# default set is empty and the work is done by name and suffix below. A host
# project with a credential somewhere unguessable can add to it.
SECRET_PATHS: frozenset[str] = frozenset()

# Basenames that are a credential wherever they sit in a tree.
SECRET_NAMES = frozenset(
    {
        "token",
        ".env",
        ".netrc",
        ".pgpass",
        ".htpasswd",
        "credentials",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)

# Suffixes that say "this file is key material". `.pub` is deliberately not
# here: a public key is public, and redacting it would cost a reviewer real
# context for nothing.
SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"})

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

# Two kinds, and the difference decides what a human is allowed to wave away.
#
# KEYWORD_PATTERNS recognise a credential by the *name* next to it: `token =`,
# `password:`, `api_key=`. They are the ones that fire on ordinary code in any
# project where a token is a domain object, so they are the ones a reviewed
# fixture suppression may excuse.
KEYWORD_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?key)\s*[:=]\s*" + _SECRET_VALUE
    ),
    re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*" + _SECRET_VALUE),
    re.compile(r"(?i)\b(token|bearer)\s*[:=]\s*" + _SECRET_VALUE),
    re.compile(r"(?i)client[_-]?secret\s*[:=]\s*" + _SECRET_VALUE),
]

#: A URL password that is no password: a template's field, named as a template names
#: one -- {password}, {password!r}, {{password}}, {0}, ${DB_PASS}, %(pw)s, %(pw)d,
#: <password> -- not any text
#: in braces, which a password can be. A port can be a field too (localhost:{port}/...),
#: so before a "/" it is no password either. Each part bounded, as the pattern's are,
#: so that a line of unfinished ones is scanned in linear time.
_URL_FIELD = (
    r"(?:\{\{?[A-Za-z_][\w.]{0,63}(?:![sra])?\}?\}|\{\d{0,3}(?:![sra])?\}"
    + r"|\$\{[A-Za-z_]\w{0,64}\}|\$[A-Z_][A-Z0-9_]{0,64}|%\([A-Za-z_]\w{0,63}\)[sd]"
    + r"|%[sd]|<[A-Za-z_][\w.-]{0,63}>)"
)

#: Or a placeholder word or mask, but only as the whole password: before a "/" it is
#: the start of a real one ("pass/W0rd9").
_URL_PLACEHOLDER = (
    r"(?:"
    + _URL_FIELD
    + r"|\*{1,64}|x{1,64}|X{1,64}|pass(?:w(?:or)?d)?|pwd?|secret"
    + r"|changeme)"
)


#: The parts of a URL with a password in its userinfo: scheme, "://", user (redis's
#: may be empty, Azure's holds an @, none holds a bracket, so an IPv6 host's colons
#: are not taken for one), ":". The scheme is anchored, and bounded like the rest, so
#: that a long line is scanned in linear time.
_SCHEME_NAME = r"[a-zA-Z][a-zA-Z0-9+.-]{0,31}:"
_URL_SCHEME = r"(?<![a-zA-Z0-9+.-])" + _SCHEME_NAME
#: After a quote, ' or ", what says the string goes on: not the end of a string --
#: followed by , . ) ] } ; : + > or a space (an HTML attribute's end), as in ['http://h:8080','ops@x'] or
#: 'https://h'.format(...) -- or the pattern runs from one string into the next.
_QUOTE_GOES_ON = r"(?![.,\])};:+>\s])"
#: The user: like the password (``_URL_PASSWORD_CHAR``), not past a quote that closes
#: a string -- or it ran from one into the next: ['https://example.com','mailto:a@b'].
#: Without an @ in it (``_URL_USER_NO_AT``) or with one (``_URL_USER_WITH_AT``), whose
#: password may hold no "/". Inside double quotes, where a ' ends nothing, any '
#: (``_URL_USER_DOUBLE_QUOTED``, ``_URL_USER_DOUBLE_QUOTED_NO_AT``).
_URL_USER_DOUBLE_QUOTED = r"//[^\s/:\"?#\[\]]{0,256}:"
_URL_USER_DOUBLE_QUOTED_NO_AT = r"//[^\s/:\"?#\[\]@]{0,256}:"
#: The user without an @ in it, and with one.
_URL_USER_CHAR = r"(?:\\[^\s@]|[^\s/:\"?#\[\]'\\@]|['\"]" + _QUOTE_GOES_ON + ")"
_URL_USER_NO_AT = r"//" + _URL_USER_CHAR + r"{0,256}:"
_URL_USER_WITH_AT = (
    r"//" + _URL_USER_CHAR + r"{0,256}@" + r"(?:" + _URL_USER_CHAR + r"|@){0,256}:"
)
#: What follows that ":" is no password when it is a port -- digits, any number of them
#: and none at all among them (an empty port is a valid one), then the path,
#: query or fragment (http://host:8080/@scope/pkg, redis://cache:6379/0?x=a@b) --
#: whatever the scheme, as RFC 3986 reads it: a URL's userinfo ends at its first "/",
#: so a postgres URL with "8080/abc" there has a host and a port, not a password.
#: Nor when it is a template's field in a port's place -- a shell default among them,
#: ${PORT:-5432}, which is no password's placeholder: its default can be a real one --
#: or a placeholder.
_URL_PORT_FIELD = r"(?:" + _URL_FIELD + r"|\$\{[A-Za-z_]\w{0,64}:?[-=]\d{1,16}\})"
_URL_NOT_A_PASSWORD = (
    r"(?!\d{1,16}[/?#]|/)(?!"
    + _URL_PORT_FIELD
    + r"[/?#])(?!"
    + _URL_PLACEHOLDER
    + r"@)"
)
#: A character of the password: it may hold ' (base64 and generated ones do), but
#: not a quote that closes a string literal -- one followed by , ) ] } ; : + or a
#: space -- or the pattern ran from one string into the next
#: (['http://h:8080','ops@x']). A backslash and what follows it are one character, so
#: an escaped quote (\') closes nothing.
_URL_PASSWORD_CHAR = r"(?:\\[^\s@]|\\(?=@)|[^\s@\"?#'\\]|['\"]" + _QUOTE_GOES_ON + ")"
#: The password: with no "/" in it, any length -- the scan stops at the next "/", and
#: every URL after it has one, so it stays linear -- and it may hold ? and #, as
#: generated ones do; or with a "/", which ends userinfo only by the RFC and is
#: common in base64, up to 256 characters with no ? or #, which would then be a
#: query or fragment.
_URL_PASSWORD = (
    r"(?:(?:\\[^\s/@]|\\(?=@)|[^\s@\"'/\\]|['\"]"
    + _QUOTE_GOES_ON
    + ")+|"
    + _URL_PASSWORD_CHAR
    + r"{1,256})"
)

# SHAPE_PATTERNS recognise a credential by its own shape, with no help from a
# name: `sk-ant-...`, `AKIA...`, a PEM block, a service-account blob. Nothing
# excuses these. A string shaped like a live key *is* a live key as far as this
# is concerned -- a comment calling it a fixture is exactly what somebody would
# write to smuggle one past, and a fixture that really is shaped like a key
# should be redacted from a review diff anyway.
SHAPE_PATTERNS = [
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
    # GitHub's other token kinds: fine-grained, app, user-to-server, refresh.
    re.compile(r"github_pat_[0-9A-Za-z_]{40,}"),
    re.compile(r"gh[sur]_[A-Za-z0-9]{36,}"),
    # A Google API key: with agy on by default, the diff goes to Google itself.
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"(?:sk|rk)_live_[0-9a-zA-Z]{20,}"),
    re.compile(r"xox[abprs]-[0-9A-Za-z-]{10,}"),
    # Slack app tokens and incoming webhooks, Discord webhooks, SendGrid keys.
    re.compile(r"xapp-\d-[A-Za-z0-9-]{20,}"),
    re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{20,}"),
    re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{50,}"),
    re.compile(r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"),
    # A password in a URL's userinfo (``_URL_USER_NO_AT``), "@" -- in a postgres, redis or
    # proxy URL -- unless it is no password (``_URL_NOT_A_PASSWORD``): documentation
    # and connection-string code are full of those, and a redaction aborts the
    # review. Written in parts, so that this line is not one.
    re.compile(
        _URL_SCHEME + _URL_USER_NO_AT + _URL_NOT_A_PASSWORD + _URL_PASSWORD + r"@"
    ),
    # A user holding an @ (Azure's user@server) with a password that has no "/": with
    # one, it is an scp-style git address with a revision -- a "/" path, then "@v1" --
    # not a password (git+ssh://git@github.com:org/repo.git@v1).
    re.compile(
        _URL_SCHEME
        + _URL_USER_WITH_AT
        + _URL_NOT_A_PASSWORD
        + r"(?:\\[^\s/@]|\\(?=@)|[^\s@\"'/\\]|['\"]"
        + _QUOTE_GOES_ON
        + r")+@"
    ),
    # Inside double quotes, where a ' is no string's end and ends nothing: the password
    # runs to the "@" (K7a');s9 is one).
    re.compile(
        r'"'
        + _URL_SCHEME
        + _URL_USER_DOUBLE_QUOTED_NO_AT
        + _URL_NOT_A_PASSWORD
        + r'[^"\s@]{1,256}@'
    ),
    # And with an @ in the user, a password with no "/", as outside quotes.
    re.compile(
        r'"'
        + _URL_SCHEME
        + _URL_USER_DOUBLE_QUOTED
        + _URL_NOT_A_PASSWORD
        + r'[^"\s@/]{1,256}@'
    ),
    # A URL no quote opens (YAML, an .ini, prose): a quote in it ends no string, so
    # its password may hold one (a YAML dsn whose password has a ' and a comma). Slash-free,
    # so a path with an @ further on is no password.
    re.compile(
        r"(?<![A-Za-z0-9+.\-'\"`])"
        + _SCHEME_NAME
        + _URL_USER_NO_AT
        + _URL_NOT_A_PASSWORD
        + r"[^\s@/]{1,256}@"
    ),
    # And with no user name, where nothing before the ":" is a host, so nothing after
    # it is a port.
    re.compile(
        _URL_SCHEME
        + r"//:(?!"
        + _URL_PLACEHOLDER
        + r"@)"
        + _URL_PASSWORD_CHAR
        + r"{1,256}@"
    ),
    # Google's OAuth access and refresh tokens and client secrets, and the package
    # registries' tokens.
    re.compile(r"ya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(r"1//0[0-9A-Za-z_-]{30,}"),
    re.compile(r"GOCSPX-[0-9A-Za-z_-]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{30,}"),
    re.compile(r"npm_[A-Za-z0-9]{36}"),
    re.compile(r"pypi-AgE[0-9A-Za-z_-]{50,}"),
    re.compile(r"glpat-[a-zA-Z0-9\-]{20,}"),
    re.compile(r"(?i)DefaultEndpointsProtocol=https;AccountName="),
    re.compile(r'"type"\s*:\s*"service_account"'),
]

#: Every pattern, for callers and tests that do not care about the split.
CREDENTIAL_PATTERNS = [*KEYWORD_PATTERNS, *SHAPE_PATTERNS]

#: A line a linter flagged as a possible hardcoded credential and a person
#: then marked as a fixture. ruff's `S105` (a name like `token` or `password`
#: assigned a literal), `S106` (the same as a keyword argument) and `S107` (as
#: a default) are the three checks that ask this question, so their
#: suppressions are the three answers worth honouring.
_REVIEWED_FIXTURE = re.compile(r"#\s*noqa:[^#\n]*\bS10[567]\b")

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


def _is_secret_file(diff_header: str) -> bool:
    """Check if a diff header names a file that is entirely a credential.

    The mirror image of :func:`_is_safe_file`, and deliberately different in
    three ways, each because the safe-file version's choice is the wrong one
    here:

    * *Any* side naming a secret is enough, not both. A rename is a way to move
      a token's content into a file with an innocent name, and the content is
      the secret either way.
    * Every header form, not just ``diff --git``. A ``diff --cc`` section from
      a merge commit carries bare paths with no ``a/``/``b/`` prefix, and that
      section still holds the file's contents.
    * Words that are not paths are read as paths anyway. ``diff`` and
      ``--git`` are not going to be in the sets below, and a false positive
      here costs a reviewer one redacted hunk while a false negative costs a
      credential.
    """
    return any(_is_secret_path(part) for part in diff_header.split())


def _is_secret_path(part: str) -> bool:
    """Whether one word from a diff header names key material."""
    if part.startswith("-"):
        # `--cc`, `--git`, `--combined`. Not paths, and `PurePath` would read
        # them as relative names with confusing suffixes.
        return False
    path = part[2:] if part.startswith(("a/", "b/")) else part
    name = PurePath(path)
    return (
        path in SECRET_PATHS
        or name.name in SECRET_NAMES
        or name.suffix.lower() in SECRET_SUFFIXES
    )


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


def scrub_line(
    line: str,
    in_safe_file: bool,
    *,
    in_hunk: bool = True,
    in_secret_file: bool = False,
) -> str:
    """Replace a line with a redaction marker if it matches any pattern.

    Args:
        line: The diff line to check.
        in_safe_file: If True, this line is part of a safe file
            (the scrubber itself or its test suite) that contains
            credential patterns as string literals, not real secrets.
            Only lines matching :func:`_is_intentional_fixture_line`
            bypass scrubbing in safe files.
        in_secret_file: If True, this line belongs to a file whose whole
            content is a credential (:func:`_is_secret_file`). Every body line
            is redacted, whatever is in it, and the safe-file bypass does not
            apply -- a file cannot be both.

    Returns:
        The original line, or a redaction marker that preserves the
        diff prefix (``+``, ``-``, or `` ``) so the patch remains
        syntactically valid.
    """
    if _is_metadata(line, in_hunk=in_hunk):
        # Headers are structure, not content. A file *named* `.env.example`
        # is not a secret, and redacting its header breaks the patch.
        return line
    if in_secret_file:
        # Before any content heuristic, and not subject to the safe-file
        # bypass. There is no pattern that would have caught this line on its
        # own -- that is the whole reason for reading the path.
        return _redact_preserving_prefix(line)
    if in_safe_file and _is_intentional_fixture_line(line):
        return line
    # Match the body, not the `+`/`-`/` ` the diff format puts in front of it.
    # `+` is neither a line start nor one of the delimiters the anchored
    # patterns accept, so `^`-anchored patterns could never fire on an added
    # line -- `+.env.production` went through untouched while the same text in
    # prose was redacted.
    body = _body(line, in_hunk=in_hunk)
    for pattern in SHAPE_PATTERNS:
        if pattern.search(body):
            return _redact_preserving_prefix(line)
    # A linter already asked the keyword question and a person already answered
    # it. `S105`/`S106`/`S107` are ruff's "possible hardcoded password", so a
    # line carrying the suppression is one somebody looked at and called a
    # fixture -- which is exactly the judgement this scrubber cannot make for
    # itself, and the alternative was aborting a whole review over
    # `TOKEN = "token-for-tests"`.
    #
    # Only the keyword patterns, and deliberately: the shapes above have
    # already had their say, because a comment calling a live `sk-ant-` key a
    # fixture is what somebody would write to smuggle one past.
    if _REVIEWED_FIXTURE.search(body):
        return line
    for pattern in KEYWORD_PATTERNS:
        if pattern.search(body):
            return _redact_preserving_prefix(line)
    return line


#: A terminal colour or style escape, as git writes one.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _force_utf8(stream: object) -> None:
    """Pin a standard stream to UTF-8 with replacement.

    Diffs carry whatever bytes the repository holds -- latin-1 sources,
    `--text` output of near-binary files -- and CI often runs under `LC_ALL=C`,
    which makes stdout ASCII. Under the ambient codec either one raises
    mid-stream, and the exception escapes after an unknown number of lines have
    already been written: a truncated patch whose tail was never scrubbed.
    Replacement characters in a reviewer's diff are a far better outcome.

    And lines split at "\n" alone, as git splits them: read with universal newlines,
    a lone "\r" ended a line, and what followed it was a line of its own with no
    +, - or space -- taken for metadata, and passed unscrubbed.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:  # pragma: no branch - always a TextIOWrapper
        reconfigure(encoding="utf-8", errors="replace", newline="\n")


def _key_open_after(line: str, before: bool) -> bool:
    """Whether a private key's body continues past ``line``: what its last BEGIN or END
    marker says, or ``before`` if it has neither. The last, not the first: a line can
    end one key and begin another (END ... BEGIN), whose body is on the lines after."""
    marks = [(m.start(), True) for m in _PEM_BEGIN.finditer(line)]
    marks += [(m.start(), False) for m in _PEM_END.finditer(line)]
    return max(marks)[1] if marks else before


def _opens_key(line: str) -> bool:
    """Whether ``line`` begins a private key whose body is on the lines after it."""
    return _key_open_after(line, False)


def scrub_text() -> int:
    """``--text``: scrub stdin as plain text rather than a diff -- every line is
    content, as an added line is -- writing it to stdout. Returns the number of
    lines redacted. For what goes into a prompt beside the diff: full mode's context
    bundle quotes project files, and is sent wherever the diff is."""
    n_redacted = 0
    in_key = False
    for line in sys.stdin:
        if in_key:
            # A private key's body, after its redacted BEGIN line, to its END line.
            clean = _redact_preserving_prefix("+" + line)[1:]
            in_key = _key_open_after(line, True)
        else:
            # Read as the audit's quotes are (``redacts``), the literal URL check among
            # it: text is not source, and a quote in it ends no string.
            content = line.rstrip("\n")
            clean = (
                _redact_preserving_prefix("+" + line)[1:] if redacts(content) else line
            )
            # A key on one line -- JSON- or \n-escaped -- opens no block: its END is on
            # that line too, and everything after it was redacted as its body.
            in_key = clean != line and _opens_key(line)
        sys.stdout.write(clean)
        if REDACTED in clean and clean != line:
            n_redacted += 1
    return n_redacted


#: What a redacted string becomes, outside a diff: the marker without the "# " that
#: keeps a redacted diff line a comment.
REDACTED_TEXT = REDACTED.removeprefix("# ")


#: A URL's password in text that is not source: a decoded JSON value, a quoted line.
#: There a quote ends no string literal, so the source patterns' rules for quotes do
#: not apply (alice:p4ss',x@db is a password). Not in the diff's own scrub, where a
#: wrong match aborts the review; here it only replaces a value.
_LITERAL_URL_PASSWORD = re.compile(
    _URL_SCHEME
    + r"//[^\s/:?#\[\]]{0,256}:"
    # No port to mistake, either: a wrong match here only replaces a value, so digits
    # then ? or # (2024#Sec9) are read as the password they may be.
    + r"(?!"
    + _URL_PLACEHOLDER
    + r"@)[^\s@/]{1,256}@"
)


def redacts(text: str) -> bool:
    """Whether the scrubber would redact any line of ``text`` read as an added line of
    a diff, or it holds a URL password read literally (``_LITERAL_URL_PASSWORD``) --
    the one test the audit's quotes and a JSON document's strings share."""
    # At "\n" alone: splitlines() also broke at "\r" and Unicode's separators, and a
    # credential's name and value on either side of one went through apart.
    for part in text.split("\n"):
        added = "+" + part + "\n"
        if scrub_line(added, False) != added or _LITERAL_URL_PASSWORD.search(part):
            return True
    return False


@functools.lru_cache(maxsize=1024)
def _secret_field(key: str) -> bool:
    """Whether ``key`` names a credential: whether the scrubber would redact it
    assigned an ordinary-looking value. The value is not put in the probe -- one
    holding a quote or a newline changed what the line said, and passed. OAuth's own
    names are named outright: the line patterns do not read access_token as one."""
    if re.fullmatch(r"(?:access|refresh|id|auth|bearer)[_-]?token", key, re.IGNORECASE):
        return True
    return any(
        redacts(probe)
        for probe in (f'{key} = "Zq7xK9mP2w4t"', f'"{key}": "Zq7xK9mP2w4t"')
    )


def _holds_value(value: object) -> bool:
    """Whether a field's value could be a credential: a string or number with some
    length to it, not an empty one or a flag -- or a list or object that is not empty,
    under a key that names a credential ({"token": ["..."]}), whose leaves would lose
    that key's context once walked into."""
    if isinstance(value, (list, dict)):
        return bool(value)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    return len(str(value)) >= 4


def scrub_json() -> int:
    """``--json``: scrub a JSON document's strings -- every line of each is content, as
    an added line is -- and write it back out, still JSON. Returns the number of
    strings redacted. For the preflight audit, which quotes the files under review: a
    project's own installed copy of the preflight may be too old to scrub its quotes.

    Keys too, and a value with its key: a password field's value says, beside its key,
    what it does not alone -- as the same assignment does in a diff.
    """
    redacted = 0

    def walk(node: object) -> object:
        nonlocal redacted
        if isinstance(node, str):
            if redacts(node):
                redacted += 1
                return REDACTED_TEXT
            return node
        if isinstance(node, list):
            # A private key kept as a list of lines: its body lines say nothing alone,
            # so from a BEGIN to its END every string is redacted.
            cleaned: list[object] = []
            in_key = False
            for item in node:
                if isinstance(item, str) and (in_key or _opens_key(item)):
                    in_key = _key_open_after(item, in_key)
                    redacted += 1
                    cleaned.append(REDACTED_TEXT)
                else:
                    cleaned.append(walk(item))
            return cleaned
        if isinstance(node, dict):
            out: dict[str, object] = {}
            for index, (key, value) in enumerate(node.items()):
                name = walk(key)
                if name != key:
                    # Distinct from every other key, the document's own included: a
                    # collision dropped one of the two values.
                    name = f"{REDACTED_TEXT} {index}"
                    while name in node or name in out:
                        name += "_"
                if _secret_field(key) and _holds_value(value):
                    redacted += 1
                    out[str(name)] = REDACTED_TEXT
                    continue
                out[str(name)] = walk(value)
            return out
        return node

    document = walk(json.load(sys.stdin))
    json.dump(document, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return redacted


def main() -> None:
    """Read stdin, scrub, write to stdout. Exit 1 if any redactions."""
    _force_utf8(sys.stdin)
    _force_utf8(sys.stdout)

    if sys.argv[1:] and sys.argv[1:] not in (["--text"], ["--json"]):
        # Not diff mode by default: a line there without a +, - or space passes
        # unscrubbed, and a caller that asked for another mode would get exit 0.
        print(f"# scrub_diff.py: unknown arguments {sys.argv[1:]!r}", file=sys.stderr)
        sys.exit(2)
    if sys.argv[1:] in (["--text"], ["--json"]):
        text = sys.argv[1] == "--text"
        n_text = scrub_text() if text else scrub_json()
        if n_text:
            what = "line(s)" if text else "string(s)"
            print(f"# scrub_diff.py: {n_text} {what} redacted", file=sys.stderr)
            sys.exit(1)
        return

    n_redacted = 0
    in_safe = False
    in_secret = False
    in_key = False
    in_hunk = False

    for raw_line in sys.stdin:
        # Colour codes off for reading: a diff in colour (color.diff=always) has an
        # escape before every "+", and read as it came, no line was content. Written
        # out as it came unless redacted -- a file's own escapes are its content.
        line = _ANSI.sub("", raw_line)
        # Fail closed on every header form, not just `diff --git`. `in_safe`
        # used to latch: a `diff --cc` section from a merge commit following a
        # safe file inherited its bypass, and any line in it containing
        # `re.compile(` -- which is to say any Python file defining a regex --
        # passed through unscrubbed.
        if line.startswith("diff "):
            in_safe = line.startswith("diff --git ") and _is_safe_file(line)
            # Every header form, unlike `in_safe`: a `diff --cc` section
            # naming the token file is still the token file. See
            # `_is_secret_file`.
            in_secret = _is_secret_file(line)
            in_key = False
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        if in_key and not _is_body_line(line, in_hunk=in_hunk):
            in_key = False

        if in_key:
            clean = _redact_preserving_prefix(line)
            in_key = _key_open_after(line, True)
        else:
            clean = scrub_line(line, in_safe, in_hunk=in_hunk, in_secret_file=in_secret)
            # Only a *redacted* BEGIN line opens a block. In a safe file the
            # banner is an intentional fixture and passes through, and the
            # fixture's body must not then be swallowed. Nor does a key whose END
            # is on the same line (JSON- or \n-escaped), or the rest of the hunk
            # was redacted as its body.
            if clean != line and _opens_key(line):
                in_key = True

        sys.stdout.write(raw_line if clean == line else clean)
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
