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
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .agy import (
    AGY_PREAMBLE,
    agy_message,
    agy_model,
    agy_reachable,
    agy_result,
    agy_sandbox,
    agy_sandbox_parts,
    agy_schema,
    agy_seed_state,
    agy_unsandboxed,
)
from .backends import codex_is_confined, detect_backends
from .config import env_flag
from .findings import validate_result
from .results import (
    EXPECTED_REVIEWERS,
    _unwrap_envelope,
    agy_answer,
    agy_failure,
    parse_review,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _codex_check() -> int:
    """Exit 0 if codex may review: its sandbox refuses a write, or the check is skipped."""
    if env_flag("MMCRA_SKIP_CODEX_SANDBOX_CHECK"):
        print(
            "  codex: sandbox check skipped (MMCRA_SKIP_CODEX_SANDBOX_CHECK)",
            file=sys.stderr,
        )
        return 0
    ok, why = codex_is_confined()
    print(f"  codex: {'' if ok else 'DISABLED for review -- '}{why}", file=sys.stderr)
    return 0 if ok else 1


def _agy_detect(prefer_hermes: str = "false") -> int:
    """Print the model agy will run, or nothing if agy is not to be used.

    ``prefer_hermes`` is the runner's PREFER_HERMES, ``true`` for a project pinned to
    Bedrock: agy is then not even detected unless asked for by name.
    """
    with contextlib.redirect_stdout(sys.stderr):
        found = detect_backends(
            verify_codex=False, prefer_hermes=prefer_hermes == "true"
        )
        ok = found["agy"]
        model = agy_model() if ok else None
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


def _agy_sandbox(workspace: str, state: str) -> int:
    """NUL-separated: the length of the env part, the env part, bwrap's part.

    ``0`` and nothing else when the host is trusted to be the sandbox itself.
    """
    if agy_unsandboxed():
        sys.stdout.write("0\0")
        return 0
    env, bwrap = agy_sandbox_parts(Path(workspace), agy_seed_state(Path(state)))
    sys.stdout.write("\0".join([str(len(env)), *env, *bwrap]) + "\0")
    return 0


def _agy_reachable(workspace: str, state: str) -> int:
    """Exit 0 if agy's sandbox reaches nothing it must not, right now; else say what.

    The startup check's last part, again before the launch (``agy.agy_reachable``).
    """
    if agy_unsandboxed():
        return 0
    failure = agy_reachable(agy_sandbox(Path(workspace), Path(state)), Path(workspace))
    if failure:
        print(failure)
        return 1
    return 0


def _agy_preamble(prompt_file: str) -> int:
    """Prepend agy's no-tools preamble to the prompt file, which records what it is sent."""
    path = Path(prompt_file)
    path.write_text(AGY_PREAMBLE + path.read_text(encoding="utf-8"), encoding="utf-8")
    return 0


def _agy_message(prompt_file: str) -> int:
    """Print the prompt file framed as agy's one stream-json input line."""
    sys.stdout.write(agy_message(Path(prompt_file).read_text(encoding="utf-8")))
    return 0


def _extract(raw: str, out: str, slot: str, backend: str = "") -> int:
    """Write the review in ``raw`` to ``out``, unwrapped and checked as the loop does it.

    agy's stream is read as such only when agy ran the slot, and only a finished turn
    counts. Then the loop's own ``validate_result``: the required keys, a list of
    findings that are objects, and the reviewer named by the slot, not by the model.
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
        text = _unwrap_envelope(text)
    review = validate_result(parse_review(text), int(slot), EXPECTED_REVIEWERS)
    if review is None:
        print("no review in the output: no JSON object with the required keys and a"
              " list of findings", file=sys.stderr)  # fmt: skip
        return 1
    Path(out).write_text(json.dumps(review, indent=2), encoding="utf-8")
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
            f"Reviewer {slot} failed: {why}. Check extract-result-{slot}.err"
            f" and stderr-{slot}.txt."
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
    "agy-preamble": _agy_preamble,
    "agy-message": _agy_message,
    "extract": _extract,
    "placeholder": _placeholder,
    "flag": _flag,
}


def main(argv: list[str]) -> int:
    """Run the command ``argv`` names; its exit status, or 2 for a usage error."""
    command = _COMMANDS.get(argv[0]) if argv else None
    if command is None:
        print(f"usage: runner_cli {{{','.join(_COMMANDS)}}} [args...]", file=sys.stderr)
        return 2
    try:
        inspect.signature(command).bind(*argv[1:])
    except TypeError as exc:
        print(f"runner_cli {argv[0]}: {exc}", file=sys.stderr)
        return 2
    return command(*argv[1:])
