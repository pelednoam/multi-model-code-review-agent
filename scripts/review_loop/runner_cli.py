"""The Python the full-mode runner in docs/ensemble-review.md calls, in one place.

The runner is bash, and what it needs from this package -- whether codex and agy are
safe to use, agy's sandbox and what agy is sent, the review taken out of a reviewer's
output -- is the convergence loop's own code, called through here rather than copied
into one-liners that drift from it. It runs as::

    python3 -I -c 'import sys, types; pkg = types.ModuleType("scripts");
        pkg.__path__ = [sys.argv.pop(1) + "/scripts"]; sys.modules["scripts"] = pkg;
        from scripts.review_loop.runner_cli import main; sys.exit(main(sys.argv[1:]))'
        "$root" <command> [args...]

``-I`` keeps PYTHONPATH, the user's site directory and the working directory out, and
only the ``scripts`` package comes from the checkout: the checkout's root is not put on
the path, so a ``json.py`` at its top cannot stand in for the standard library.
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
    agy_result,
    agy_sandbox_parts,
    agy_schema,
    agy_seed_state,
    agy_unsandboxed,
)
from .backends import codex_is_confined, detect_backends
from .config import RESULT_SCHEMA, env_flag
from .results import _unwrap_envelope, agy_answer, parse_review

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


def _agy_detect() -> int:
    """Print the model agy will run, or nothing if agy is not to be used."""
    with contextlib.redirect_stdout(sys.stderr):
        ok = detect_backends(verify_codex=False)["agy"]
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


def _agy_preamble(prompt_file: str) -> int:
    """Prepend agy's no-tools preamble to the prompt file, which records what it is sent."""
    path = Path(prompt_file)
    path.write_text(AGY_PREAMBLE + path.read_text(encoding="utf-8"), encoding="utf-8")
    return 0


def _agy_message(prompt_file: str) -> int:
    """Print the prompt file framed as agy's one stream-json input line."""
    sys.stdout.write(agy_message(Path(prompt_file).read_text(encoding="utf-8")))
    return 0


def _extract(raw: str, out: str, backend: str = "") -> int:
    """Write the review in ``raw`` to ``out``, unwrapped and parsed as the loop does it.

    agy's stream is read as such only when agy ran the slot, and only a finished turn
    counts (``results.agy_answer``). The review must carry the schema's required keys.
    """
    text = Path(raw).read_text(encoding="utf-8", errors="replace")
    if backend == "agy":
        result = agy_result(text)
        if result is None or result.get("status") != "SUCCESS":
            why = (
                "no result event"
                if result is None
                else (
                    f"turn status {result.get('status')}: {result.get('error') or ''}"
                )
            )
            print(f"agy gave no review ({why})", file=sys.stderr)
            return 1
        text = agy_answer(result)
    else:
        text = _unwrap_envelope(text)
    review = parse_review(text)
    if review is None:
        print("no JSON object in the output", file=sys.stderr)
        return 1
    required = json.loads(RESULT_SCHEMA.read_text(encoding="utf-8"))["required"]
    missing = sorted(set(required) - set(review))
    if missing:
        print(f"Missing keys: {missing}", file=sys.stderr)
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
