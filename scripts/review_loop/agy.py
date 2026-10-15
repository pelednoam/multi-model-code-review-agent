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
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import AGY_MODELS_TIMEOUT, RESULT_SCHEMA, env_flag
from .proc import _run, signals_unwind

if TYPE_CHECKING:
    from collections.abc import Callable

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

    ``REVIEW_USE_AGY=0`` switches it off. A machine without agy, or where its sandbox
    cannot be proven, falls through to the next backend for slot 3.
    """
    return os.environ.get("REVIEW_USE_AGY", "1") != "0"


def agy_unsandboxed() -> bool:
    """Whether the host is trusted to confine agy itself (a disposable container).

    ``MMCRA_SKIP_AGY_SANDBOX_CHECK=1`` (or true, yes, on). Anything else -- ``0``,
    ``false``, a typo -- keeps the sandbox: a value that reads as "do not skip" must
    not turn it off.
    """
    return env_flag("MMCRA_SKIP_AGY_SANDBOX_CHECK")


# -- the model -----------------------------------------------------------------


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
            done = _run(
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
#: a container with the host filesystem mounted -- masked where they exist.
_HOST_SOCKETS = (
    "/var/run/docker.sock",
    "/var/snap/lxd/common/lxd/unix.socket",
    "/var/lib/lxd/unix.socket",
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
    """
    homes = {Path.home(), Path(pwd.getpwuid(os.getuid()).pw_dir)}
    return sorted((h for h in homes if h != Path("/")), key=lambda h: len(h.parts))


def agy_sandbox(workspace: Path, state: Path) -> list[str]:
    """The bubblewrap prefix agy runs under: ``agy_sandbox_parts``, joined."""
    env, bwrap = agy_sandbox_parts(workspace, state)
    return [*env, *bwrap]


