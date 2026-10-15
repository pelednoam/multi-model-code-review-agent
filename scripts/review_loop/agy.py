"""agy, the Antigravity CLI, as slot 3: its model, its command, what it is sent.

agy is the second reviewer whose read-only-ness has to be shown rather than assumed,
and nothing agy offers can show it. Measured on agy 1.3.4: under --mode=accept-edits it
wrote a file OUTSIDE its workspace, by absolute path, with no denial recorded -- so the
workspace is not a boundary. Under --mode=plan it never wrote, in five tries, even when
told the write was pre-approved; but nothing was ever denied either, so that is the
model declining, not agy refusing. And there is no deny list like claude's
--disallowedTools; --sandbox restricts terminal commands only.

So the boundary is the operating system's: agy runs under bubblewrap, built and proven
in ``agy_sandbox`` and ``agy_sandbox_check``. Its stream is read in ``results``.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .agy_sandbox import agy_seed_state, agy_wrapper
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
            contextlib.ExitStack() as scratch,
        ):
            workspace = Path(scratch.enter_context(tempfile.TemporaryDirectory()))
            workspace = workspace.resolve()
            cmd = ["agy", "models"]
            if not agy_unsandboxed():
                # A state directory only for the sandbox, which mounts it over agy's.
                st = Path(scratch.enter_context(tempfile.TemporaryDirectory()))
                cmd = [*agy_wrapper(workspace, agy_seed_state(st.resolve())), *cmd]
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
    # 4 and 4.0 are one version: as written, 4.0-flash-low outranked 4-pro-high.
    while len(version) > 1 and version[-1] == 0:
        version = version[:-1]
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
        _check_schema_shape(schema, path)
    else:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValueError(f"{path} is not a valid schema: {exc.message}") from exc
    return json.dumps(schema)


def _check_schema_shape(schema: object, path: Path) -> None:
    """The structure a result schema needs, where jsonschema is not there to check
    all of it: an object schema whose properties are schemas, requiring only keys it
    has. A type check alone let a schema with broken parts through, for agy to
    reject only after launch."""

    def fail(why: str) -> ValueError:
        return ValueError(f"{path} is not a usable result schema: {why}")

    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise fail("not an object schema")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not all(
        isinstance(value, dict) for value in properties.values()
    ):
        raise fail("its properties are not a mapping of schemas")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(key, str) and key in properties for key in required
    ):
        raise fail("it requires keys it does not define")


def agy_message(prompt: str) -> str:
    """One prompt, framed the way ``--input-format stream-json`` reads it."""
    return json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
