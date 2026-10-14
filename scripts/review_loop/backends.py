"""CLI backend detection and a thin subprocess wrapper."""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Any

from .config import AGY_MODELS_TIMEOUT, REPO_ROOT

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
# rather than assumed, and nothing agy offers can show it. Measured on agy 1.3.4: under
# --mode=accept-edits it wrote a file OUTSIDE its workspace, by absolute path, with no
# denial recorded -- so the workspace is not a boundary. Under --mode=plan it never
# wrote, in five tries, even when told the write was pre-approved; but nothing was ever
# denied either, so that is the model declining, not agy refusing. And there is no deny
# list like claude's --disallowedTools; --sandbox restricts terminal commands only.
#
# So the boundary is the operating system's: agy runs under bubblewrap (agy_sandbox).
# Measured under it with --mode=accept-edits, the worst case, agy's own write_to_file
# failed with "read-only file system". The flags below stay as defence in depth, and
# agy_is_confined() proves the wrapper -- with no model call -- before the slot is used.
AGY_SAFETY_ARGS: tuple[str, ...] = (
    "--mode=plan",
    "--sandbox",
    "--disable-slash-commands",
)

#: Prepended to the reviewer prompt for agy only. Measured: given the shared prompt, the
#: model's first act was `ls -la`; headless mode denied it, and a denied tool call ends
#: the turn with no answer -- status SUCCESS, empty response. The claude reviewers have
#: Read/Grep/Glob and use them; agy has nothing worth reading, so it is told so.
AGY_PREAMBLE = (
    "You need no tools for this review and must not call any. The diff and the audit"
    " below are the whole input, and your working directory is deliberately empty."
    " Do not run commands, list or read files, or write a plan: a tool call is refused"
    " here, and a refused tool call ends your turn with no answer, which discards your"
    " review. Read the material below and reply with the JSON object it asks for.\n\n"
)


def agy_enabled() -> bool:
    """Whether the agy slot is switched on: by default, wherever agy is installed.

    ``REVIEW_USE_AGY=0`` switches it off. A machine without agy, or where its sandbox
    cannot be proven, falls through to the next backend for slot 3.
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
    # Not from the repository: whatever agy runs in, it treats as its workspace.
    try:
        done = _run(
            ["agy", "models"],
            cwd=_Path(tempfile.gettempdir()),
            stdin=subprocess.DEVNULL,
            timeout=AGY_MODELS_TIMEOUT,
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
    """The agy invocation for a reviewer, before the sandbox wrapper.

    The prompt goes in on stdin as one stream-json line (``agy_review_message``), not
    ``-p``: a single argument is capped at 128 KB on Linux, which a large diff passes
    before agy even starts. agy exits once stdin closes and the turn completes.

    ``--print-timeout 0`` is "no limit" (the default on 1.3.4; builds before 1.3
    defaulted to five minutes). The loop owns the deadline: it scales with the diff and
    REVIEWER_TIMEOUT can raise it past any ceiling fixed here.
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
        "0",
        *extra,
    ]


def agy_state_dir() -> _Path:
    """Where agy keeps its sign-in, conversations, plans and helper binaries."""
    return _Path.home() / ".gemini" / "antigravity-cli"


#: Inside agy's state directory: what a sandboxed run gets read-only (its own helper
#: binaries -- writable, they are a place to plant a program the next *unsandboxed* agy
#: runs) and what it gets a per-run copy of (the sign-in token, which agy refreshes).
_AGY_STATE_SHARED = ("bin", "builtin")
_AGY_STATE_COPIED = ("antigravity-oauth-token",)

#: Sockets that run code outside any sandbox for whoever can connect: docker's runs a
#: container with the host filesystem mounted, and a read-only mount does not stop a
#: connect(). Masked where they exist. The user's runtime directory -- the D-Bus session
#: bus, whose systemd answers `systemd-run --user`, and other tools' sockets and tokens --
#: is replaced wholesale, and the variables that point at any of them are unset.
_HOST_SOCKETS = ("/run/docker.sock",)
_SOCKET_ENV = (
    "DBUS_SESSION_BUS_ADDRESS",
    "SSH_AUTH_SOCK",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "DOCKER_HOST",
)


