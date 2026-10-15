"""Make a round's output files discoverable.

The round directory holds four files per reviewer and it is not obvious which
one carries the findings. In particular `raw-N.txt` is *empty on a successful
run* for the codex slot, because that backend is invoked with `-o` and writes
its answer to `codex-out-N.txt`. A reader who checks `raw-N.txt` — the
obvious-looking name — can conclude a reviewer failed when it succeeded.

Writing this key into the round directory, and pointing at it from the console
summary, means anyone picking the run apart afterwards (a human, or another
Claude Code session) finds the findings without having to read the source.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING, Any

from .results import agy_denied, agy_failure, agy_result, printable

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

ARTIFACTS_FILENAME = "ARTIFACTS.md"

_ARTIFACTS_DOC = """# Where this round's output lives

Read `result-*.json`. Everything else is intermediate.

| File | What it is |
|---|---|
| `result-N.json` | **The findings.** One object per reviewer, checked for the schema's required top-level keys and a list of finding objects (agy's is also held to `docs/ensemble_review_result_schema.json` by `--json-schema`; `scripts/validate_review_results.py` checks the full schema). This is what you want. |
| `raw-N.txt` | Whatever the CLI printed on stdout, before parsing. Empty for the codex slot on a *successful* run — see below. For agy, one stream-json event per line; the review is the final `result` event's `structured_output`, and only a turn whose status is `SUCCESS` counts. |
| `codex-out-N.txt` | The codex slot's answer, its last message (`-o`), read before its `raw-N.txt`. |
| `stderr-N.txt` | Diagnostics. Read this when a slot is missing a `result-N.json`. |
| `prompt-N.txt` | The exact prompt that reviewer received. For agy it begins with a preamble telling it to call no tools. |
| `backend-N.txt` | Which CLI ran slot N (`agy`, `claude`, `codex`, ...), for reading. The loop itself goes by what it launched, not by this file: only the slot it ran agy in has its `raw-N.txt` read as agy's stream-json, and the same shape from any other reviewer is ignored. |
| `diff.patch` | The scrubbed diff every reviewer saw. |
| `audit.json` | Preflight audit (git state, coverage, patterns) included in each prompt. |

## Two things that look like failures and are not

**An empty `raw-N.txt`.** The codex slot runs with `-o` and writes its answer
to `codex-out-N.txt`, so its raw file is empty every time it succeeds. Judge a
slot by whether `result-N.json` exists, never by the size of its raw file: the
loop writes it only from a validated answer, and removes any it did not write.

**A file that is not there yet.** Reviewers write to a private directory while
they run; their `raw-N.txt`, `stderr-N.txt` and `codex-out-N.txt` are copied here
only once every reviewer has stopped, so that none can write another's. Wait for
`Reviewers finished in Ns` before drawing conclusions.

## Reading the findings

```bash
# every finding, most severe first
jq -s '[.[].findings[]] | sort_by(.severity)' result-*.json

# just the blocking ones
jq -s '[.[].findings[] | select(.blocking or .severity == "critical")]' result-*.json

# which reviewers reported at all
for f in result-*.json; do echo "$f: $(jq '.findings | length' "$f") findings"; done
```
"""


def write_artifacts_key(round_dir: Path) -> Path:
    """Drop a key to the round's files alongside them. Returns its path."""
    path = round_dir / ARTIFACTS_FILENAME
    path.write_text(_ARTIFACTS_DOC)
    return path


def describe_outputs(
    round_dir: Path, answered_by: Mapping[int, str], n_slots: int = 4
) -> str:
    """A console summary naming where the findings actually are.

    ``answered_by`` is the CLI that ran each slot (``launch_reviewers``).
    """
    present = [
        i for i in range(1, n_slots + 1) if (round_dir / f"result-{i}.json").exists()
    ]
    missing = [i for i in range(1, n_slots + 1) if i not in present]
    lines = [
        "",
        f"Output: {round_dir}",
        f"  findings   -> result-{{{','.join(str(i) for i in present)}}}.json"
        if present
        else "  findings   -> (none produced)",
    ]
    for slot in missing:
        lines.extend(_explain_missing(round_dir, slot, answered_by.get(slot)))
    lines.append(f"  file guide -> {round_dir / ARTIFACTS_FILENAME}")
    return "\n".join(lines)


#: How much of an unparseable reply to put in front of the operator. Enough to
#: see whether it was a real review; the whole thing is in raw-N.txt.
_SALVAGE_CHARS = 600


def _explain_missing(round_dir: Path, slot: int, backend: str | None) -> list[str]:
    """Say why a slot produced no findings, and show what it did produce.

    "NO RESULT" covered two different things and read as one. A reviewer that
    crashed produced nothing; a reviewer that answered in prose instead of the
    schema produced a *review* that the parser could not use -- and one of those
    was silently binned while carrying a correct finding about duplicated tests.
    Whatever it wrote goes in front of the operator either way.
    """
    raw, text = _read_answer(round_dir, slot, backend)
    # Parsed once, and only when agy is the backend that wrote it.
    event = agy_result(text) if text and backend == "agy" else None
    failed = agy_failure(event) if event is not None else None
    if failed:
        # Whatever it drafted first is not a review it gave: the turn did not finish.
        return [
            f"  FAILED     -> R{slot} (agy) did not finish its turn:",
            *[f"                {line}" for line in textwrap.wrap(failed, 74)],
            f"                (in full: {raw})",
        ]
    shown = printable(_display_text(text, event))
    if not shown:
        return [f"  NO OUTPUT  -> R{slot} produced nothing; see stderr-{slot}.txt"]
    return [
        f"  NOT JSON   -> R{slot} answered in prose, so it has no findings the loop",
        "                can act on. It still said something -- read it:",
        *[f"                {line}" for line in textwrap.wrap(shown, 74)],
        f"                (in full: {raw})",
    ]


def _read_answer(round_dir: Path, slot: int, backend: str | None) -> tuple[Path, str]:
    """The file holding the slot's answer, and what it says (empty if nothing).

    codex's answer is its -o file, read first as extraction reads it: an answer in
    prose there read as "NO OUTPUT" from its empty stdout.
    """
    names = (["codex-out-{}.txt"] if backend == "codex" else []) + ["raw-{}.txt"]
    for name in names:
        path = round_dir / name.format(slot)
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                return path, content
    return round_dir / f"raw-{slot}.txt", ""


def _display_text(text: str, event: dict[str, Any] | None) -> str:
    """What to show of a reviewer's unusable reply: ``text``, unwrapped as far as it can
    be, and trimmed. ``event`` is agy's result event, if agy wrote it.

    For showing, not parsing (that is ``results.unwrap_envelope``): it keeps what a
    failed turn says, and a refused tool call, where parsing keeps nothing.
    """
    if event is not None:
        # A failed turn has no response but says why, which is the useful part. So
        # does one cut short by a refused tool call, which otherwise reads as silence.
        said = event.get("response") or event.get("error") or ""
        denied = agy_denied(event)
        if not said and denied:
            said = (
                f"agy ended its turn with no answer after a refused tool call"
                f" ({', '.join(denied)})"
            )
        return str(said).strip()[:_SALVAGE_CHARS]
    try:
        wrapper = json.loads(text)
    except ValueError:
        return text[:_SALVAGE_CHARS]
    if isinstance(wrapper, dict) and isinstance(wrapper.get("result"), str):
        return str(wrapper["result"]).strip()[:_SALVAGE_CHARS]
    return text[:_SALVAGE_CHARS]
