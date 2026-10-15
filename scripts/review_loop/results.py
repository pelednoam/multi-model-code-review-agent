"""Extract reviewer JSON output from raw subprocess logs."""

from __future__ import annotations

import json
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


def _unwrap_envelope(text: str, agy_ran: bool = False) -> str:
    """Take the review out of the envelope its CLI wrapped it in.

    ``claude -p --output-format json`` wraps it in ``{"result": "<text>"}``. agy's
    stream-json -- read as such only when ``agy_ran`` -- ends in a ``result`` event
    whose ``structured_output`` is the schema-checked object, with ``response`` --
    free text -- as the fallback. Anything else is returned as-is.
    """
    agy = agy_result(text) if agy_ran else None
    if agy is not None:
        # ERROR, CANCELED, INTERRUPTED and WAITING can all carry review-shaped output.
        # A turn that did not finish is not a review, however finished it looks.
        if agy.get("status") != "SUCCESS":
            return ""
        if isinstance(agy.get("structured_output"), dict):
            return json.dumps(agy["structured_output"])
        return str(agy.get("response") or "")
    try:
        wrapper = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if isinstance(wrapper, dict) and isinstance(wrapper.get("result"), str):
        result: str = wrapper["result"]
        return result
    return text


def _extract_from_raw(round_dir: Path, slot: int) -> dict[str, Any] | None:
    """Parse raw-{slot}.txt as JSON, writing result-{slot}.json on success."""
    raw_path = round_dir / f"raw-{slot}.txt"
    if not raw_path.exists():
        return None
    text = _unwrap_envelope(raw_path.read_text(), ran_agy(round_dir, slot))
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        d = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
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

