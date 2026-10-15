"""Which CLI backends this machine has, and whether each is safe to use."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .agy import agy_enabled, agy_model_choice, agy_unsandboxed
from .agy_sandbox_check import agy_is_confined
from .config import env_flag
from .proc import run


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
        canary = Path(tmp) / "canary.txt"
        canary.write_text("original", encoding="utf-8")

        # 1. does the sandbox run anything at all?
        ran = run(["codex", "sandbox", "--", "sh", "-c", "echo SANDBOX_OK"], cwd=Path(tmp))
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
        # The path as an argument, never spliced into the script: a TMPDIR with a space
        # in it split it in two, and a shell character in it would run.
        rd = run(_codex_sh('cat -- "$1"', canary), cwd=Path(tmp))
        if "original" not in (rd.stdout or ""):
            return False, "codex sandbox could not read a file; a reviewer needs read access"

        # 3. the one that matters: is a write REFUSED?
        run(_codex_sh('echo changed > "$1"', canary), cwd=Path(tmp))
        if canary.read_text(encoding="utf-8").strip() != "original":
            return False, (
                "codex sandbox ALLOWED a write -- it is not read-only on this host."
                " Check approvals_reviewer in ~/.codex/config.toml (auto_review escalates"
                " past -s read-only). See docs/codex-sandbox.md."
            )
    return True, "codex sandbox verified read-only (runs, reads, refuses writes)"


def codex_usable() -> tuple[bool, str]:
    """Whether codex may review, and why: its sandbox is proven, or the check skipped.

    ``MMCRA_SKIP_CODEX_SANDBOX_CHECK=1`` (or true, yes, on; not ``0``) skips it, for
    hosts that are already externally sandboxed, e.g. a disposable container.
    """
    if env_flag("MMCRA_SKIP_CODEX_SANDBOX_CHECK"):
        return True, "sandbox check skipped (MMCRA_SKIP_CODEX_SANDBOX_CHECK)"
    return codex_is_confined()


def _codex_sh(script: str, path: Path) -> list[str]:
    """``script`` run by sh inside codex's sandbox, with ``path`` as its ``$1``."""
    return ["codex", "sandbox", "--", "sh", "-c", script, "codex-check", str(path)]


def detect_backends(
    verify_codex: bool = True, prefer_hermes: bool = False, repo: Path | None = None
) -> dict[str, bool]:
    """Detect which CLI backends are installed.

    Codex additionally has to PROVE it is confined (``codex_usable``). An unconfined
    codex is worse than no codex: the whole contract of this tool is that reviewers
    report and never edit.

    agy is held to the same standard whenever it is installed and not switched off
    (``REVIEW_USE_AGY=0``): its sandbox is proven by ``agy_sandbox_check.agy_is_confined``. A project
    pinned to Bedrock (``prefer_hermes``) gets agy only with ``REVIEW_USE_AGY=1``.
    ``MMCRA_SKIP_AGY_SANDBOX_CHECK=1`` runs agy without the sandbox, for hosts that
    are already one.
    """
    found = {
        cli: shutil.which(cli) is not None
        for cli in ("hermes", "claude", "codex", "gemini", "agy")
    }
    if found["codex"] and verify_codex:
        ok, reason = codex_usable()
        if not ok:
            print(f"  codex: DISABLED for review -- {reason}")
            found["codex"] = False
    if found["agy"]:
        found["agy"] = _agy_usable(prefer_hermes, repo)
    return found


def _agy_usable(prefer_hermes: bool, repo: Path | None = None) -> bool:
    """Whether the installed agy is to review -- wanted here, its sandbox proven, a
    model to run -- saying why not. ``repo``, the repository under review, is hidden
    from agy while it is checked and while it lists its models, as in a review."""
    also_hide = (repo,) if repo is not None else ()
    if prefer_hermes and not env_flag("REVIEW_USE_AGY"):
        # Pinned to Bedrock to keep the code with providers it chose: agy is not run at
        # all -- not its check, not a signed-in listing of its models -- unless asked
        # for by name.
        print(
            "  agy: not used, the project is pinned to Bedrock (REVIEW_USE_AGY=1 uses it)"
        )
        return False
    if not agy_enabled():
        # Installed is not the same as in use.
        print(f"  agy: switched off (REVIEW_USE_AGY={os.environ['REVIEW_USE_AGY']!r})")
        return False
    # The sandbox first: picking a model runs agy, and a host that will drop agy should
    # not run it at all.
    if agy_unsandboxed():
        print("  agy: running WITHOUT its sandbox (MMCRA_SKIP_AGY_SANDBOX_CHECK)")
    else:
        ok, reason = agy_is_confined(also_hide)
        print(f"  agy: {'' if ok else 'DISABLED for review -- '}{reason}")
        if not ok:
            return False
    model, how = agy_model_choice(also_hide)
    print(f"  agy: {how}" if model else f"  agy: DISABLED -- {how}")
    return model is not None
