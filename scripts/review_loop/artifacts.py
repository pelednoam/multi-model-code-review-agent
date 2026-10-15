"""Make a round's output files discoverable.

The round directory holds four files per reviewer and it is not obvious which
one carries the findings. In particular `raw-N.txt` is *empty on a successful
run* for the codex slot, because that backend is invoked with `-o result-N.json`
and writes straight to the result file. A reader who checks `raw-N.txt` — the
obvious-looking name — can conclude a reviewer failed when it succeeded.

Writing this key into the round directory, and pointing at it from the console
summary, means anyone picking the run apart afterwards (a human, or another
Claude Code session) finds the findings without having to read the source.
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import TYPE_CHECKING

from .agy import agy_result, ran_agy

if TYPE_CHECKING:
    from pathlib import Path

ARTIFACTS_FILENAME = "ARTIFACTS.md"

_ARTIFACTS_DOC = """# Where this round's output lives

Read `result-*.json`. Everything else is intermediate.

| File | What it is |
|---|---|
| `result-N.json` | **The findings.** One validated object per reviewer, matching `docs/ensemble_review_result_schema.json`. This is what you want. |
| `raw-N.txt` | Whatever the CLI printed on stdout, before parsing. Empty for the codex slot on a *successful* run — see below. For agy, one stream-json event per line; the review is the final `result` event's `structured_output`, and only a turn whose status is `SUCCESS` counts. |
| `stderr-N.txt` | Diagnostics. Read this when a slot is missing a `result-N.json`. |
| `prompt-N.txt` | The exact prompt that reviewer received. For agy it begins with a preamble telling it to call no tools. |
| `backend-N.txt` | Which CLI ran slot N (`agy`, `claude`, `codex`, ...). Only a slot marked `agy` has its `raw-N.txt` read as agy's stream-json; the same shape from any other reviewer is ignored. |
| `diff.patch` | The scrubbed diff every reviewer saw. |
| `audit.json` | Preflight audit (git state, coverage, patterns) included in each prompt. |

## Two things that look like failures and are not

**An empty `raw-N.txt`.** The codex slot runs with `-o result-N.json` and
writes its answer directly to the result file, so its raw file is empty every
time it succeeds. Judge a slot by whether `result-N.json` exists and validates,
never by the size of its raw file.

**A file that is empty right now.** Reviewers stream output as they work and a
round takes several minutes. A file inspected mid-run is not a finished file.
Wait for the process to exit before drawing conclusions — the console prints
`Reviewers finished in Ns` when they are all done.

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


def describe_outputs(round_dir: Path, n_slots: int = 4) -> str:
    """A console summary naming where the findings actually are."""
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
        lines.extend(_explain_missing(round_dir, slot))
    lines.append(f"  file guide -> {round_dir / ARTIFACTS_FILENAME}")
    return "\n".join(lines)


#: How much of an unparseable reply to put in front of the operator. Enough to
#: see whether it was a real review; the whole thing is in raw-N.txt.
_SALVAGE_CHARS = 600

#: Terminal control characters, which a reply decoded from JSON can carry raw: an
#: escape sequence can rewrite or hide the lines above it, retitle the window, or on
#: some terminals write the clipboard. A reviewer's text is shaped by the diff it read.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _printable(text: str) -> str:
    """``text`` with its control characters made visible as ``?``."""
    return _CONTROL.sub("?", text)


def _explain_missing(round_dir: Path, slot: int) -> list[str]:
    """Say why a slot produced no findings, and show what it did produce.

    "NO RESULT" covered two different things and read as one. A reviewer that
    crashed produced nothing; a reviewer that answered in prose instead of the
    schema produced a *review* that the parser could not use -- and one of those
    was silently binned while carrying a correct finding about duplicated tests.
    Whatever it wrote goes in front of the operator either way.
    """
    failed = _agy_failure(round_dir, slot)
    if failed:
        # Whatever it drafted first is not a review it gave: the turn did not finish.
        return [
            f"  FAILED     -> R{slot} (agy) did not finish its turn:",
            *[f"                {line}" for line in textwrap.wrap(failed, 74)],
            f"                (in full: {round_dir / f'raw-{slot}.txt'})",
        ]
    said = _salvage(round_dir, slot)
    if not said:
        return [f"  NO OUTPUT  -> R{slot} produced nothing; see stderr-{slot}.txt"]
    return [
        f"  NOT JSON   -> R{slot} answered in prose, so it has no findings the loop",
        "                can act on. It still said something -- read it:",
        *[f"                {line}" for line in textwrap.wrap(said, 74)],
        f"                (in full: {round_dir / f'raw-{slot}.txt'})",
    ]


def _agy_failure(round_dir: Path, slot: int) -> str:
    """Why agy's turn failed -- its status and error -- or "" if it did not fail.

    A turn can fail after the model has written a whole review, as when the plan's
    quota runs out mid-turn; shown as a reply in prose, the draft hid the reason.
    """
    raw = round_dir / f"raw-{slot}.txt"
    if not ran_agy(round_dir, slot) or not raw.exists():
        return ""
    agy = agy_result(raw.read_text(encoding="utf-8", errors="replace"))
    if agy is None or agy.get("status") == "SUCCESS":
        return ""
    error = str(agy.get("error") or "no error given").strip()[:_SALVAGE_CHARS]
    return _printable(f"status {agy.get('status')}: {error}")


def _salvage(round_dir: Path, slot: int) -> str:
    """Whatever this reviewer wrote, unwrapped as far as it can be, safe to print."""
    return _printable(_unwrapped(round_dir, slot))


def _unwrapped(round_dir: Path, slot: int) -> str:
    """Whatever this reviewer wrote, unwrapped as far as it can be."""
    raw = round_dir / f"raw-{slot}.txt"
    if not raw.exists():
        return ""
    text = raw.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return ""
    agy = agy_result(text) if ran_agy(round_dir, slot) else None
    if agy is not None:
        # A failed turn has no response but says why, which is the useful part. So
        # does one cut short by a refused tool call, which otherwise reads as silence.
        said = agy.get("response") or agy.get("error") or ""
        actions = agy.get("denied_actions")
        denied = [
            str(d.get("display_name") or d.get("action"))
            for d in (actions if isinstance(actions, list) else [])
            if isinstance(d, dict) and (d.get("display_name") or d.get("action"))
        ]
        if not said and denied:
            said = (
                f"agy ended its turn with no answer after a refused tool call"
                f" ({', '.join(denied)})"
            )
        return str(said).strip()[:_SALVAGE_CHARS]
    try:
        wrapper = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text[:_SALVAGE_CHARS]
    if isinstance(wrapper, dict) and isinstance(wrapper.get("result"), str):
        return str(wrapper["result"]).strip()[:_SALVAGE_CHARS]
    return text[:_SALVAGE_CHARS]
