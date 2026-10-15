"""Which CLI backends this machine has, and whether each is safe to use."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .agy import agy_enabled, agy_is_confined, agy_model, agy_unsandboxed
from .config import env_flag
from .proc import _run


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
        ran = _run(["codex", "sandbox", "--", "sh", "-c", "echo SANDBOX_OK"], cwd=Path(tmp))
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
        rd = _run(["codex", "sandbox", "--", "sh", "-c", f"cat {canary}"], cwd=Path(tmp))
        if "original" not in (rd.stdout or ""):
            return False, "codex sandbox could not read a file; a reviewer needs read access"

        # 3. the one that matters: is a write REFUSED?
        _run(["codex", "sandbox", "--", "sh", "-c", f"echo changed > {canary}"], cwd=Path(tmp))
        if canary.read_text(encoding="utf-8").strip() != "original":
            return False, (
                "codex sandbox ALLOWED a write -- it is not read-only on this host."
                " Check approvals_reviewer in ~/.codex/config.toml (auto_review escalates"
                " past -s read-only). See docs/codex-sandbox.md."
            )
    return True, "codex sandbox verified read-only (runs, reads, refuses writes)"


def detect_backends(verify_codex: bool = True) -> dict[str, bool]:
    """Detect which CLI backends are installed.

    Codex additionally has to PROVE it is confined. An unconfined codex is worse than
    no codex: the whole contract of this tool is that reviewers report and never edit.
    Set ``MMCRA_SKIP_CODEX_SANDBOX_CHECK=1`` (or true, yes, on; not ``0``) to bypass,
    for hosts that are already externally sandboxed, e.g. a disposable container.

    agy is held to the same standard whenever it is installed and not switched off
    (``REVIEW_USE_AGY=0``): its sandbox is proven by ``agy.agy_is_confined``.
    ``MMCRA_SKIP_AGY_SANDBOX_CHECK=1`` runs agy without the sandbox, for hosts that
    are already one.
    """
    found = {
        cli: shutil.which(cli) is not None
        for cli in ("hermes", "claude", "codex", "gemini", "agy")
    }
    skip_codex = env_flag("MMCRA_SKIP_CODEX_SANDBOX_CHECK")
    if found["codex"] and verify_codex and not skip_codex:
        ok, reason = codex_is_confined()
        if not ok:
            print(f"  codex: DISABLED for review -- {reason}")
            found["codex"] = False
    if found["agy"] and not agy_enabled():
        print("  agy: switched off (REVIEW_USE_AGY=0)")
        found["agy"] = False  # installed is not the same as in use
    if found["agy"]:
        # The sandbox first: picking a model runs agy, and a host that will drop agy
        # should not run it at all.
        if agy_unsandboxed():
            print("  agy: running WITHOUT its sandbox (MMCRA_SKIP_AGY_SANDBOX_CHECK)")
        else:
            ok, reason = agy_is_confined()
            print(f"  agy: {'' if ok else 'DISABLED for review -- '}{reason}")
            found["agy"] = ok
    if found["agy"] and agy_model() is None:
        print("  agy: DISABLED -- there is no model to run")
        found["agy"] = False
    return found