def agy_seed_state(state: _Path) -> _Path:
    """Fill a fresh directory to stand in for agy's state during one sandboxed run.

    agy's real state is not mounted writable: a run that could write it could leave a
    ``settings.json`` granting every command, for the next unsandboxed agy to obey. It
    gets a copy of the sign-in token and nothing else; the helpers are mounted
    read-only over it by ``agy_sandbox``. Conversations and plans land here and are
    thrown away with it.
    """
    real = agy_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    for name in _AGY_STATE_COPIED:
        if (real / name).is_file():
            shutil.copy2(real / name, state / name)
    return state


def agy_sandbox(workspace: _Path, state: _Path) -> list[str]:
    """The bubblewrap prefix agy runs under. Everything is read-only except ``workspace``.

    What it closes, each measured or reported by a review of this wrapper:

    - writes anywhere but the workspace (``--ro-bind / /``);
    - the home directory, read whole -- ``~/.ssh``, cloud credentials, other tools'
      tokens -- replaced by an empty one holding only agy and its state;
    - agy's real state, replaced by ``state`` (see ``agy_seed_state``);
    - host processes, and through ``/proc/<pid>/cwd`` their writable view of the
      filesystem (``--unshare-pid``); a terminal to inject input into
      (``--new-session``, CVE-2017-5226);
    - sockets that run code outside the sandbox (``_HOST_SOCKETS``, the user runtime
      directory, ``_SOCKET_ENV``).

    The network is left alone: agy is a client of a hosted model. The bash launcher in
    docs/ensemble-review.md builds its wrapper by calling this, so there is one copy.
    """
    home, real = _Path.home(), agy_state_dir()
    wrap = [
        "bwrap",
        "--unshare-pid",
        "--unshare-ipc",
        "--new-session",
        "--die-with-parent",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
    ]  # fmt: skip
    runtime = _Path(f"/run/user/{os.getuid()}")
    if runtime.is_dir():
        wrap += ["--tmpfs", str(runtime)]
    for sock in _HOST_SOCKETS:
        if _Path(sock).exists():
            wrap += ["--ro-bind", "/dev/null", sock]
    wrap += ["--tmpfs", str(home)]
    exe = shutil.which("agy")
    if exe:
        for path in dict.fromkeys((exe, str(_Path(exe).resolve()))):
            wrap += ["--ro-bind", str(_Path(exe).resolve()), path]
    wrap += ["--bind", str(state), str(real)]
    for name in _AGY_STATE_SHARED:
        if (real / name).is_dir():
            wrap += ["--ro-bind", str(real / name), str(real / name)]
    wrap += ["--bind", str(workspace), str(workspace)]
    for var in _SOCKET_ENV:
        wrap += ["--unsetenv", var]
    return [*wrap, "--"]


def agy_unsandboxed() -> bool:
    """Whether the host is trusted to confine agy itself (a disposable container).

    ``MMCRA_SKIP_AGY_SANDBOX_CHECK=1`` -- and not ``=0``, which reads as "do not skip"
    and must not turn the sandbox off.
    """
    return os.environ.get("MMCRA_SKIP_AGY_SANDBOX_CHECK", "") not in ("", "0")


def agy_message(prompt: str) -> str:
    """One prompt, framed the way ``--input-format stream-json`` reads it."""
    return json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"


def agy_review_message(prompt: str) -> str:
    """The reviewer prompt as agy receives it: the preamble, then the shared prompt."""
    return agy_message(AGY_PREAMBLE + prompt)


def agy_result(stdout: str) -> dict[str, Any] | None:
    """The ``result`` payload of agy's stream-json output, or None if no turn completed.

    The stream is one event per line -- ``init``, ``step_update``s, then ``result``,
    which carries ``status``, ``response``, ``error`` on failure, and
    ``structured_output`` when ``--json-schema`` was given.
    """
    # split("\n"), not splitlines(): NDJSON separates on newlines only, and
    # splitlines() also breaks on U+0085 and U+2028, which JSON strings may carry raw.
    for line in reversed(stdout.split("\n")):
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


