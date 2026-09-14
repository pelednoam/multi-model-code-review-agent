"""CLI backend detection and a thin subprocess wrapper."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Any

from .config import REPO_ROOT

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
        check=False,
        **kwargs,
    )


def detect_backends() -> dict[str, bool]:
    """Detect which CLI backends are installed."""
    return {
        cli: shutil.which(cli) is not None
        for cli in ("hermes", "claude", "codex", "gemini")
    }


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


def detect_backends(verify_codex: bool = True) -> dict[str, bool]:
    """Detect which CLI backends are installed.

    Codex additionally has to PROVE it is confined. An unconfined codex is worse than
    no codex: the whole contract of this tool is that reviewers report and never edit.
    Set ``MMCRA_SKIP_CODEX_SANDBOX_CHECK=1`` to bypass (for hosts that are already
    externally sandboxed, e.g. a disposable container).
    """
    found = {
        cli: shutil.which(cli) is not None
        for cli in ("hermes", "claude", "codex", "gemini")
    }
    if found["codex"] and verify_codex and not os.environ.get("MMCRA_SKIP_CODEX_SANDBOX_CHECK"):
        ok, reason = codex_is_confined()
        if not ok:
            print(f"  codex: DISABLED for review -- {reason}")
            found["codex"] = False
    return found
