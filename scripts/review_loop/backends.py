"""CLI backend detection and a thin subprocess wrapper."""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Any

from .config import (
    AGY_CHECK_TIMEOUT,
    AGY_MODELS_TIMEOUT,
    REPO_ROOT,
    REVIEWER_TIMEOUT_MAX,
)

if TYPE_CHECKING:
    from pathlib import Path


def _run(
    cmd: list[str], cwd: Path | None = None, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run a command with sensible defaults."""
    return subprocess.run(
        cmd,
        cwd=cwd if cwd is not None else REPO_ROOT,
        capture_output=kwargs.pop("capture_output", True),
        text=True,
        # A repository's own bytes decide this, not the ambient locale: under
        # LC_ALL=C a single en dash in a source file made `git diff` raise.
        encoding="utf-8",
        errors="replace",
        check=False,
        **kwargs,
    )


# Codex is the only reviewer whose read-only-ness depends on the HOST, not just on a
# flag we pass. Two independent things can silently defeat it, and both were observed
# on a stock Ubuntu 24.04 box:
#
#   1. The sandbox cannot start. Ubuntu 23.10+ sets
#      kernel.apparmor_restrict_unprivileged_userns=1, which blocks bubblewrap from
#      creating a user namespace, so the sandbox never launches.
#   2. approvals_reviewer = "auto_review" in ~/.codex/config.toml auto-approves the
#      model's escalation requests "using the workspace-write sandbox" (codex's own
#      wording), which overrides `-s read-only` entirely.
#
# Either one turns a "read-only reviewer" into an agent that can edit the code it was
# asked to review. We pin what we can (see CODEX_SAFETY_ARGS in reviewers.py) and then
# PROVE the result with a canary rather than trusting it.
def codex_is_confined() -> tuple[bool, str]:
    """Verify that codex's sandbox actually refuses a write on THIS machine.

    Three steps, because a surviving canary on its own is ambiguous: the write may have
    been refused, or the sandbox may have failed to launch and never run the command at
    all. Those look identical from the outside, and telling them apart is the entire
    point of this check.

    Uses ``codex sandbox``, which exercises the same sandbox machinery with NO model
    call, so it costs nothing and can run on every launch.

    Returns ``(confined, reason)``.
    """
    if shutil.which("codex") is None:
        return False, "codex is not installed"
    with tempfile.TemporaryDirectory() as tmp:
        canary = _Path(tmp) / "canary.txt"
        canary.write_text("original", encoding="utf-8")

        # 1. does the sandbox run anything at all?
        ran = _run(["codex", "sandbox", "--", "sh", "-c", "echo SANDBOX_OK"], cwd=_Path(tmp))
        if "SANDBOX_OK" not in (ran.stdout or ""):
            detail = ((ran.stderr or ran.stdout or "").strip().splitlines() or [""])[-1][:200]
            hint = ""
            if "uid map" in detail or "RTM_NEWADDR" in detail or "userns" in detail:
                hint = (
                    " -- looks like the kernel is blocking unprivileged user namespaces"
                    " (kernel.apparmor_restrict_unprivileged_userns=1 on Ubuntu 23.10+)."
                    " See docs/codex-sandbox.md for a bwrap-scoped AppArmor profile."
                )
            return False, f"codex sandbox could not start: {detail}{hint}"

        # 2. can it read? a reviewer that cannot read is useless even if it is safe.
        rd = _run(["codex", "sandbox", "--", "sh", "-c", f"cat {canary}"], cwd=_Path(tmp))
        if "original" not in (rd.stdout or ""):
            return False, "codex sandbox could not read a file; a reviewer needs read access"

        # 3. the one that matters: is a write REFUSED?
        _run(["codex", "sandbox", "--", "sh", "-c", f"echo changed > {canary}"], cwd=_Path(tmp))
        if canary.read_text(encoding="utf-8").strip() != "original":
            return False, (
                "codex sandbox ALLOWED a write -- it is not read-only on this host."
                " Check approvals_reviewer in ~/.codex/config.toml (auto_review escalates"
                " past -s read-only). See docs/codex-sandbox.md."
            )
    return True, "codex sandbox verified read-only (runs, reads, refuses writes)"


# agy, the Antigravity CLI, is the second reviewer whose read-only-ness has to be shown
# rather than assumed, and it is the weaker case. Its headless docs say reads AND writes
# inside the active workspace are auto-allowed; there is no deny list like claude's
# --disallowedTools; --sandbox restricts terminal commands, not file edits; and --mode=plan
# is described as investigating "before writing code", not as forbidding it. So the slot
# is pinned three ways: plan mode and the terminal sandbox (AGY_SAFETY_ARGS), an empty
# workspace outside the repository (reviewers._agy_workspace), and a canary that has to
# survive before the slot is used at all (agy_is_confined).
AGY_SAFETY_ARGS: tuple[str, ...] = ("--mode=plan", "--sandbox")


def agy_enabled() -> bool:
    """Whether the agy slot is switched on: by default, wherever agy is installed.

    ``REVIEW_USE_AGY=0`` switches it off. On means one extra model call per run, for
    the confinement check, before the slot is trusted; a machine without agy, or with
    an agy that fails the check, falls through to the next backend for slot 3.
    """
    return os.environ.get("REVIEW_USE_AGY", "1") != "0"


def agy_model() -> str | None:
    """The model agy runs: ``AGY_MODEL`` if set, else the best one this account lists.

    Not a hard-coded default. Which slugs exist depends on the Google plan behind the
    sign-in and changes with releases, and an unknown slug is an error rather than a
    fallback -- so a fixed default would fail on any machine that lacks it. Asking
    ``agy models`` instead picks what this machine actually has, and moves to a newer
    model (Gemini 4 Argon, once the plan includes it) without anyone editing anything.
    """
    explicit = os.environ.get("AGY_MODEL")
    if explicit:
        return explicit
    return _picked_agy_model() if shutil.which("agy") else None


@functools.cache
def _picked_agy_model() -> str | None:
    """Pick once per run, and say what was picked and from what."""
    slugs = agy_models()
    pick = best_agy_model(slugs)
    if pick is None:
        print(
            f"  agy: AGY_MODEL is unset and none of the {len(slugs)} models in"
            " `agy models` is a Gemini model; set AGY_MODEL to use one anyway"
        )
    else:
        print(
            f"  agy: AGY_MODEL is unset; picked {pick} (newest Gemini, highest effort,"
            f" of {len(slugs)} in `agy models`)"
        )
    return pick


def agy_models() -> list[str]:
    """The model slugs ``agy models`` lists for this sign-in, or [] if it cannot say.

    Its output is a progress line, then one ``<slug>\\t<display name>`` per model.
    """
    try:
        done = _run(
            ["agy", "models"], stdin=subprocess.DEVNULL, timeout=AGY_MODELS_TIMEOUT
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [
        line.split("\t", 1)[0].strip()
        for line in (done.stdout or "").splitlines()
        if "\t" in line
    ]


_GEMINI_VERSION = re.compile(r"gemini-(\d+(?:\.\d+)*)")
_EFFORT = {"low": 0, "medium": 1, "high": 2}


def _agy_rank(slug: str) -> tuple[tuple[int, ...], int, int] | None:
    """How good a reviewer ``slug`` should be, or None if it is not a Gemini model.

    Gemini only: this slot exists to add a third provider family, and the Claude and
    GPT models agy also offers duplicate what slots 1, 2 and 4 already run. Among
    Gemini models: newest version first; then tier, with Lite below Flash below
    everything else (Pro, Argon, and whatever comes next); then reasoning effort, a
    slug without one counting as medium.
    """
    match = _GEMINI_VERSION.match(slug)
    if match is None:
        return None
    version = tuple(int(part) for part in match.group(1).split("."))
    rest = slug[match.end() :]
    tier = 0 if "lite" in rest else 1 if "flash" in rest else 2
    effort = _EFFORT.get(rest.rsplit("-", 1)[-1], 1)
    return version, tier, effort


def best_agy_model(slugs: list[str]) -> str | None:
    """The best reviewer among ``slugs`` by ``_agy_rank``, or None if none is Gemini."""
    ranked = [(rank, slug) for slug in slugs if (rank := _agy_rank(slug))]
    return max(ranked)[1] if ranked else None


def agy_command(model: str, *extra: str) -> list[str]:
    """The agy invocation shared by the reviewer and its confinement check.

    The prompt goes in on stdin as one stream-json line (``agy_message``), not through
    ``-p``: a single argument is capped at 128 KB on Linux, which a large diff passes
    before agy even starts. agy exits once stdin closes and the turn completes.

    ``--print-timeout`` is pinned to the loop's own ceiling. Builds before 1.3 defaulted
    to five minutes, shorter than the deadline the loop gives a reviewer on a large diff;
    1.3.4 defaults to no limit. It is here rather than on the reviewer alone so the
    confinement check exercises it: a flag some build rejects then disables the slot
    with a reason, once, instead of failing every round.
    """
    return [
        "agy",
        *AGY_SAFETY_ARGS,
        "--model",
        model,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{REVIEWER_TIMEOUT_MAX // 60}m",
        *extra,
    ]


def agy_message(prompt: str) -> str:
    """One prompt, framed the way ``--input-format stream-json`` reads it."""
    return json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"


def agy_result(stdout: str) -> dict[str, Any] | None:
    """The ``result`` payload of agy's stream-json output, or None if no turn completed.

    The stream is one event per line -- ``init``, ``step_update``s, then ``result``,
    which carries ``status``, ``response``, ``error`` on failure, and
    ``structured_output`` when ``--json-schema`` was given.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(event, dict)
            and event.get("event") == "result"
            and isinstance(event.get("result"), dict)
        ):
            result: dict[str, Any] = event["result"]
            return result
    return None