def agy_is_confined() -> tuple[bool, str]:
    """Verify, with no model call, the wrapper a reviewer runs agy under.

    The codex check's three steps -- it runs, it reads, it refuses a write outside the
    workspace -- then one per thing the wrapper claims to close: the workspace stays
    writable (or agy fails looking like a reviewer with nothing to say), the home
    directory is hidden, no host process is visible, no session bus or docker socket
    is reachable. A canary that survives because the sandbox never launched is not a
    pass.
    """
    if shutil.which("bwrap") is None:
        return False, (
            "bwrap (bubblewrap) is not installed, and nothing else can make agy"
            " read-only; install it, or set MMCRA_SKIP_AGY_SANDBOX_CHECK=1 on a host"
            " that is already sandboxed"
        )
    try:
        with (
            tempfile.TemporaryDirectory() as ws,
            tempfile.TemporaryDirectory() as out,
            tempfile.TemporaryDirectory() as st,
        ):
            workspace, canary = _Path(ws), _Path(out) / "canary.txt"
            canary.write_bytes(b"original")
            wrap = agy_sandbox(workspace, agy_seed_state(_Path(st)))

            def sh(script: str) -> subprocess.CompletedProcess[str]:
                return _run([*wrap, "sh", "-c", script], cwd=workspace, timeout=30)

            def holds(test: str, path: str) -> bool:
                said = sh(f"[ {test} {shlex.quote(path)} ] && echo YES").stdout or ""
                return "YES" in said

            # 1. does the sandbox run anything at all?
            ran = sh("echo SANDBOX_OK")
            if "SANDBOX_OK" not in (ran.stdout or ""):
                detail = ((ran.stderr or "").strip().splitlines() or [""])[-1][:200]
                hint = ""
                if "uid map" in detail or "userns" in detail or "namespace" in detail:
                    hint = (
                        " -- looks like the kernel is blocking unprivileged user"
                        " namespaces; see docs/codex-sandbox.md"
                    )
                return False, f"bwrap could not start: {detail}{hint}"
            # 2. can it read outside? a reviewer that cannot read the system cannot run.
            if "original" not in (sh(f"cat {shlex.quote(str(canary))}").stdout or ""):
                return False, "bwrap sandbox could not read outside the workspace"
            # 3. the one that matters: is a write outside the workspace REFUSED?
            sh(f"echo changed > {shlex.quote(str(canary))}")
            if not canary.is_file() or canary.read_bytes() != b"original":
                return False, "bwrap sandbox ALLOWED a write outside the workspace"
            # 4. and inside it, allowed? agy keeps its working files there.
            sh(f"echo ok > {shlex.quote(str(workspace / 'probe'))}")
            if not (workspace / "probe").is_file():
                return False, "bwrap sandbox refused a write inside agy's workspace"
            # 5. the escapes the wrapper exists to close beyond plain writes. The home
            # directory holds nothing but what the wrapper put there on purpose.
            home = _Path.home()
            allowed = {
                _Path(arg).relative_to(home).parts[0]
                for arg in wrap
                if arg != str(home) and _Path(arg).is_relative_to(home)
            }
            listed = sh(f"ls -A {shlex.quote(str(home))}").stdout or ""
            extra = set(listed.split()) - allowed
            if extra:
                return False, f"bwrap sandbox shows the home dir ({sorted(extra)[0]})"
            if holds("-e", f"/proc/{os.getpid()}"):
                return False, "bwrap sandbox shows host processes (no --unshare-pid)"
            for sock in (f"/run/user/{os.getuid()}/bus", *_HOST_SOCKETS):
                if holds("-S", sock):
                    return False, f"bwrap sandbox can reach {sock}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"agy sandbox check failed: {exc}"
    return True, "agy sandbox verified (read-only, home hidden, no pids or sockets)"


def detect_backends(verify_codex: bool = True) -> dict[str, bool]:
    """Detect which CLI backends are installed.

    Codex additionally has to PROVE it is confined. An unconfined codex is worse than
    no codex: the whole contract of this tool is that reviewers report and never edit.
    Set ``MMCRA_SKIP_CODEX_SANDBOX_CHECK=1`` to bypass (for hosts that are already
    externally sandboxed, e.g. a disposable container).

    agy is held to the same standard whenever it is installed and not switched off
    (``REVIEW_USE_AGY=0``): its sandbox is proven by ``agy_is_confined``.
    ``MMCRA_SKIP_AGY_SANDBOX_CHECK=1`` runs agy without the sandbox, for hosts that
    are already one.
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
    if found["agy"] and not agy_enabled():
        print("  agy: switched off (REVIEW_USE_AGY=0)")
        found["agy"] = False  # installed is not the same as in use
    if found["agy"]:
        if agy_model() is None:
            print("  agy: DISABLED -- there is no model to run")
            found["agy"] = False
        elif agy_unsandboxed():
            print("  agy: running WITHOUT its sandbox (MMCRA_SKIP_AGY_SANDBOX_CHECK)")
        else:
            ok, reason = agy_is_confined()
            print(f"  agy: {'' if ok else 'DISABLED for review -- '}{reason}")
            found["agy"] = ok
    return found
