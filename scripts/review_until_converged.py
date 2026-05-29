"""Run the ensemble review in a loop until convergence.

Each round: preflight audit + 4 parallel reviewers + merge agent + tests.
Stops when:
  - Only suggestions remain (converged)
  - Same blocking findings appear twice in a row (stuck)
  - Tests fail after fixes (regression -- stops without rollback)
  - Max rounds reached

Usage:
    python scripts/review_until_converged.py \\
        --repo /path/to/repo \\
        --max-rounds 5 \\
        --auto-commit

Loop pieces live in ``scripts/review_loop/``. This file is the CLI
entry point and a back-compat re-export shim so existing imports like
``from scripts.review_until_converged import run_gate`` keep working.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# When invoked as `python scripts/review_until_converged.py`, the repo
# root isn't on sys.path yet -- the sub-package import needs it.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Re-export the public API so existing imports keep working.
from scripts.review_loop import (  # noqa: E402
    MERGE_TIMEOUT,
    REPO_ROOT,
    REVIEWER_TIMEOUT,
    SCRIPTS_DIR,
    TEST_TIMEOUT,
    FindingKey,
    SecretsDetectedError,
    _run,
    apply_fixes,
    build_reviewer_prompt,
    collect_blocking_findings,
    collect_diff,
    commit_and_push,
    detect_backends,
    extract_results,
    fingerprint,
    launch_reviewers,
    run_gate,
    run_preflight,
    run_tests,
    validate_result,
)

__all__ = [
    "MERGE_TIMEOUT",
    "REPO_ROOT",
    "REVIEWER_TIMEOUT",
    "SCRIPTS_DIR",
    "TEST_TIMEOUT",
    "FindingKey",
    "SecretsDetectedError",
    "_run",
    "apply_fixes",
    "build_reviewer_prompt",
    "collect_blocking_findings",
    "collect_diff",
    "commit_and_push",
    "detect_backends",
    "extract_results",
    "fingerprint",
    "launch_reviewers",
    "run_gate",
    "run_preflight",
    "run_tests",
    "validate_result",
]


def _run_one_round(
    round_num: int,
    session_dir: Path,
    repo: Path,
    backends: dict[str, bool],
    context: str,
    previous_fp: set[FindingKey],
    auto_commit: bool,
    prefer_hermes: bool = False,
) -> tuple[int | None, set[FindingKey]]:
    """Execute a single round. Return (exit_code or None, new_fingerprint).

    exit_code is None when the round completed and the loop should continue.
    """
    print(f"\n=== Round {round_num} ===")
    round_dir = session_dir / f"round-{round_num}"
    round_dir.mkdir(exist_ok=True)

    try:
        diff_path, n_lines = collect_diff(round_dir, repo)
    except SecretsDetectedError as e:
        print(f"\nABORT: secrets detected in diff.\n\n{e}")
        return 8, previous_fp
    print(f"Diff: {n_lines} lines")
    if n_lines == 0:
        print("No changes -- nothing to review.")
        return 0, previous_fp

    audit_path = run_preflight(round_dir, repo)
    diff_text = diff_path.read_text()
    audit_text = audit_path.read_text()

    t0 = time.time()
    launch_reviewers(round_dir, diff_text, audit_text, context, backends, prefer_hermes)
    print(f"Reviewers finished in {time.time() - t0:.0f}s")

    results = extract_results(round_dir)
    n_ok = sum(1 for r in results if r is not None)
    print(f"Results: {n_ok}/4 reviewers succeeded")
    if n_ok == 0:
        print(
            "\nERROR: no reviewer results could be parsed; "
            "cannot determine convergence."
        )
        return 7, previous_fp

    blocking = collect_blocking_findings(results)
    print(f"Blocking findings: {len(blocking)} (blocking=true or critical)")
    if not blocking:
        print("\nCONVERGED: only suggestions remain.")
        return 0, previous_fp

    current_fp = fingerprint(blocking)
    if current_fp == previous_fp:
        print("\nSTUCK: same findings as previous round. Manual review needed.")
        return 2, current_fp
    if previous_fp:
        new = current_fp - previous_fp
        repeat = current_fp & previous_fp
        print(f"  New: {len(new)}, Repeated: {len(repeat)}")

    if not apply_fixes(blocking, round_dir, backends, repo):
        print("\nMerge agent failed -- stopping.")
        return 3, current_fp

    gate_ok, gate_output = run_gate(repo)
    (round_dir / "gate-output.txt").write_text(gate_output)
    if not gate_ok:
        print(
            "\nGate failed (lint/format/mypy/tests) after fixes -- "
            "stopping without destructive reset."
        )
        return 4, current_fp
    print("Gate (lint + format + mypy + tests) pass.")

    if auto_commit and not commit_and_push(round_num, len(blocking), repo):
        print("Commit/push failed -- stopping.")
        return 5, current_fp

    return None, current_fp


def main() -> int:
    """CLI entry point."""
    # Force line-buffered stdout so progress is visible when output
    # is redirected to a file or pipe (e.g. background invocations).
    # mypy doesn't know sys.stdout is always a TextIOWrapper here.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument(
        "--auto-commit", action="store_true", help="Commit and push after each round"
    )
    parser.add_argument("--review-dir", type=Path, default=None)
    parser.add_argument(
        "--prefer-hermes",
        action="store_true",
        help=(
            "Route Anthropic reviewers through Hermes/Bedrock (paid API) "
            "instead of the local claude CLI. Also triggered automatically "
            "when .use-hermes exists in the project root."
        ),
    )
    args = parser.parse_args()

    repo = args.repo.resolve()
    review_dir = (
        args.review_dir if args.review_dir is not None else repo / "data" / "reviews"
    )

    backends = detect_backends()
    print(f"Backends: {backends}")
    if not backends["claude"] and not backends["hermes"]:
        print("ERROR: need claude or hermes for Anthropic reviewers")
        return 1

    # Bedrock opt-in: explicit flag OR project-pinned via .use-hermes
    # at repo root. (Marker lives outside .claude/ so it can be committed
    # even when .claude/ is gitignored.) Default is the free local CLI
    # path so casual users aren't surprised by Bedrock charges.
    prefer_hermes = args.prefer_hermes or (repo / ".use-hermes").exists()
    if prefer_hermes:
        print("Bedrock opt-in: routing Anthropic reviewers via Hermes")

    session_id = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    session_dir = review_dir / f"loop_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    print(f"Session: {session_dir}")

    previous_fp: set[FindingKey] = set()
    context = "Autonomous review loop. Find and fix all issues until convergence."

    for round_num in range(1, args.max_rounds + 1):
        exit_code, previous_fp = _run_one_round(
            round_num,
            session_dir,
            repo,
            backends,
            context,
            previous_fp,
            args.auto_commit,
            prefer_hermes,
        )
        if exit_code is not None:
            return exit_code

    print(f"\nMAX ROUNDS ({args.max_rounds}) reached without convergence.")
    return 6


if __name__ == "__main__":
    sys.exit(main())