def agy_is_confined(model: str) -> tuple[bool, str]:
    """Verify that agy, run exactly as a reviewer runs, does not write to its workspace.

    Weaker than the codex check, and worth saying why: agy has no model-free sandbox
    subcommand, so this has to ask the *model* to write and watch whether the write
    lands. A refusal by policy and a refusal by the model look the same from here. What
    it does rule out is the failure that matters -- a write that goes through -- and the
    ambiguity the codex check was built around: a check that never ran is not a pass.
    The nonce exists only in the file, so a reply that contains it proves agy started,
    ran under these flags, and could read.

    One model call, so ``detect_backends`` runs it only when the slot is switched on.
    """
    if shutil.which("agy") is None:
        return False, "agy is not installed"
    with tempfile.TemporaryDirectory() as tmp:
        nonce = uuid.uuid4().hex
        canary = _Path(tmp) / "canary.txt"
        canary.write_text(nonce, encoding="utf-8")
        prompt = (
            "Automated permissions check. Read canary.txt in the current directory and"
            " quote its contents in your reply. Then replace the entire contents of"
            " canary.txt with the single word: changed."
        )
        try:
            done = _run(
                agy_command(model),
                cwd=_Path(tmp),
                input=agy_message(prompt),
                timeout=AGY_CHECK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return (
                False,
                f"agy did not finish the confinement check in {AGY_CHECK_TIMEOUT}s",
            )
        result = agy_result(done.stdout or "")
        if result is None or result.get("status") != "SUCCESS":
            said = (result or {}).get("error") or (
                (done.stderr or "").strip().splitlines() or ["no output"]
            )[-1]
            return False, (
                f"agy confinement check did not run: {str(said)[:200]}"
                " -- signed in? (run `agy` once interactively) AGY_MODEL valid? (`agy models`)"
            )
        if nonce not in str(result.get("response") or ""):
            return False, (
                "agy did not read the canary, so the check proves nothing;"
                " a reviewer also needs read access"
            )
        if canary.read_text(encoding="utf-8").strip() != nonce:
            return False, (
                "agy ALLOWED a write under --mode=plan -- it is not read-only on this host."
                " Check permissions.allow in ~/.gemini/antigravity-cli/settings.json."
            )
    return True, "agy verified: ran, read its workspace, did not write to it"


def detect_backends(verify_codex: bool = True) -> dict[str, bool]:
    """Detect which CLI backends are installed.

    Codex additionally has to PROVE it is confined. An unconfined codex is worse than
    no codex: the whole contract of this tool is that reviewers report and never edit.
    Set ``MMCRA_SKIP_CODEX_SANDBOX_CHECK=1`` to bypass (for hosts that are already
    externally sandboxed, e.g. a disposable container).

    agy is held to the same standard whenever it is installed and not switched off
    (``REVIEW_USE_AGY=0``); its check is a model call. ``MMCRA_SKIP_AGY_SANDBOX_CHECK=1``
    bypasses it.
    """
    found = {
        cli: shutil.which(cli) is not None
        for cli in ("hermes", "claude", "codex", "gemini", "agy")
    }
    if found["codex"] and verify_codex and not os.environ.get("MMCRA_SKIP_CODEX_SANDBOX_CHECK"):
        ok, reason = codex_is_confined()
        if not ok:
            print(f"  codex: DISABLED for review -- {reason}")
            found["codex"] = False
    if found["agy"] and agy_enabled():
        model = agy_model()
        if model is None:
            print("  agy: DISABLED -- there is no model to run")
            found["agy"] = False
        elif not os.environ.get("MMCRA_SKIP_AGY_SANDBOX_CHECK"):
            print(f"  agy: checking confinement ({model}, one model call)...")
            ok, reason = agy_is_confined(model)
            print(f"  agy: {'' if ok else 'DISABLED for review -- '}{reason}")
            found["agy"] = ok
    return found
