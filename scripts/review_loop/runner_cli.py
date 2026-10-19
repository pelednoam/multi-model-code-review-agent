"""The Python the full-mode runner in docs/ensemble-review.md calls, in one place.

The runner is bash, and what it needs from this package -- whether codex and agy are
safe to use, agy's sandbox and what agy is sent, the review taken out of a reviewer's
output -- is the convergence loop's own code, called through here rather than copied
into one-liners that drift from it. It runs as
``python3 -I scripts/review_loop/runner_entry.py <command> [args...]``.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .agy import (
    AGY_PREAMBLE,
    agy_message,
    agy_model_choice,
    agy_schema,
    agy_unsandboxed,
)
from .agy_sandbox import agy_seed_state, agy_wrapper_parts
from .agy_sandbox_check import agy_reachable
from .backends import codex_mcp_off, codex_usable, detect_backends
from .claude_tools import claude_denied, claude_disallowed, claude_tool_names
from .config import env_flag
from .findings import validate_result
from .results import (
    EXPECTED_REVIEWERS,
    agy_answer,
    agy_failure,
    agy_result,
    encode_review,
    parse_review,
    unwrap_envelope,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _codex_check() -> int:
    """Exit 0 if codex may review (``backends.codex_usable``)."""
    ok, why = codex_usable()
    print(f"  codex: {'' if ok else 'DISABLED for review -- '}{why}", file=sys.stderr)
    return 0 if ok else 1


def _agy_detect(prefer_hermes: str = "true") -> int:
    """Print the model agy will run, or nothing if agy is not to be used.

    ``prefer_hermes`` is the runner's PREFER_HERMES. Anything but exactly ``false``
    -- empty included -- reads as pinned to Bedrock, the safe misreading, and agy is
    then not even detected unless asked for by name (REVIEW_USE_AGY=1).
    """
    with contextlib.redirect_stdout(sys.stderr):
        # Pinned unless plainly "false": a value read the wrong way must not send a
        # Bedrock project's code to Google.
        found = detect_backends(
            verify_codex=False, prefer_hermes=prefer_hermes != "false"
        )
        # Picked once per run (cached): detection already chose it.
        model = agy_model_choice()[0] if found["agy"] else None
    print(model or "")
    return 0


def _agy_schema(path: str) -> int:
    """Print the result schema as one line, or fail if it is missing or not a schema."""
    try:
        print(agy_schema(Path(path)))
    except (OSError, ValueError) as exc:
        print(f"agy result schema unusable: {exc}", file=sys.stderr)
        return 1
    return 0


def _agy_sandbox(workspace: str, state: str, review_root: str, *hide: str) -> int:
    """NUL-separated: the length of the env part, the env part, bwrap's part.

    ``review_root``, the repository under review, is hidden inside, as the loop hides
    it, and ``hide`` -- the runner's private directories, wherever TMPDIR put them.
    ``0`` and nothing else when the host is trusted to be the sandbox itself.
    """
    if agy_unsandboxed():
        sys.stdout.write("0\0")
        return 0
    env, bwrap = agy_wrapper_parts(
        Path(workspace),
        agy_seed_state(Path(state)),
        (Path(review_root), *(Path(path) for path in hide)),
    )
    sys.stdout.write("\0".join([str(len(env)), *env, *bwrap]) + "\0")
    return 0


def _agy_reachable(workspace: str) -> int:
    """Exit 0 if agy's sandbox reaches nothing it must not, right now; else say what.

    The startup check's last part, again before the launch
    (``agy_sandbox_check.agy_reachable``). The wrapper is the one agy will run under,
    NUL-separated on stdin, as the runner holds it -- not one built again here, which
    could differ from it.
    """
    if agy_unsandboxed():
        return 0
    # Bytes, as the shell wrote them: decoded strictly, a path that is not UTF-8 failed;
    # and empty arguments kept, so that what is checked is what runs. Only the final
    # terminator goes.
    wrap = [os.fsdecode(arg) for arg in sys.stdin.buffer.read().split(b"\0")]
    if wrap and wrap[-1] == "":
        wrap.pop()
    if not wrap:
        print("no sandbox to check")
        return 1
    failure = agy_reachable(wrap, Path(workspace))
    if failure:
        print(failure)
        return 1
    return 0


def _agy_input(prompt_file: str, message_file: str) -> int:
    """Write agy's one stream-json input line, then record it in the prompt file.

    The prompt with agy's no-tools preamble goes to ``message_file``, and only then is
    the preamble written into ``prompt_file``, the record of what agy was sent: if the
    first fails, the prompt is untouched for the backend the slot falls through to.
    And the second replaces the file whole, from a copy written beside it: written in
    place, a failure halfway left a truncated prompt for that backend.
    """
    path = Path(prompt_file)
    prompt = AGY_PREAMBLE + path.read_text(encoding="utf-8")
    Path(message_file).write_text(agy_message(prompt), encoding="utf-8")
    fd, copy = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as new:
            new.write(prompt)
        os.replace(copy, path)
    finally:
        Path(copy).unlink(missing_ok=True)
    return 0


def _extract(raw: str, out: str, slot: str, backend: str = "", model: str = "") -> int:
    """Write the review in ``raw`` to ``out``, unwrapped and checked as the loop does it.

    agy's stream is read as such only when agy ran the slot, and only a finished turn
    counts. Then the loop's own ``validate_result``: the required keys, a list of
    findings that are objects, and the reviewer named by the slot, not by the model.

    One difference from the loop: ``model``, the model the runner launched (hermes's
    as the loop labels it, claude's alias, CODEX_MODEL, agy's or gemini's), replaces
    the one the review names -- the runner's prompts were written for the lens's own
    model, which a fallback is not. Empty -- codex on its default -- the review keeps
    what the reviewer reported, as the loop's always does.
    """
    text = Path(raw).read_text(encoding="utf-8", errors="replace")
    if backend == "agy":
        result = agy_result(text)
        failed = agy_failure(result)
        if failed:
            print(f"agy gave no review ({failed[:600]})", file=sys.stderr)
            return 1
        text = agy_answer(result)
    else:
        text = unwrap_envelope(text, backend)
    review = validate_result(parse_review(text), int(slot), EXPECTED_REVIEWERS)
    if review is None:
        print("no review in the output: no JSON object with the required keys and a"
              " list of findings", file=sys.stderr)  # fmt: skip
        return 1
    if model:
        # The model that ran, as the runner launched it: the prompt names the lens's
        # own, which a fallback backend is not.
        review["model"] = model
    encoded = encode_review(review)
    if encoded is None:
        print("the review is nested too deep to write", file=sys.stderr)
        return 1
    Path(out).write_text(encoded, encoding="utf-8")
    return 0


def _claude_deny(tools: str, repo: str = "") -> int:
    """Print what a claude reviewer allowed ``tools`` is denied, space-separated.

    ``claude_tools.claude_disallowed``: the tools it is not granted, and reads of the
    credential paths no grant lifts. A tool granted with a scope is granted whole, as
    far as the deny list goes, which stderr says.
    """
    _, scoped = claude_denied(tools)
    for tool in scoped:
        print(
            f"{tool} is granted whole: its scope holds only while claude asks before"
            " each use, which settings can turn off",
            file=sys.stderr,
        )
    print(" ".join(claude_disallowed(tools, Path(repo) if repo else None)))
    return 0


def _codex_mcp_off(cwd: str = ".") -> int:
    """Print the ``-c`` arguments that turn off codex's MCP servers, one per line
    (``backends.codex_mcp_off``); exit 1, and codex is not to run, when they cannot
    be listed."""
    off = codex_mcp_off(Path(cwd))
    if off is None:
        print("codex's MCP servers could not be listed", file=sys.stderr)
        return 1
    print("\n".join(off))
    return 0


def _audit_requote(raw: str, repo: str) -> int:
    """Re-read each line the raw audit ``raw`` quotes from its source in ``repo``, and
    redact those that hold a credential (``diff.requote_from_source``) -- as the loop
    does before it scrubs the audit: an older installed preflight cuts its quotes."""
    from .diff import requote_from_source

    requote_from_source(Path(raw), Path(repo))
    return 0


def _claude_tools(tools: str) -> int:
    """Print the --tools value for a claude reviewer allowed ``tools``: the names it
    grants, comma-separated (``claude_tools.claude_tool_names``)."""
    print(claude_tool_names(tools))
    return 0


def _flag(name: str) -> int:
    """Exit 0 if the environment variable ``name`` says yes (``config.env_flag``)."""
    return 0 if env_flag(name) else 1


def _placeholder(out: str, slot: str, why: str) -> int:
    """Write a result for a slot that produced none, saying why and where to look."""
    review = {
        "reviewer": "unknown",
        "model": "unknown",
        "findings": [],
        "overall_assessment": (
            f"Reviewer {slot} failed: {why}."
            # A slot that never ran has neither file to look in.
            + (
                ""
                if why.startswith("never launched")
                else f" Check extract-result-{slot}.err and stderr-{slot}.txt."
            )
        ),
    }
    Path(out).write_text(json.dumps(review, indent=2), encoding="utf-8")
    return 0


_COMMANDS: dict[str, Callable[..., int]] = {
    "codex-check": _codex_check,
    "agy-detect": _agy_detect,
    "agy-schema": _agy_schema,
    "agy-sandbox": _agy_sandbox,
    "agy-reachable": _agy_reachable,
    "agy-input": _agy_input,
    "extract": _extract,
    "placeholder": _placeholder,
    "claude-deny": _claude_deny,
    "claude-tools": _claude_tools,
    "audit-requote": _audit_requote,
    "codex-mcp-off": _codex_mcp_off,
    "flag": _flag,
}


def _usage(name: str) -> str:
    """``name`` and its arguments in order, optional ones in brackets: bash passes
    them by position, so the order is the interface."""
    params = inspect.signature(_COMMANDS[name]).parameters.values()
    shown = [
        f"[{p.name.upper()}...]"
        if p.kind is inspect.Parameter.VAR_POSITIONAL
        else p.name.upper()
        if p.default is inspect.Parameter.empty
        else f"[{p.name.upper()}]"
        for p in params
    ]
    return " ".join([name, *shown])


def main(argv: list[str]) -> int:
    """Run the command ``argv`` names; its exit status, or 2 for a usage error."""
    command = _COMMANDS.get(argv[0]) if argv else None
    if command is None:
        print("usage: runner_cli COMMAND [ARGS...], one of:", file=sys.stderr)
        for name in _COMMANDS:
            print(f"  {_usage(name)}", file=sys.stderr)
        return 2
    try:
        inspect.signature(command).bind(*argv[1:])
    except TypeError as exc:
        print(f"runner_cli {argv[0]}: {exc}; usage: {_usage(argv[0])}", file=sys.stderr)
        return 2
    return command(*argv[1:])
