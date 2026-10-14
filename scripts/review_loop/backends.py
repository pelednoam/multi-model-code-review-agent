"""CLI backend detection and a thin subprocess wrapper."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Any

from .config import AGY_CHECK_TIMEOUT, REPO_ROOT, REVIEWER_TIMEOUT_MAX

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
    """Whether the agy slot is switched on. Opt-in: it costs a model call to verify."""
    return os.environ.get("REVIEW_USE_AGY", "") == "1"


def agy_model() -> str | None:
    """The model agy runs, from ``AGY_MODEL``.

    No default. Slugs are whatever ``agy models`` lists, they change with releases, and
    an unknown one is an error rather than a fallback -- a guessed default would fail
    every round instead of saying once what is missing.
    """
    return os.environ.get("AGY_MODEL") or None


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
    The token exists only in the file, so a reply that contains it proves agy started,
    ran under these flags, and could read.

    One model call, so ``detect_backends`` runs it only when the slot is switched on.
    """
    if shutil.which("agy") is None:
        return False, "agy is not installed"
    with tempfile.TemporaryDirectory() as tmp:
        token = uuid.uuid4().hex
        canary = _Path(tmp) / "canary.txt"
        canary.write_text(token, encoding="utf-8")
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
        if token not in str(result.get("response") or ""):
            return False, (
                "agy did not read the canary, so the check proves nothing;"
                " a reviewer also needs read access"
            )
        if canary.read_text(encoding="utf-8").strip() != token:
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

    agy is held to the same standard, but only checked when ``REVIEW_USE_AGY=1``,
    because its check is a model call. ``MMCRA_SKIP_AGY_SANDBOX_CHECK=1`` bypasses it.
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
            print("  agy: DISABLED -- REVIEW_USE_AGY=1 but AGY_MODEL is unset")
            found["agy"] = False
        elif not os.environ.get("MMCRA_SKIP_AGY_SANDBOX_CHECK"):
            print(f"  agy: checking confinement ({model}, one model call)...")
            ok, reason = agy_is_confined(model)
            print(f"  agy: {'' if ok else 'DISABLED for review -- '}{reason}")
            found["agy"] = ok
    return found