def agy_sandbox_parts(workspace: Path, state: Path) -> tuple[list[str], list[str]]:
    """The bubblewrap prefix agy runs under, as ``env -i VAR=val...`` and bwrap's part.

    Everything is read-only except ``workspace``. The parts are separate so that a
    launcher can put a command between them: the bash runner's ``timeout`` goes after
    env, so its command line, readable by any local user in /proc, holds no values.

    What it closes, each measured or reported by a review of this wrapper:

    - writes anywhere but the workspace (``--ro-bind / /``);
    - the home directory, read whole -- ``~/.ssh``, cloud credentials, other tools'
      tokens -- both of them (``_homes``), and /tmp: replaced by empty ones holding
      only what agy needs;
    - agy's real state, replaced by ``state`` (see ``agy_seed_state``);
    - the environment, cleared to ``_AGY_ENV`` before bwrap starts (``env -i``): bwrap
      runs as PID 1 inside, and ``/proc/1/environ`` would otherwise hand every API key
      in the caller's environment to a file read, whatever the child was given;
    - host processes, and through ``/proc/<pid>/cwd`` their writable view of the
      filesystem (``--unshare-pid``); a terminal to inject input into
      (``--new-session``, CVE-2017-5226);
    - sockets that run code outside the sandbox. Most live under /run, replaced
      wholesale: docker's; the system D-Bus, where polkit lets a local session power off
      or mount disks; snapd's; the user's runtime directory with the session bus, whose
      systemd answers ``systemd-run --user``. Only the resolver's directory is put
      back, for DNS. /tmp holds tmux's server socket and X11's; ``_HOST_SOCKETS``
      names the known ones elsewhere.

    Not closed, and said so in the README: the network, which agy needs to reach its
    model -- and with it abstract-namespace sockets, localhost services and a cloud
    metadata endpoint -- and sockets outside /run and /tmp that ``_HOST_SOCKETS`` does
    not name. Headless agy refused the one terminal command it tried; whether its
    browser and MCP tools can reach the network unprompted has not been measured.

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
    # DNS: /etc/resolv.conf is usually a link into /run (systemd-resolved, NetworkManager).
    resolver = Path("/etc/resolv.conf").resolve().parent
    if resolver.is_relative_to("/run") and resolver != Path("/run"):
        wrap += ["--ro-bind", str(resolver), str(resolver)]
    for sock in _HOST_SOCKETS:
        if Path(sock).exists() and not Path(sock).resolve().is_relative_to("/run"):
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

    def sh(self, script: str, **kw: Any) -> str:
        """What ``script`` printed inside the sandbox."""
        done = _run(
            [*self.wrap, "sh", "-c", script], cwd=self.workspace, timeout=30, **kw
        )
        self.stderr = done.stderr or ""
        return done.stdout or ""

    def strays(self, root: Path) -> set[str]:
        """What ``root`` shows inside beyond what the wrapper mounted there."""
        # Mount destinations only -- an environment value such as a PATH under HOME is
        # not a mount. Paths, not strings: HOME=/home/user/ is the same directory.
        args = list(enumerate(self.wrap))
        dests = [Path(self.wrap[i + 1]) for i, a in args if a == "--tmpfs"]
        dests += [Path(self.wrap[i + 2]) for i, a in args if a.endswith("-bind")]
        mounted = {
            d.relative_to(root).parts[0]
            for d in dests
            if d.is_relative_to(root) and d != root
        }
        # One name per line, so a name with a space stays one name.
        listed = self.sh(f"ls -A1 {shlex.quote(str(root))}")
        return {n for n in listed.split("\n") if n} - mounted


# Each check returns why the wrapper fails it, or None. In order: the codex check's
# three -- it runs, it reads, it refuses a write outside the workspace -- then one per
# thing the wrapper claims. A canary that survives because the sandbox never launched
# is not a pass, which is why it runs first.


def _check_runs(box: _Box) -> str | None:
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


def _check_agy_starts(box: _Box) -> str | None:
    # A wrapper that hides agy's runtime passes every check below and leaves a slot
    # that never answers.
    if "AGY_OK" in box.sh("agy --version >/dev/null 2>&1 && echo AGY_OK"):
        return None
    return "agy does not start inside its sandbox"


def _check_files(box: _Box, canary: Path) -> str | None:
    # A reviewer that cannot read the system cannot run.
    if "original" not in box.sh(f"cat {shlex.quote(str(canary))}"):
        return "bwrap sandbox could not read outside the workspace"
    # The one that matters: is a write outside the workspace REFUSED?
    box.sh(f"echo changed > {shlex.quote(str(canary))}")
    if not canary.is_file() or canary.read_bytes() != b"original":
        return "bwrap sandbox ALLOWED a write outside the workspace"
    # And inside it, allowed? agy keeps its working files there, and fails without
    # them looking like a reviewer with nothing to say.
    box.sh(f"echo ok > {shlex.quote(str(box.workspace / 'probe'))}")
    if not (box.workspace / "probe").is_file():
        return "bwrap sandbox refused a write inside agy's workspace"
    return None


def _check_hidden(box: _Box) -> str | None:
    for root in (*_homes(), Path("/tmp"), Path("/run")):
        extra = box.strays(root)
        if extra:
            return f"bwrap sandbox shows {root}/{sorted(extra)[0]}"
    return None


def _check_processes(box: _Box) -> str | None:
    # A namespace, compared as one: a host PID can exist inside too (the loop as PID 1
    # in a container), so looking one up proves nothing.
    inner = box.sh("readlink /proc/self/ns/pid").strip()
    if not inner or inner == os.readlink("/proc/self/ns/pid"):
        return "bwrap sandbox shares the host's processes"
    return None


def _check_environment(box: _Box) -> str | None:
    # Every process's environment, not only the child's: bwrap is PID 1 inside, and
    # its /proc/1/environ is as readable as the child's own variables.
    leak = box.sh(
        'printf %s "$MMCRA_ENV_PROBE"; cat /proc/[0-9]*/environ 2>/dev/null'
        " | tr '\\0' '\\n' | grep -q '^MMCRA_ENV_PROBE=' && echo LEAK",
        env={**os.environ, "MMCRA_ENV_PROBE": "x"},
    )
    return "bwrap sandbox passes the host environment through" if leak.strip() else None


def _check_sockets(box: _Box) -> str | None:
    paths = " ".join(
        shlex.quote(s) for s in (f"/run/user/{os.getuid()}/bus", *_HOST_SOCKETS)
    )
    reached = box.sh(f'for s in {paths}; do [ -S "$s" ] && echo "$s"; done').split()
    if reached:
        return f"bwrap sandbox can reach {reached[0]}"
    # Abstract sockets belong to the network namespace, which agy keeps to reach its
    # model, so no mount hides them. A dbus-launch session bus starts any program it is
    # asked to: refused. (An X server is named by ``_x_server_note``, not refused.)
    if " @/tmp/dbus-" in box.sh("cat /proc/net/unix"):
        return "bwrap sandbox can reach a session bus (@/tmp/dbus-*)"
    return None


def _x_server_note(box: _Box) -> str:
    # Reachable only if its access control lets this user in without the cookie, which
    # the hidden home holds: named rather than refused, or no desktop could run agy.
    if " @/tmp/.X11-unix/" in box.sh("cat /proc/net/unix"):
        return "; an X server is reachable (@/tmp/.X11-unix), see the README"
    return ""


def agy_is_confined() -> tuple[bool, str]:
    """Verify, with no model call, the wrapper a reviewer runs agy under.

    It runs, agy starts in it, it reads outside the workspace but writes only inside,
    both homes, /tmp and /run hold only what was put there, the process namespace is
    not the host's, the environment is cleared, and no session bus or named host
    socket is reachable, by path or abstract name.
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
            checks: tuple[Callable[[_Box], str | None], ...] = (
                _check_runs,
                _check_agy_starts,
                functools.partial(_check_files, canary=canary),
                _check_hidden,
                _check_processes,
                _check_environment,
                _check_sockets,
            )
            for check in checks:
                failure = check(box)
                if failure:
                    return False, failure
            note = _x_server_note(box)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"agy sandbox check failed: {exc}"
    verified = "agy sandbox verified (read-only; home, /tmp, /run hidden; no env)"
    return True, verified + note


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
