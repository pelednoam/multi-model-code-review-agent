"""Extract reviewer JSON output from raw subprocess logs."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from .agy import agy_result, ran_agy
from .findings import validate_result

if TYPE_CHECKING:
    from pathlib import Path

EXPECTED_REVIEWERS = {
    1: "security",
    2: "correctness",
    3: "readability_performance",
    4: "spec_contract",
}


def _try_load_existing_result(result_path: Path, slot: int) -> dict[str, Any] | None:
    """Return a valid result dict from a pre-written result-N.json, else None."""
    if not result_path.exists():
        return None
    try:
        d = json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None
    return validate_result(d, slot, EXPECTED_REVIEWERS)


#: Terminal control characters, which a reply decoded from JSON can carry raw: an
#: escape sequence can rewrite or hide the lines above it, retitle the window, or on
#: some terminals write the clipboard. A reviewer's text is shaped by the diff it read.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """``text`` with its control characters made visible as ``?``, safe to print."""
    return _CONTROL.sub("?", text)


def agy_failure(result: dict[str, Any] | None) -> str | None:
    """Why agy's ``result`` event (``agy_result``) holds no review, or None if it does.

    Only a finished turn is a review. ERROR, CANCELED, INTERRUPTED and WAITING can all
    carry review-shaped output -- a quota can run out after a whole review is drafted
    -- and so can a stream with no result event at all.
    """
    if result is None:
        return "no result event"
    if result.get("status") != "SUCCESS":
        error = str(result.get("error") or "no error given").strip()
        return printable(f"status {result.get('status')}: {error}")
    return None


def agy_answer(result: dict[str, Any] | None) -> str:
    """The review in agy's ``result`` event (``agy_result``), or "" if it gave none.

    "" for anything short of a finished turn (``agy_failure``): read as plain text, a
    bare object in a stream with no result event passed as agy's verdict. Nothing in
    the schema-checked ``structured_output`` is unwrapped further: a ``result`` key the
    diff talked agy into adding does not replace it.
    """
    if result is None or agy_failure(result):
        return ""
    if isinstance(result.get("structured_output"), dict):
        return json.dumps(result["structured_output"])
    return str(result.get("response") or "")


def _unwrap_envelope(text: str, agy_ran: bool = False) -> str:
    """Take the review out of the envelope its CLI wrapped it in.

    ``claude -p --output-format json`` wraps it in ``{"result": "<text>"}``. agy's
    stream-json -- read as such only when ``agy_ran`` -- ends in a ``result`` event
    whose ``structured_output`` is the schema-checked object, with ``response`` --
    free text -- as the fallback; anything short of a finished turn is "". Anything
    else is returned as-is.
    """
    if agy_ran:
        return agy_answer(agy_result(text))
    try:
        wrapper = json.loads(text)
    except ValueError:
        return text
    if isinstance(wrapper, dict) and isinstance(wrapper.get("result"), str):
        result: str = wrapper["result"]
        return result
    return text


def parse_review(text: str) -> dict[str, Any] | None:
    """The JSON object in ``text`` -- from its first ``{`` to its last ``}`` -- or None.

    Unwrapped text still carries what the model put around its answer: a sentence
    before it, a markdown fence. Shared with the full-mode runner (``runner_cli``).
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        review = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return review if isinstance(review, dict) else None


def _extract_from_raw(round_dir: Path, slot: int) -> dict[str, Any] | None:
    """Parse raw-{slot}.txt as JSON, writing result-{slot}.json on success."""
    raw_path = round_dir / f"raw-{slot}.txt"
    if not raw_path.exists():
        return None
    text = _unwrap_envelope(raw_path.read_text(), ran_agy(round_dir, slot))
    d = parse_review(text)
    if d is None:
        return None
    d = validate_result(d, slot, EXPECTED_REVIEWERS)
    if d is None:
        return None
    with open(round_dir / f"result-{slot}.json", "w") as f:
        json.dump(d, f, indent=2)
    return d


def extract_results(round_dir: Path) -> list[dict[str, Any] | None]:
    """Extract and validate result JSON from each reviewer's output."""
    results: list[dict[str, Any] | None] = []
    for i in range(1, 5):
        result_path = round_dir / f"result-{i}.json"
        d = _try_load_existing_result(result_path, i) or _extract_from_raw(round_dir, i)
        results.append(d)
    return results


def missing_reviewers(results: list[dict[str, Any] | None]) -> list[int]:
    """The slots (1-based, as in ``result-N.json``) whose reviewer produced nothing.

    A reviewer that failed -- a revoked API key, a network drop, a timeout --
    leaves a hole in the round, not a clean bill of health. Its findings are
    unknown, so "no blocking findings" among the others is not convergence.
    """
    return [slot for slot, one in enumerate(results, start=1) if one is None]

