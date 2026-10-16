"""Collect the scrubbed diff and run the preflight subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

from .config import SCRIPTS_DIR, VENDORED_PATHS, diff_pathspec, is_agent_repo
from .proc import run

if TYPE_CHECKING:
    from pathlib import Path


#: scrub_diff.py exits 2 when it failed part-way through. What reached stdout
#: is a truncated patch whose tail was never scrubbed.
SCRUBBER_ABORTED = 2


class ScrubberFailedError(RuntimeError):
    """Raised when scrub_diff.py crashed instead of finishing.

    Distinct from :class:`SecretsDetectedError` because the remedy is
    different: nothing leaked that rotating a credential would fix, but the
    patch on disk is incomplete and must not be shown to a reviewer.
    """


class SecretsDetectedError(RuntimeError):
    """Raised when scrub_diff.py reports that secrets were redacted.

    The agent contract says secrets must never reach a reviewer -- the
    review must STOP so the user can rotate the secret and re-stage.
    Continuing with a partially-scrubbed diff would defeat the whole
    point of having a scrubber.
    """


#: What to diff against when the caller does not say. The common case: a
#: branch under review, against the trunk it will land on.
DEFAULT_BASE = "origin/main"


def _warn_of_vendored_changes(repo: Path, diff_range: list[str]) -> None:
    """Name the vendored review files this change touches, which are not reviewed.

    They are left out of a host project's diff (``diff_pathspec``) so that a fresh
    install is not spent reviewing the reviewer. But full mode runs the project's own
    copy -- its sandbox builder among them -- so a change that weakens it would run
    with no reviewer having seen it. Re-vendoring changes them too, so this warns
    rather than refuses. And it is itself part of that copy: a change that removes it
    removes the warning too. The protection is the one docs/agy-sandbox.md gives --
    run the loop from a trusted checkout of this agent, with --repo naming the
    project; this only makes the untrusted case visible where it is not doctored.
    ``diff_range`` is the range the diff was taken over, its fallbacks included, and
    new files count as they do there.
    """
    if is_agent_repo(repo):
        return
    changed = run(
        ["git", "diff", *diff_range, "--name-only", "--", *VENDORED_PATHS], cwd=repo
    )
    added = run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *VENDORED_PATHS],
        cwd=repo,
    )
    if changed.returncode != 0 or added.returncode != 0:
        print(
            "  WARNING: could not tell whether this change touches the reviewer's own"
            " vendored files, which are not reviewed"
        )
        return
    names = (changed.stdout or "").split() + (added.stdout or "").split()
    if names:
        shown = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
        print(
            f"  WARNING: this change touches the reviewer's own vendored files"
            f" ({shown}), which are not reviewed; full mode runs them. If they did"
            " not come from re-running install.sh, check them by hand."
        )


#: A diff the scrubber can read, whatever the user's git config says: color.diff=always
#: put an escape before every +, and the scrubber, seeing no +, passed each line; an
#: external diff tool or a textconv filter changes the lines altogether.
_PLAIN = ("--no-color", "--no-ext-diff", "--no-textconv")


def _git_diff(cmd: list[str], repo: Path) -> subprocess.CompletedProcess[str]:
    """A git diff's output, decoded but not translated: read as text, a lone "\r" in
    a changed line became a line break, and what followed it reached the scrubber as
    a line with no +, - or space -- metadata to it, and passed unscrubbed."""
    done = subprocess.run(
        cmd, cwd=repo, capture_output=True, stdin=subprocess.DEVNULL, check=False
    )
    return subprocess.CompletedProcess(
        cmd,
        done.returncode,
        done.stdout.decode("utf-8", errors="replace"),
        done.stderr.decode("utf-8", errors="replace"),
    )


def collect_diff(round_dir: Path, repo: Path, base: str = DEFAULT_BASE) -> tuple[Path, int]:
    """Collect the scrubbed diff between ``base`` and the working tree.

    The working tree includes files git does not track yet (``_untracked``):
    a new module is part of the change whether or not anybody has staged it.

    ``base`` defaults to ``origin/main``, which is right while the work is on a
    branch. It is wrong the moment the work is *pushed*: the merge-base diff is
    then empty, this falls back to ``HEAD~1``, and a review of "everything I
    did today" silently becomes a review of the last commit. Reviewing a
    range that is already on the trunk -- after a merge, or before a release --
    is a real thing to want, and there was no way to ask for it.

    Raises:
        SecretsDetectedError: scrub_diff.py redacted at least one line.
            The user must rotate the leaked secret and re-run.
        RuntimeError: If ``base`` names nothing git can resolve. Deliberately
            not a fallback: a caller that named a base meant it, and quietly
            reviewing ``HEAD~1`` instead is how you end up believing something
            was reviewed that never was.
    """
    if base != DEFAULT_BASE:
        named = run(["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd=repo)
        if named.returncode != 0:
            msg = f"--base {base!r} does not name a commit in this repository"
            raise RuntimeError(msg)
    diff_path = round_dir / "diff.patch"
    # Exclude the agent's own vendored files. install.sh copies them into the
    # host project, so without this a host's first review spends most of its
    # budget -- 60% of the diff on a fresh install -- reviewing the reviewer.
    pathspec = diff_pathspec(repo)
    untracked = _untracked(repo, pathspec)
    diff_range = ["--merge-base", base]
    git = _git_diff(["git", "diff", *_PLAIN, *diff_range, "--", *pathspec], repo)
    if git.returncode != 0:
        diff_range = ["HEAD~1"]
        fallback = _git_diff(
            ["git", "diff", *_PLAIN, *diff_range, "--", *pathspec], repo
        )
        if fallback.returncode != 0:
            raise RuntimeError(
                f"failed to collect diff: {git.stderr}\n{fallback.stderr}"
            )
        git = fallback
    elif not git.stdout.strip() and not untracked and base == DEFAULT_BASE:
        # Only for the default. An empty diff against a base somebody named is
        # an answer -- "nothing changed since there" -- not a reason to review
        # something else. And not when there are new files: those *are* the
        # change, and reviewing the last commit instead would review the
        # wrong thing without saying so.
        diff_range = ["HEAD~1"]
        git = _git_diff(["git", "diff", *_PLAIN, *diff_range, "--", *pathspec], repo)
    _warn_of_vendored_changes(repo, diff_range)
    diff_input = (git.stdout or "") + untracked
    # Every hop is pinned to UTF-8 with replacement. scrub_diff.py pins its own
    # streams because CI runs under LC_ALL=C, but that was the only hop that
    # did: git's output, the pipe into the scrubber and the read-back all used
    # the ambient codec, so one non-ASCII byte anywhere in the tree still
    # raised -- and this repository's own sources are full of en dashes.
    with open(diff_path, "w", encoding="utf-8", errors="replace") as out_f:
        scrubber = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "scrub_diff.py")],
            stdin=subprocess.PIPE,
            stdout=out_f,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _, scrub_err = scrubber.communicate(input=diff_input)
    if scrubber.returncode == SCRUBBER_ABORTED:
        raise ScrubberFailedError(
            "scrub_diff.py aborted part-way through. The diff on disk is "
            "truncated and was not fully scrubbed, so it must not be "
            "reviewed.\n"
            f"Scrubber stderr: {scrub_err.strip()}\n"
            f"Partial diff saved to: {diff_path}"
        )
    if scrubber.returncode != 0:
        raise SecretsDetectedError(
            "scrub_diff.py redacted at least one line from the diff. "
            "The leaked secret must be rotated, the diff re-staged, and "
            "the review re-run before proceeding.\n"
            f"Scrubber stderr: {scrub_err.strip()}\n"
            f"Redacted diff saved to: {diff_path}"
        )
    text = diff_path.read_text(encoding="utf-8", errors="replace")
    n_lines = len(text.splitlines())
    for note in dominant_files(text):
        print(f"  NOTE: {note}")
    return diff_path, n_lines


def _untracked(repo: Path, pathspec: list[str]) -> str:
    """The files git does not track yet, as a diff that adds each one.

    ``git diff`` compares *tracked* files with the base, so a module written
    and never ``git add``-ed was absent from the review -- silently, while the
    docstring above promised "the working tree". The preflight counted it
    ("Untracked: 1") and the reviewers never saw a line of it: a new file is
    the change most likely to be the whole point of a branch.

    Ignored files stay out (``--exclude-standard``), and the same pathspec
    applies, so an untracked copy of the vendored agent is still not reviewed.

    Raises:
        RuntimeError: If git cannot list the untracked files, or cannot diff
            one of them. A review that silently dropped a file is the failure
            this exists to end, so it does not get to happen quietly here.
    """
    listed = run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec],
        cwd=repo,
    )
    if listed.returncode != 0:
        raise RuntimeError(f"failed to list untracked files: {listed.stderr}")
    parts: list[str] = []
    for name in (one for one in listed.stdout.split("\0") if one):
        added = _git_diff(
            ["git", "diff", *_PLAIN, "--no-index", "--", "/dev/null", name], repo
        )
        # --no-index exits 1 when the two sides differ, which a new file
        # against /dev/null always does; anything else is git failing.
        if added.returncode not in (0, 1):
            raise RuntimeError(
                f"failed to diff untracked file {name!r}: {added.stderr}"
            )
        parts.append(added.stdout)
    return "".join(parts)


def _preflight_script(repo: Path) -> Path:
    """The preflight to run for ``repo`` -- its own installed copy if it has one.

    The preflight reads ``scripts/preflight/config.py`` *next to itself*:
    SOURCE_DIRS, TEST_DIRS, SIGNED_MANIFESTS, all of it. Running this repo's
    copy against another project therefore audits the wrong tree's layout, and
    does it silently -- the coverage gate reports "no Python file matched
    SOURCE_DIRS", the changed-file list belongs to the agent rather than the
    project, and reviewers reason from both. Prefer the copy install.sh put in
    the project.
    """
    installed = repo / "scripts" / "review_preflight.py"
    if installed.is_file() and not is_agent_repo(repo):
        return installed
    return SCRIPTS_DIR / "review_preflight.py"


def run_preflight(round_dir: Path, repo: Path) -> Path:
    """Run preflight audit, return path to audit JSON.

    review_preflight.py exits 1 when ``n_warnings > 0`` to signal humans
    that the tree has concerns worth eyeballing. That signal must NOT be
    fatal for the automated review loop -- if the audit JSON was written
    successfully, the reviewers can read the warnings from it and decide
    whether they're blocking. Only treat the run as failed if the audit
    JSON wasn't produced at all.
    """
    audit_path = round_dir / "audit.json"
    # Written raw beside it, scrubbed into it, and removed whatever happens: the round
    # directory is kept, and a raw audit there would be what the scrub keeps out.
    raw = round_dir / ".audit.raw.json"
    try:
        result = run(
            [sys.executable, str(_preflight_script(repo)), "--output", str(raw)],
            cwd=repo,
        )
        if not raw.exists():
            raise RuntimeError(
                f"preflight failed (no audit JSON, exit {result.returncode}): "
                f"{result.stderr}"
            )
        _requote_from_source(raw, repo)
        _scrub_audit(raw, audit_path)
    finally:
        raw.unlink(missing_ok=True)
    return audit_path


def _requote_from_source(raw: Path, repo: Path) -> None:
    """Redact, in the raw audit, each quoted line whose whole source line holds a
    credential. An installed preflight older than this checkout quotes lines cut to
    120 characters, and a credential cut at the boundary was only partly there for
    the scrub after it to see; the file and line it names are read whole here."""
    # Here, not at the top: the full-mode runner's private copy of this package has no
    # scrub_diff beside it, and imports this module (the package's __init__ does).
    from scripts.scrub_diff import REDACTED_TEXT, redacts

    try:
        audit = json.loads(raw.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return  # the scrub after this refuses it
    hits = audit.get("suspicious_patterns") if isinstance(audit, dict) else None
    if not isinstance(hits, list):
        return
    root = repo.resolve()
    changed = False
    for hit in hits:
        file = hit.get("file") if isinstance(hit, dict) else None
        number = hit.get("line") if isinstance(hit, dict) else None
        if not isinstance(file, str) or not isinstance(number, int) or number < 1:
            continue
        path = (root / file).resolve()
        if not path.is_relative_to(root):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            continue
        if number <= len(lines) and redacts(lines[number - 1]):
            hit["text"] = REDACTED_TEXT
            changed = True
    if changed:
        raw.write_text(json.dumps(audit), encoding="utf-8")


def _scrub_audit(raw: Path, audit_path: Path) -> None:
    """Scrub the audit in ``raw`` with this checkout's scrubber into ``audit_path``.

    The audit quotes the files under review, and goes into every prompt. The
    preflight that wrote it may be the project's own installed copy
    (``_preflight_script``), too old to scrub its quotes, so they are scrubbed here
    whichever wrote them. A redaction is not an abort, as it is for the diff: the
    quote is replaced, and the review goes on. One that cannot be done is.
    """
    with raw.open(encoding="utf-8", errors="replace") as source:
        done = run(
            [sys.executable, str(SCRIPTS_DIR / "scrub_diff.py"), "--json"], stdin=source
        )
    if done.returncode not in (0, 1):
        raise RuntimeError(f"could not scrub the audit: {done.stderr.strip()[-500:]}")
    audit_path.write_text(done.stdout, encoding="utf-8")


#: A single file bigger than this share of the diff is worth mentioning...
_DOMINANT_SHARE = 0.4

#: ...but only once it is big enough to matter. Half of a sixty-line diff is
#: just a small change to one file; half of a fifteen-thousand-line one is a
#: lockfile in front of four models.
_DOMINANT_MINIMUM = 500


def dominant_files(diff: str) -> list[str]:
    """Name any single file that is most of the diff.

    Reviewers are paid by the token and read in one pass, so a generated file
    left in the diff does not just waste money -- it crowds out the code. A
    lockfile was once 69% of a review: ten thousand lines of resolved dependency
    tree, in front of four models, instead of the change being reviewed.

    Reported rather than excluded. What counts as generated is the project's
    call, and ``.gitattributes`` is where that call belongs.
    """
    sizes = _lines_per_file(diff)
    total = sum(sizes.values())
    if total == 0:
        return []
    return [
        f"{path} is {count / total:.0%} of this diff ({count} lines). "
        f"If it is generated, `{path} -diff linguist-generated=true` in "
        f".gitattributes keeps it out of the review."
        for path, count in sorted(sizes.items(), key=lambda kv: -kv[1])
        if count / total >= _DOMINANT_SHARE and count >= _DOMINANT_MINIMUM
    ]


def _lines_per_file(diff: str) -> dict[str, int]:
    """How many lines of the diff belong to each file."""
    sizes: dict[str, int] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = _path_of(line)
            sizes.setdefault(current, 0)
        elif current:
            sizes[current] += 1
    return sizes


def _path_of(header: str) -> str:
    """The file a `diff --git` header is about."""
    for part in header.split():
        if part.startswith("b/"):
            return part[2:]
    return header
