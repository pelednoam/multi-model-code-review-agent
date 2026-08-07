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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

ARTIFACTS_FILENAME = "ARTIFACTS.md"

_ARTIFACTS_DOC = """# Where this round's output lives

Read `result-*.json`. Everything else is intermediate.

| File | What it is |
|---|---|
| `result-N.json` | **The findings.** One validated object per reviewer, matching `docs/ensemble_review_result_schema.json`. This is what you want. |
| `raw-N.txt` | Whatever the CLI printed on stdout, before parsing. Empty for the codex slot on a *successful* run — see below. |
| `stderr-N.txt` | Diagnostics. Read this when a slot is missing a `result-N.json`. |
| `prompt-N.txt` | The exact prompt that reviewer received. |
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
    if missing:
        slots = ",".join(str(i) for i in missing)
        lines.append(f"  NO RESULT  -> slots {slots}; see stderr-{{{slots}}}.txt")
    lines.append(f"  file guide -> {round_dir / ARTIFACTS_FILENAME}")
    return "\n".join(lines)
