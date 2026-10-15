"""Extract reviewer JSON output from raw subprocess logs."""

from __future__ import annotations

import json
import unicodedata
from typing import TYPE_CHECKING, Any

from .findings import validate_result

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

EXPECTED_REVIEWERS = {
    1: "security",
    2: "correctness",
    3: "readability_performance",
    4: "spec_contract",
}


#: The Unicode categories of what does not print as itself. Control characters (Cc),
#: which a reply decoded from JSON can carry raw: an escape sequence can rewrite or hide
#: the lines above it, retitle the window, or on some terminals write the clipboard.
#: Format characters (Cf): bidi controls (the "Trojan Source" trick), zero-width ones,
#: the BOM, tag characters ("ASCII smuggling"). Line and paragraph separators (Zl,
#: Zp). Lone surrogates (Cs), which a JSON "\\ud800" decodes to and which no terminal
#: encoding can print: one raised UnicodeEncodeError and ended the loop. And private-use
#: characters (Co), which show as whatever a font makes of them. By category rather
#: than a list of ranges, which missed some every time it was reviewed. A reviewer's
#: text is shaped by the diff it read.
_INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})


def printable(text: str) -> str:
    """``text`` with what does not print as itself made visible as ``?``, safe to
    print. Tabs and newlines stay."""
    return "".join(
        "?" if c not in "\t\n" and unicodedata.category(c) in _INVISIBLE else c
        for c in text
    )


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


def agy_denied(result: dict[str, Any]) -> list[str]:
    """The tool calls agy's turn was refused, by name: one ends the turn unanswered."""
    actions = result.get("denied_actions")
    return [
        str(d.get("display_name") or d.get("action"))
        for d in (actions if isinstance(actions, list) else [])
        if isinstance(d, dict) and (d.get("display_name") or d.get("action"))
    ]


def agy_summary(result: dict[str, Any]) -> str:
    """What agy's turn said, for a person: its answer, or why there is none.

    A failed turn has no response but says why, which is the useful part. So does one
    cut short by a refused tool call, which otherwise reads as silence.
    """
    said = result.get("response") or result.get("error") or ""
    denied = agy_denied(result)
    if not said and denied:
        said = (
            f"agy ended its turn with no answer after a refused tool call"
            f" ({', '.join(denied)})"
        )
    return str(said).strip()


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


def unwrap_envelope(text: str, agy_ran: bool = False) -> str:
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


def _extract_from(
    output: Path, round_dir: Path, slot: int, backend: str
) -> dict[str, Any] | None:
    """Parse one of the slot's output files, writing result-{slot}.json on success."""
    try:
        # As UTF-8 whatever the locale: under LC_ALL=C one non-ASCII character in a
        # reviewer's answer raised, and took the whole round with it.
        raw = output.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = unwrap_envelope(raw, backend == "agy")
    d = parse_review(text)
    if d is None:
        return None
    d = validate_result(d, slot, EXPECTED_REVIEWERS)
    if d is None:
        return None
    (round_dir / f"result-{slot}.json").write_text(
        json.dumps(d, indent=2), encoding="utf-8"
    )
    return d


def extract_results(
    round_dir: Path, answered_by: Mapping[int, str]
) -> list[dict[str, Any] | None]:
    """Extract and validate result JSON from each reviewer's output.

    ``answered_by`` is the CLI that ran each slot (``launch_reviewers``): only agy's
    output is read as an agy stream. Any reviewer can be talked into printing a line
    shaped like agy's ``result`` event -- one with a failed status would silence a
    real review -- so the shape alone decides nothing.

    Each slot is read from its own CLI's output: codex's answer (-o,
    ``codex-out-N.txt``), then -- in this loop only; the full-mode runner reads the
    answer alone -- its stdout (``raw-N.txt``), as every other CLI's is read. A
    result-N.json is only ever written here, so one already present was put there by
    something else -- a reviewer with a file tool, hermes --yolo -- and is removed, as
    is one for a slot with no usable answer: the round's files list only real ones.
    """
    results: list[dict[str, Any] | None] = []
    for i in range(1, 5):
        (round_dir / f"result-{i}.json").unlink(missing_ok=True)
        backend = answered_by.get(i)
        found = None
        if backend == "codex":
            found = _extract_from(
                round_dir / f"codex-out-{i}.txt", round_dir, i, backend
            )
        # A slot never launched has no answer at all, whatever its files say.
        if found is None and backend is not None:
            found = _extract_from(round_dir / f"raw-{i}.txt", round_dir, i, backend)
        results.append(found)
    return results


def missing_reviewers(results: list[dict[str, Any] | None]) -> list[int]:
    """The slots (1-based, as in ``result-N.json``) whose reviewer produced nothing.

    A reviewer that failed -- a revoked API key, a network drop, a timeout --
    leaves a hole in the round, not a clean bill of health. Its findings are
    unknown, so "no blocking findings" among the others is not convergence.
    """
    return [slot for slot, one in enumerate(results, start=1) if one is None]

