"""agy, the Antigravity CLI, as slot 3: its model, its sandbox, its stream.

agy is the second reviewer whose read-only-ness has to be shown rather than assumed,
and nothing agy offers can show it. Measured on agy 1.3.4: under --mode=accept-edits it
wrote a file OUTSIDE its workspace, by absolute path, with no denial recorded -- so the
workspace is not a boundary. Under --mode=plan it never wrote, in five tries, even when
told the write was pre-approved; but nothing was ever denied either, so that is the
model declining, not agy refusing. And there is no deny list like claude's
--disallowedTools; --sandbox restricts terminal commands only.

So the boundary is the operating system's: agy runs under bubblewrap (``agy_sandbox``).
Measured under it with --mode=accept-edits, the worst case, agy's own write_to_file
failed with "read-only file system". ``agy_is_confined`` proves the wrapper -- with no
model call -- before the slot is used.
"""

from __future__ import annotations

import functools
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import AGY_MODELS_TIMEOUT, RESULT_SCHEMA, env_flag
from .proc import run, signals_unwind

# The flags that remain once the sandbox is in place: --sandbox restricts terminal
# commands, and --disable-slash-commands keeps the contributor's diff from expanding as
# a command or skill. There is no --mode=plan: agy implements it as a slash command, so
# with expansion disabled it is a no-op -- agy says so on stderr -- and a flag that does
# nothing must not be listed as a protection.
AGY_SAFETY_ARGS: tuple[str, ...] = (
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

#: Said whenever agy takes slot 3, by both launchers: it is on by default, and this is
#: where the scrubbed diff and audit leave for Google.
AGY_EGRESS = "agy sends the diff and audit to Google (REVIEW_USE_AGY=0 to stop)"


def agy_enabled() -> bool:
    """Whether the agy slot is switched on: by default, wherever agy is installed.

    ``REVIEW_USE_AGY=0`` switches it off, and so does every other value that reads as
    off -- ``false``, ``no``, ``off``, empty -- because the wrong reading of this one
    sends the diff to a provider the user meant to keep it from. A machine without agy,
    or where its sandbox cannot be proven, falls through to the next backend for slot 3.
    """
    value = os.environ.get("REVIEW_USE_AGY")
    return value is None or value.strip().lower() not in ("0", "false", "no", "off", "")


def agy_unsandboxed() -> bool:
    """Whether the host is trusted to confine agy itself (a disposable container).

    ``MMCRA_SKIP_AGY_SANDBOX_CHECK=1`` (or true, yes, on). Anything else -- ``0``,
    ``false``, a typo -- keeps the sandbox: a value that reads as "do not skip" must
    not turn it off.
    """
    return env_flag("MMCRA_SKIP_AGY_SANDBOX_CHECK")


# -- the model -----------------------------------------------------------------


def agy_model() -> str | None:
    """The model agy runs, or None if there is none to run (``agy_model_choice``)."""
    return agy_model_choice()[0]


def agy_model_choice() -> tuple[str | None, str]:
    """The model agy runs -- ``AGY_MODEL`` if set, else the best one this account
    lists -- and how it was chosen, in words.

    Not a hard-coded default. Which slugs exist depends on the Google plan behind the
    sign-in and changes with releases, and an unknown slug is an error rather than a
    fallback -- so a fixed default would fail on any machine that lacks it. Asking
    ``agy models`` instead picks what this machine actually has, and moves to a newer
    model (Gemini 4 Argon, once the plan includes it) without anyone editing anything.
    """
    explicit = os.environ.get("AGY_MODEL")
    if explicit:
        return explicit, f"model {explicit} (AGY_MODEL)"
    if not shutil.which("agy"):
        return None, "agy is not installed"
    return _picked_agy_model()


@functools.cache
def _picked_agy_model() -> tuple[str | None, str]:
    """Pick once per run: the sandbox check and every round run the same model."""
    slugs = agy_models()
    pick = best_agy_model(slugs)
    if pick is None:
        return None, (
            f"AGY_MODEL is unset and none of the {len(slugs)} models in `agy models`"
            " is a Gemini model; set AGY_MODEL to use one anyway"
        )
    return pick, (
        f"AGY_MODEL is unset; picked {pick} (newest Gemini, highest effort,"
        f" of {len(slugs)} in `agy models`)"
    )


def agy_models() -> list[str]:
    """The model slugs ``agy models`` lists for this sign-in, or [] if it cannot say.

    Its output is a progress line, then one ``<slug>\\t<display name>`` per model.
    It runs under the same wrapper as a review -- it is sent no diff, but it is agy,
    signed in and on the network -- in an empty directory of its own, since whatever
    agy runs in it treats as its workspace: not the repository, nor a shared /tmp.
    """
    try:
        with (
            signals_unwind(),
            tempfile.TemporaryDirectory() as ws,
            tempfile.TemporaryDirectory() as st,
        ):
            workspace = Path(ws).resolve()
            cmd = ["agy", "models"]
            if not agy_unsandboxed():
                state = agy_seed_state(Path(st).resolve())
                cmd = [*agy_sandbox(workspace, state), *cmd]
            done = run(
                cmd,
                cwd=workspace,
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
#: Gemini models that do not answer in text: they cannot write a review, so newer is
#: not better. ``gemini-3.9-flash-image`` must not outrank ``gemini-3.8-flash-high``.
_NOT_A_REVIEWER = re.compile(r"-(image|tts|audio|live|embedding|embed|computer-use)\b")
_EFFORT = {"low": 0, "medium": 1, "high": 2}


def _agy_rank(slug: str) -> tuple[tuple[int, ...], int, int] | None:
    """How good a reviewer ``slug`` should be, or None if it is not a Gemini model.

    Gemini only: this slot exists to add a third provider family, and the Claude and
    GPT models agy also offers duplicate what slots 1, 2 and 4 already run. Nor a
    Gemini model that makes images, speech or embeddings rather than text. Among
    Gemini models: newest version first; then tier, with Lite below Flash below
    everything else (Pro, Argon, and whatever comes next); then reasoning effort, a
    slug without one counting as medium.
    """
    match = _GEMINI_VERSION.match(slug)
    if match is None or _NOT_A_REVIEWER.search(slug):
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


# -- the command and what it is sent -------------------------------------------


def agy_command(model: str, *extra: str) -> list[str]:
    """The agy invocation for a reviewer, before the sandbox wrapper.

    The prompt goes in on stdin as one stream-json line (``agy_message``), not
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


@functools.cache
def agy_schema(path: Path = RESULT_SCHEMA) -> str:
    """The result schema at ``path`` as one line of JSON, for ``--json-schema``.

    Inline, not a path: the sandbox hides the home directory and /tmp, where a checkout
    usually lives, so the file would not exist for agy. Raises OSError if it is missing
    and ValueError if it is not a schema -- checked here, because agy rejects a bad one
    only after launch, when the slot can no longer go to another backend.
    """
    schema = json.loads(path.read_text(encoding="utf-8"))
    try:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
        from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
    except ImportError:  # the loop runs without it; validate_review_results does not
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"{path} is not an object schema") from None
    else:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValueError(f"{path} is not a valid schema: {exc.message}") from exc
    return json.dumps(schema)


def agy_message(prompt: str) -> str:
    """One prompt, framed the way ``--input-format stream-json`` reads it."""
    return json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"


# -- the sandbox ---------------------------------------------------------------


def agy_state_dir() -> Path:
    """Where agy keeps its sign-in, conversations, plans and helper binaries."""
    return Path.home() / ".gemini" / "antigravity-cli"


#: Inside agy's state directory: what a sandboxed run gets read-only (its own helper
#: binaries -- writable, they are a place to plant a program the next *unsandboxed* agy
#: runs) and what it gets a per-run copy of (the sign-in token, which agy refreshes).
_AGY_STATE_SHARED = ("bin", "builtin")
_AGY_STATE_COPIED = ("antigravity-oauth-token",)

#: Sockets outside /run and /tmp that run code for whoever can connect -- docker's runs
#: a container with the host filesystem mounted -- masked where they exist. The check
#: refuses any other socket left reachable (``_check_sockets``); these are the ones
#: known to be common enough to mask rather than lose the slot over.
_HOST_SOCKETS = (
    "/var/run/docker.sock",
    "/var/snap/lxd/common/lxd/unix.socket",
    "/var/lib/lxd/unix.socket",
    "/var/lib/incus/unix.socket",
)

#: The only environment agy gets. The rest -- API keys, cloud tokens, socket addresses --
#: is one read of /proc/self/environ away from a file tool, with the network open.
#: Given to bwrap by ``env -i``, not as ``--setenv`` arguments: bwrap's command line is
#: world-readable in /proc for the whole run, and a proxy URL can carry a password.
#: env's own command line holds the values only until it execs the next program.
_AGY_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "TERM", "TZ",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)  # fmt: skip


def agy_seed_state(state: Path) -> Path:
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


def _homes() -> list[Path]:
    """Every directory that is this user's home: ``$HOME``, and the account's own.

    They differ wherever HOME is pointed elsewhere -- CI, ``sudo -E``, a wrapper script,
    a nested agent -- and hiding only ``$HOME`` then leaves ``~/.ssh`` readable. Parents
    first, so a home inside the other is mounted on top of it, not under it.

    Raises OSError when a home cannot be hidden, and the sandbox check then fails rather
    than the run going on with a home it could not account for: an account with no
    passwd entry (a container under a bare numeric UID), whose home is unknown; or a
    home of ``/``, which cannot be replaced without replacing everything.
    """
    try:
        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError as exc:
        raise OSError(
            f"uid {os.getuid()} has no passwd entry; its home is unknown"
        ) from exc
    homes = {Path.home().resolve(), account.resolve()}
    if Path("/") in homes:
        raise OSError("a home directory of / cannot be hidden from agy")
    return sorted(homes, key=lambda h: len(h.parts))


def _resolver_dir() -> Path | None:
    """The directory under /run that DNS needs back, or None.

    /etc/resolv.conf is usually a link into /run (systemd-resolved, NetworkManager),
    which the sandbox replaces.
    """
    resolver = Path("/etc/resolv.conf").resolve().parent
    if resolver.is_relative_to("/run") and resolver != Path("/run"):
        return resolver
    return None


def _maskable(sock: str) -> bool:
    """Whether ``sock`` exists outside /run, which the sandbox replaces anyway.

    One the user cannot even stat -- under a root-only directory -- is one the user
    cannot reach, so it needs no mask; that is not an error to lose the slot over.
    """
    try:
        return os.path.lexists(sock) and not Path(sock).resolve().is_relative_to("/run")
    except OSError:
        return False


def agy_sandbox(workspace: Path, state: Path) -> list[str]:
    """The bubblewrap prefix agy runs under: ``agy_sandbox_parts``, joined."""
    env, bwrap = agy_sandbox_parts(workspace, state)
    return [*env, *bwrap]


def agy_sandbox_parts(workspace: Path, state: Path) -> tuple[list[str], list[str]]:
    """The bubblewrap prefix agy runs under, as ``env -i VAR=val...`` and bwrap's part.

    Everything is read-only except ``workspace``. The parts are separate so that a
    launcher can put a command between them: the bash runner's ``timeout`` goes after
    env, so its command line, readable by any local user in /proc, holds no values.

    It replaces the home directories (``_homes``), /tmp and /run with empty ones --
    putting back only the resolver's directory for DNS -- masks ``_HOST_SOCKETS``,
    gives agy a throwaway copy of its state (``agy_seed_state``), runs bwrap as PID 1
    of a new process namespace and session, and clears the environment to
    ``_AGY_ENV`` with ``env -i`` before bwrap starts: bwrap's own /proc/1/environ
    would otherwise hand every API key to a file read. What that closes and what it
    does not -- the network, mainly -- is docs/agy-sandbox.md; ``agy_is_confined``
    proves it.

    The bash launcher in docs/ensemble-review.md builds its wrapper by calling this
    (through ``runner_cli``), so there is one copy.
    """
    real = agy_state_dir()
    # bwrap by absolute path: env -i has emptied PATH by the time it is looked up.
    wrap = [
        shutil.which("bwrap") or "bwrap",
        "--unshare-pid",
        "--unshare-ipc",
        "--new-session",
        "--die-with-parent",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
    ]  # fmt: skip
    resolver = _resolver_dir()
    if resolver is not None:
        wrap += ["--ro-bind", str(resolver), str(resolver)]
    for sock in _HOST_SOCKETS:
        if _maskable(sock):
            wrap += ["--ro-bind", "/dev/null", sock]
    for home in _homes():
        wrap += ["--tmpfs", str(home)]
    exe = shutil.which("agy")
    if exe:
        target = str(Path(exe).resolve())
        for path in dict.fromkeys((exe, target)):
            wrap += ["--ro-bind", target, path]
    wrap += ["--bind", str(state), str(real)]
    for name in _AGY_STATE_SHARED:
        if (real / name).is_dir():
            wrap += ["--ro-bind", str(real / name), str(real / name)]
    wrap += ["--bind", str(workspace), str(workspace)]
    keep = [f"{var}={os.environ[var]}" for var in _AGY_ENV if var in os.environ]
    return [shutil.which("env") or "env", "-i", *keep], [*wrap, "--"]


# -- proving the sandbox -------------------------------------------------------


def _canary_parent() -> str | None:
    """Where to put a canary the sandbox leaves visible: not /tmp, which it hides."""
    return "/var/tmp" if os.access("/var/tmp", os.W_OK) else None


class _Box:
    """One wrapper under test, and a way to run a shell script inside it."""

    def __init__(self, wrap: list[str], workspace: Path) -> None:
        self.wrap = wrap
        self.workspace = workspace
        self.stderr = ""
        self.returncode = 0

    def sh(self, script: str, **kw: Any) -> str:
        """What ``script`` printed inside the sandbox; its status is ``returncode``."""
        done = run(
            [*self.wrap, "sh", "-c", script], cwd=self.workspace, timeout=30, **kw
        )
        self.stderr = done.stderr or ""
        self.returncode = done.returncode
        return done.stdout or ""

    def mounted_under(self, root: Path) -> set[str]:
        """The names directly under ``root`` that the wrapper mounted there."""
        # Mount destinations only -- an environment value such as a PATH under HOME is
        # not a mount. Paths, not strings: HOME=/home/user/ is the same directory.
        # --tmpfs DEST, and --bind/--ro-bind SRC DEST (and their -try forms).
        dests = []
        for i, arg in enumerate(self.wrap):
            if arg == "--tmpfs":
                dests.append(Path(self.wrap[i + 1]))
            elif arg.endswith("-bind"):
                dests.append(Path(self.wrap[i + 2]))
        return {
            d.relative_to(root).parts[0]
            for d in dests
            if d.is_relative_to(root) and d != root
        }


def _probe(box: _Box, canary: Path, roots: list[Path]) -> dict[str, str]:
    """Everything the check asks inside the sandbox, from one launch of it.

    One script with its answers marked, where one launch per question had the check
    start bwrap a dozen times. The two writes are in it too; whether they landed is
    read from outside.
    """
    # Opens each answer; what follows, up to the next mark, is it. New each run: a file
    # name the sandbox fails to hide can hold a line break and a mark of its own, and
    # one that could be guessed would rewrite the answers after it.
    mark = f"@@mmcra-probe-{secrets.token_hex(16)}@@"
    q = shlex.quote
    questions = {
        "agy": "agy --version >/dev/null 2>&1 && echo AGY_OK",
        # Read first, then try to overwrite.
        "canary": f"cat {q(str(canary))}; echo changed > {q(str(canary))}",
        "workspace": f"echo ok > {q(str(box.workspace / 'probe'))}",
        "pidns": "readlink /proc/self/ns/pid",
        # Every process's environment, not only the shell's: bwrap is PID 1 inside.
        "env": 'printf %s "$MMCRA_ENV_PROBE"; cat /proc/[0-9]*/environ'
        " | tr '\\0' '\\n' | grep -q '^MMCRA_ENV_PROBE=' && echo LEAK",
        **{f"ls {i}": f"ls -A1 {q(str(root))}" for i, root in enumerate(roots)},
    }
    script = "\n".join(
        f"echo {mark}{key}; {{ {ask}; }} 2>/dev/null" for key, ask in questions.items()
    )
    said = box.sh(script, env={**os.environ, "MMCRA_ENV_PROBE": "x"})
    answers: dict[str, list[str]] = {}
    for line in said.split("\n"):
        if line.startswith(mark):
            answers[line[len(mark) :]] = []
        elif answers:
            answers[next(reversed(answers))].append(line)
    return {key: "\n".join(lines) for key, lines in answers.items()}


def _check_runs(box: _Box) -> str | None:
    # First, alone: a canary that survives because the sandbox never launched is not a
    # pass, and why it did not launch is in its stderr.
    if "SANDBOX_OK" in box.sh("echo SANDBOX_OK"):
        return None
    detail = (box.stderr.strip().splitlines() or [""])[-1][:200]
    hint = ""
    if "uid map" in detail or "userns" in detail or "namespace" in detail:
        hint = (
            " -- looks like the kernel is blocking unprivileged user namespaces;"
            " see docs/codex-sandbox.md"
        )
    return f"bwrap could not start: {detail}{hint}"


# Each check below reads the probe's answers and returns why the wrapper fails, or None.


def _check_agy_starts(seen: dict[str, str]) -> str | None:
    # A wrapper that hides agy's runtime passes every other check and leaves a slot
    # that never answers.
    return (
        None
        if "AGY_OK" in seen.get("agy", "")
        else "agy does not start inside its sandbox"
    )


def _check_files(seen: dict[str, str], canary: Path, workspace: Path) -> str | None:
    # A reviewer that cannot read the system cannot run.
    if "original" not in seen.get("canary", ""):
        return "bwrap sandbox could not read outside the workspace"
    # The one that matters: was the write outside the workspace REFUSED?
    if not canary.is_file() or canary.read_bytes() != b"original":
        return "bwrap sandbox ALLOWED a write outside the workspace"
    # And inside it, allowed? agy keeps its working files there, and fails without
    # them looking like a reviewer with nothing to say.
    if not (workspace / "probe").is_file():
        return "bwrap sandbox refused a write inside agy's workspace"
    return None


def _check_hidden(seen: dict[str, str], box: _Box, roots: list[Path]) -> str | None:
    for i, root in enumerate(roots):
        listed = {name for name in seen.get(f"ls {i}", "").split("\n") if name}
        extra = listed - box.mounted_under(root)
        if extra:
            return f"bwrap sandbox shows {root}/{sorted(extra)[0]}"
    return None


def _check_processes(seen: dict[str, str]) -> str | None:
    # A namespace, compared as one: a host PID can exist inside too (the loop as PID 1
    # in a container), so looking one up proves nothing.
    inner = seen.get("pidns", "").strip()
    if not inner or inner == os.readlink("/proc/self/ns/pid"):
        return "bwrap sandbox shares the host's processes"
    return None


def _check_environment(seen: dict[str, str]) -> str | None:
    if seen.get("env", "").strip():
        return "bwrap sandbox passes the host environment through"
    return None


#: One socket in /proc/net/unix, as the kernel prints it: ``%pK: %08X %08X %08X %04X
#: %02X %5lu``, then a space and the name if it has one, to the end of the line.
_UNIX_LINE = re.compile(
    r"[0-9A-Fa-f]+: [0-9A-F]{8} [0-9A-F]{8} [0-9A-F]{8} [0-9A-F]{4} [0-9A-F]{2} +\d+"
    r"(?: (.*))?"
)


def _unix_names(unix: str) -> list[str]:
    """Every name bound in /proc/net/unix, paths and ``@abstract`` ones, whole.

    Raises ValueError if ``unix`` is not that file: an empty or unreadable list would
    pass every check that reads it. And for a line that is not a socket's: the kernel
    prints a name's bytes raw, so a name with a line break in it is cut in two, and
    the half checked would not be the socket that exists.
    """
    lines = unix.split("\n")
    if not lines[0].startswith("Num "):
        raise ValueError("could not read the socket list (/proc/net/unix)")
    names = []
    for line in filter(None, lines[1:]):
        match = _UNIX_LINE.fullmatch(line)
        if match is None:
            # repr: the line is a name someone else chose, escapes and all.
            raise ValueError(
                "a socket's name has a line break in it, so the socket list cannot be"
                f" read ({line[:80]!r})"
            )
        if match.group(1):
            names.append(match.group(1))
    return names


def _check_sockets(box: _Box, names: list[str]) -> str | None:
    """No socket ``names`` lists can be connected to from inside, but DNS's.

    /proc/net/unix lists the sockets of this network namespace, which the sandbox
    shares, by the path each was bound at. Every one of those the sandbox can still
    reach is refused, not a list of known ones, which always lags what is installed.
    The resolver's directory is put back on purpose, for DNS. A socket bound in
    another namespace (a container's), or whose directory has since moved, is not
    listed at its reachable path: docs/agy-sandbox.md says so.
    """
    relative = [n for n in names if not n.startswith(("/", "@"))]
    if relative:
        return f"a socket bound by a relative name ({relative[0]!r}) cannot be checked"
    paths = sorted({n for n in names if n.startswith("/")})
    # On stdin, one NUL-terminated path each: not in the script, which a busy host's
    # list would push past the 128 KB limit on one argument.
    # Each batch exits 0 whatever it found, so a status of anything else is the probe
    # failing -- no xargs, a sandbox that did not start -- which must not read as a
    # sandbox that reaches nothing.
    probe = (
        "xargs -0 sh -c 'for s; do"
        ' [ -S "$s" ] && [ -w "$s" ] && printf "%s\\0" "$s";'
        " done; exit 0' sh"
    )
    reached = ""
    if paths:
        reached = box.sh(probe, input="".join(f"{p}\0" for p in paths))
        if box.returncode != 0:
            detail = (box.stderr.strip().splitlines() or ["no detail"])[-1][:200]
            return f"could not check the sockets agy's sandbox can reach: {detail}"
    allowed = _resolver_dir()
    for path in reached.split("\0"):
        if path and not (allowed and Path(path).is_relative_to(allowed)):
            return f"bwrap sandbox can reach {path}"
    # Abstract sockets belong to the network namespace, which agy keeps to reach its
    # model, so no mount hides them. A session bus -- dbus-launch's under /tmp, IBus's
    # under ~/.cache -- starts any program it is asked to.
    for name in names:
        if name.startswith("@") and "/dbus-" in name:
            return f"bwrap sandbox can reach a session bus ({name})"
    return None


_X_DISPLAY = re.compile(r"@/tmp/\.X11-unix/X(\d+)$")


def _check_x_servers(names: list[str]) -> str | None:
    """No X server in ``names`` lets this user in without credentials.

    Its abstract socket is reachable from inside, and one that admits a client with no
    cookie -- stock GNOME and Xwayland grant ``SI:localuser`` -- takes keystrokes into a
    terminal outside the sandbox. Asked from here, which is the same thing: the server
    decides by the connecting uid, and inside it is this one.
    """
    for name in names:
        match = _X_DISPLAY.match(name)
        if match and _x_admits(name):
            display = f":{match.group(1)}"
            return (
                f"the X server {display} admits this user without a cookie, and a"
                f" program inside could type into a terminal outside; `xhost"
                f" -si:localuser:$USER` (on DISPLAY={display}) closes that"
            )
    return None


def _x_admits(name: str) -> bool:
    """Whether the X server at abstract socket ``name`` accepts a client with no auth."""
    # The connection setup: little-endian, protocol 11.0, no authorization name or
    # data. The first byte of the reply is 1 for Success, 0 Failed, 2 Authenticate.
    setup = b"l\0" + struct.pack("<HHHHH", 11, 0, 0, 0, 0)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(2)
            conn.connect("\0" + name[1:])
            conn.sendall(setup)
            return conn.recv(1) == b"\x01"
    except OSError:
        return False


def _check_reachable(box: _Box, unix: str) -> str | None:
    """No socket and no X server ``unix`` (/proc/net/unix) lists is reachable."""
    try:
        names = _unix_names(unix)
    except ValueError as exc:
        return str(exc)
    return _check_sockets(box, names) or _check_x_servers(names)


def agy_reachable(wrap: list[str], workspace: Path) -> str | None:
    """What the sandbox ``wrap`` can reach right now that it must not, or None.

    The startup check describes the host as it was then. A loop runs for half an hour,
    and a session bus, a socket or an X server that appears later would be reachable
    from every later round, so this part of it runs again before each launch. The list
    is read here: the sandbox shares this network namespace, so it is the same list.
    """
    try:
        unix = Path("/proc/net/unix").read_text(encoding="utf-8", errors="replace")
        return _check_reachable(_Box(wrap, workspace), unix)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not check what agy's sandbox can reach: {exc}"


def agy_is_confined() -> tuple[bool, str]:
    """Verify, with no model call, the wrapper a reviewer runs agy under.

    It runs, agy starts in it, it reads outside the workspace but writes only inside,
    both homes, /tmp and /run hold only what was put there, the process namespace is
    not the host's, the environment is cleared, and nothing that runs code outside --
    a socket, a session bus, an X server -- is reachable.
    """
    if shutil.which("bwrap") is None:
        return False, (
            "bwrap (bubblewrap) is not installed, and nothing else can make agy"
            " read-only; install it, or set MMCRA_SKIP_AGY_SANDBOX_CHECK=1 on a host"
            " that is already sandboxed"
        )
    parent = _canary_parent()
    if parent is None:
        return False, (
            "agy sandbox check needs a writable /var/tmp for its canary (the sandbox"
            " hides /tmp); make it writable, or set MMCRA_SKIP_AGY_SANDBOX_CHECK=1"
            " on a host that is already sandboxed"
        )
    try:
        with (
            signals_unwind(),
            tempfile.TemporaryDirectory() as ws,
            tempfile.TemporaryDirectory(dir=parent) as out,
            tempfile.TemporaryDirectory() as st,
        ):
            workspace, canary = Path(ws).resolve(), Path(out) / "canary.txt"
            canary.write_bytes(b"original")
            # An empty state: this checks the mounts, which need no sign-in, so there
            # is no reason for a copy of the token to exist while it runs.
            box = _Box(agy_sandbox(workspace, Path(st).resolve()), workspace)
            failure = _check_runs(box)
            if failure:
                return False, failure
            roots = [*_homes(), Path("/tmp"), Path("/run")]
            seen = _probe(box, canary, roots)
            # Read here, not inside: the sandbox shares this network namespace, so it
            # is the same list, and a name in it cannot reach the probe's answers.
            unix = Path("/proc/net/unix").read_text(encoding="utf-8", errors="replace")
            failure = (
                _check_agy_starts(seen)
                or _check_files(seen, canary, workspace)
                or _check_hidden(seen, box, roots)
                or _check_processes(seen)
                or _check_environment(seen)
                or _check_reachable(box, unix)
            )
            if failure:
                return False, failure
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"agy sandbox check failed: {exc}"
    return True, "agy sandbox verified (read-only; home, /tmp, /run hidden; no env)"


# -- its output ----------------------------------------------------------------


def ran_agy(round_dir: Path, slot: int) -> bool:
    """Whether agy is the backend that answered ``slot`` (``backend-N.txt``).

    Only agy's output is read as an agy stream. Any reviewer can be talked into
    printing a line shaped like agy's ``result`` event -- one with a failed status
    would silence a real review -- so the shape alone decides nothing.
    """
    try:
        named = (round_dir / f"backend-{slot}.txt").read_text(encoding="utf-8")
    except OSError:
        return False
    return named.strip() == "agy"


def agy_result(stdout: str) -> dict[str, Any] | None:
    """The ``result`` payload of agy's stream-json output, or None if no turn completed.

    The stream is one event per line -- ``init``, ``step_update``s, then ``result``,
    which carries ``status``, ``response``, ``error`` on failure, and
    ``structured_output`` when ``--json-schema`` was given.
    """
    # From the end, a line at a time: the result is the last event, and the stream --
    # every step_update -- can be large. On newlines only, not splitlines(): NDJSON
    # separates on newlines, and JSON strings may carry U+0085 and U+2028 raw.
    end = len(stdout)
    while end > 0:
        start = stdout.rfind("\n", 0, end) + 1
        line = stdout[start:end].strip()
        end = start - 1
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
