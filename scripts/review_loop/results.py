"""Extract reviewer JSON output from raw subprocess logs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

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
        d = json.loads(result_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return validate_result(d, slot, EXPECTED_REVIEWERS)


def _unwrap_claude_result(text: str) -> str:
    """Claude -p --output-format json wraps the result in a {result: str} envelope.

    Unwrap if needed, otherwise return the text as-is.
    """
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
    text = _unwrap_claude_result(raw_path.read_text())
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
