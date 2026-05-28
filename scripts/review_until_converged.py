"""Run the ensemble review in a loop until convergence.

Each round: preflight audit + 4 parallel reviewers + merge agent + tests.
Stops when:
  - Only suggestions remain (converged)
  - Same blocking findings appear twice in a row (stuck)
  - Tests fail after fixes (regression -- rolls back)
  - Max rounds reached

Usage:
    python scripts/review_until_converged.py \\
        --repo /path/to/repo \\
        --max-rounds 5 \\
        --auto-commit
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent

REVIEWER_TIMEOUT = 600
MERGE_TIMEOUT = 900
TEST_TIMEOUT = 300


@dataclass(frozen=True)
class FindingKey:
    """Stable identity for a finding across rounds."""

    file: str
    issue: str

    @classmethod
    def from_dict(cls, f: dict) -> FindingKey:
        return cls(file=f.get("file", ""), issue=f.get("issue", ""))


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Run a command with sensible defaults."""
    return subprocess.run(
        cmd,
        cwd=kwargs.pop("cwd", REPO_ROOT),
        capture_output=kwargs.pop("capture_output", True),
        text=True,
        check=False,
        **kwargs,
    )


def detect_backends() -> dict[str, bool]:
    """Detect which CLI backends are installed."""
    return {
        cli: shutil.which(cli) is not None
        for cli in ("hermes", "claude", "codex", "gemini")
    }


def collect_diff(round_dir: Path) -> tuple[Path, int]:
    """Collect scrubbed diff against origin/main."""
    diff_path = round_dir / "diff.patch"
    git = _run(["git", "diff", "--merge-base", "origin/main", "--", "."])
    if not git.stdout.strip():
        git = _run(["git", "diff", "HEAD~1"])
    scrubber = subprocess.Popen(
        ["python", str(SCRIPTS_DIR / "scrub_diff.py")],
        stdin=subprocess.PIPE,
        stdout=open(diff_path, "w"),
        stderr=subprocess.PIPE,
        text=True,
    )
    _, scrub_err = scrubber.communicate(input=git.stdout)
    if scrubber.returncode != 0:
        print(
            f"  WARNING: scrubber redacted lines: {scrub_err.strip()}", file=sys.stderr
        )
    n_lines = sum(1 for _ in open(diff_path))
    return diff_path, n_lines


def run_preflight(round_dir: Path) -> Path:
    """Run preflight audit, return path to audit JSON."""
    audit_path = round_dir / "audit.json"
    _run(
        [
            "python",
            str(SCRIPTS_DIR / "review_preflight.py"),
            "--output",
            str(audit_path),
        ]
    )
    return audit_path


def build_reviewer_prompt(
    lens: str,
    focus: str,
    reviewer_id: str,
    model_label: str,
    diff: str,
    audit: str,
    context: str,
) -> str:
    """Build a single reviewer's prompt."""
    schema = (
        "Output ONLY the JSON result object: "
        '{"reviewer":"'
        + reviewer_id
        + '","model":"'
        + model_label
        + '","findings":[{"severity":"critical|warning|suggestion",'
        '"confidence":"high|medium|low",'
        '"category":"security|correctness|spec|runtime|test|perf|docs",'
        '"file":"<path>","line":null,"issue":"<one-line>",'
        '"rationale":"<evidence>",'
        '"observed_or_inferred":"observed_in_diff|observed_in_audit|inferred",'
        '"blocking":false,"suggested_fix":"<concrete before/after code>",'
        '"repro_command":null,"contract_reference":null}],'
        '"overall_assessment":"<2-3 sentences>"}'
    )
    return (
        f"You are a {lens} reviewer. Focus: {focus}. "
        f"reviewer={reviewer_id}, model={model_label}.\n"
        f"{context}\n\n"
        f"DIFF:\n{diff}\n\n"
        f"AUDIT: {audit}\n\n"
        f"{schema}"
    )


def launch_reviewers(
    round_dir: Path,
    diff: str,
    audit: str,
    context: str,
    backends: dict[str, bool],
) -> None:
    """Launch 4 reviewers in parallel, write raw outputs to round_dir."""
    lenses = [
        (
            "security and robustness",
            "injection, regex, scrubber correctness, stdin handling",
            "security",
            "claude-opus-4.6",
        ),
        (
            "correctness and edge cases",
            "logic errors, JSON parsing, bash quoting, edge cases",
            "correctness",
            "gpt-5.5-codex",
        ),
        (
            "readability and performance",
            "naming, dead code, function structure, doc consistency",
            "readability_performance",
            "claude-sonnet-4.6",
        ),
        (
            "spec-contract compliance",
            "schema vs code, model table vs routing, doc alignment",
            "spec_contract",
            "claude-opus-4.6",
        ),
    ]
    procs = []
    for i, (lens, focus, rv, ml) in enumerate(lenses, 1):
        prompt = build_reviewer_prompt(lens, focus, rv, ml, diff, audit, context)
        prompt_path = round_dir / f"prompt-{i}.txt"
        prompt_path.write_text(prompt)
        out_path = round_dir / f"raw-{i}.txt"
        err_path = round_dir / f"stderr-{i}.txt"
        if i in (1, 4) and backends["hermes"]:
            cmd = [
                "hermes",
                "-z",
                prompt,
                "-m",
                "us.anthropic.claude-opus-4-6-v1",
                "--provider",
                "bedrock",
                "-t",
                "file",
                "--yolo",
            ]
        elif i == 2 and backends["codex"]:
            result_path = round_dir / f"result-{i}.json"
            cmd = ["codex", "exec", "-o", str(result_path), "-"]
        elif i == 3 and backends["gemini"]:
            cmd = ["gemini", "--approval-mode", "plan", "--skip-trust"]
        elif backends["claude"]:
            model = "opus" if i in (1, 4) else "sonnet" if i == 3 else "haiku"
            cmd = [
                "claude",
                "-p",
                "--model",
                model,
                "--allowedTools",
                "Read Grep Glob",
                "--output-format",
                "json",
            ]
        else:
            print(f"  R{i}: SKIPPED (no backend)")
            continue
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if cmd[0] in ("codex", "gemini", "claude") else None,
            stdout=open(out_path, "w"),
            stderr=open(err_path, "w"),
            text=True,
        )
        if cmd[0] in ("codex", "gemini", "claude"):
            proc.stdin.write(prompt)
            proc.stdin.close()
        procs.append((i, proc, cmd[0]))
        print(f"  R{i}: launched via {cmd[0]}")
    for i, proc, _ in procs:
        try:
            proc.wait(timeout=REVIEWER_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            print(f"  R{i}: TIMEOUT after {REVIEWER_TIMEOUT}s")


def extract_results(round_dir: Path) -> list[dict | None]:
    """Extract and validate result JSON from each reviewer's output."""
    results: list[dict | None] = []
    for i in range(1, 5):
        result_path = round_dir / f"result-{i}.json"
        if result_path.exists():
            try:
                results.append(json.loads(result_path.read_text()))
                continue
            except (json.JSONDecodeError, OSError):
                pass
        raw_path = round_dir / f"raw-{i}.txt"
        if not raw_path.exists():
            results.append(None)
            continue
        text = raw_path.read_text()
        try:
            wrapper = json.loads(text)
            if isinstance(wrapper.get("result"), str):
                text = wrapper["result"]
        except (json.JSONDecodeError, ValueError):
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            results.append(None)
            continue
        try:
            d = json.loads(text[start : end + 1])
            required = {"reviewer", "model", "findings", "overall_assessment"}
            if required.issubset(d.keys()):
                with open(result_path, "w") as f:
                    json.dump(d, f, indent=2)
                results.append(d)
                continue
        except (json.JSONDecodeError, ValueError):
            pass
        results.append(None)
    return results


def collect_blocking_findings(results: list[dict | None]) -> list[dict]:
    """Return all critical/warning findings across reviewers."""
    blocking = []
    for r in results:
        if r is None:
            continue
        for f in r.get("findings", []):
            if f.get("severity") in ("critical", "warning"):
                blocking.append({**f, "_reviewer": r.get("reviewer", "?")})
    return blocking


def fingerprint(findings: list[dict]) -> set[FindingKey]:
    """Stable fingerprint of a set of findings."""
    return {FindingKey.from_dict(f) for f in findings}


def apply_fixes(
    findings: list[dict],
    round_dir: Path,
    backends: dict[str, bool],
) -> bool:
    """Spawn merge agent (claude -p) to apply fixes. Return True on success."""
    if not backends["claude"]:
        print("  Cannot apply fixes: claude CLI not installed")
        return False
    fixes_text = "\n\n".join(
        f"### Fix {i} ({f['_reviewer']}): {f['file']}\n"
        f"Issue: {f.get('issue', '')}\n"
        f"Suggested fix: {f.get('suggested_fix', '(no fix provided)')}"
        for i, f in enumerate(findings, 1)
    )
    prompt = (
        "You are a code merge agent. You have NOT seen the development "
        "conversation. Apply these concrete code fixes from independent "
        "reviewers.\n\n"
        f"Repository: {REPO_ROOT}\n\n"
        f"Fixes to apply:\n\n{fixes_text}\n\n"
        "Read each affected file using the Read tool. Apply each fix "
        "using the reviewer's code verbatim. Do NOT rewrite or improve "
        "fixes. If a fix doesn't apply cleanly, report which fix failed. "
        "After all fixes are applied, report what changed."
    )
    (round_dir / "merge-prompt.txt").write_text(prompt)
    print(f"  Launching merge agent for {len(findings)} fixes...")
    proc = subprocess.Popen(
        [
            "claude",
            "-p",
            "--model",
            "opus",
            "--allowedTools",
            "Read Edit Write Bash Grep Glob",
            "--output-format",
            "json",
        ],
        stdin=subprocess.PIPE,
        stdout=open(round_dir / "merge-output.json", "w"),
        stderr=open(round_dir / "merge-stderr.txt", "w"),
        text=True,
    )
    proc.stdin.write(prompt)
    proc.stdin.close()
    try:
        proc.wait(timeout=MERGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"  Merge agent TIMEOUT after {MERGE_TIMEOUT}s")
        return False
    return proc.returncode == 0


def run_tests() -> tuple[bool, str]:
    """Run pytest. Return (passed, output)."""
    result = _run(
        ["python", "-m", "pytest", "tests/", "-x", "-q"],
        timeout=TEST_TIMEOUT,
    )
    return result.returncode == 0, result.stdout + result.stderr


def commit_and_push(round_num: int, n_fixes: int) -> bool:
    """Commit and push current changes."""
    _run(["git", "add", "-A"])
    status = _run(["git", "status", "--short"])
    if not status.stdout.strip():
        print(f"  Nothing to commit after round {round_num}")
        return True
    msg = (
        f"Round {round_num}: apply {n_fixes} fixes from ensemble review\n\n"
        "Applied by merge agent (clean context).\n\n"
        "Co-Authored-By: review-loop <noreply@anthropic.com>"
    )
    commit = _run(["git", "commit", "-m", msg])
    if commit.returncode != 0:
        print(f"  Commit failed: {commit.stderr}")
        return False
    push = _run(["git", "push"])
    if push.returncode != 0:
        print(f"  Push failed: {push.stderr}")
        return False
    return True


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument(
        "--auto-commit", action="store_true", help="Commit and push after each round"
    )
    parser.add_argument(
        "--review-dir", type=Path, default=REPO_ROOT / "data" / "reviews"
    )
    args = parser.parse_args()

    backends = detect_backends()
    print(f"Backends: {backends}")
    if not backends["claude"] and not backends["hermes"]:
        print("ERROR: need claude or hermes for Anthropic reviewers")
        return 1

    session_id = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    session_dir = args.review_dir / f"loop_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    print(f"Session: {session_dir}")

    previous_fp: set[FindingKey] = set()
    context = "Autonomous review loop. Find and fix all issues until convergence."

    for round_num in range(1, args.max_rounds + 1):
        print(f"\n=== Round {round_num} ===")
        round_dir = session_dir / f"round-{round_num}"
        round_dir.mkdir(exist_ok=True)

        diff_path, n_lines = collect_diff(round_dir)
        print(f"Diff: {n_lines} lines")
        if n_lines == 0:
            print("No changes -- nothing to review.")
            return 0

        audit_path = run_preflight(round_dir)
        diff_text = diff_path.read_text()
        audit_text = audit_path.read_text()

        t0 = time.time()
        launch_reviewers(round_dir, diff_text, audit_text, context, backends)
        print(f"Reviewers finished in {time.time() - t0:.0f}s")

        results = extract_results(round_dir)
        n_ok = sum(1 for r in results if r is not None)
        print(f"Results: {n_ok}/4 reviewers succeeded")

        blocking = collect_blocking_findings(results)
        print(f"Blocking findings: {len(blocking)} (critical+warning)")

        if not blocking:
            print("\nCONVERGED: only suggestions remain.")
            return 0

        current_fp = fingerprint(blocking)
        if current_fp == previous_fp:
            print("\nSTUCK: same findings as previous round. Manual review needed.")
            return 2
        new_findings = current_fp - previous_fp
        repeat_findings = current_fp & previous_fp
        if previous_fp:
            print(f"  New: {len(new_findings)}, Repeated: {len(repeat_findings)}")
        previous_fp = current_fp

        ok = apply_fixes(blocking, round_dir, backends)
        if not ok:
            print("\nMerge agent failed -- stopping.")
            return 3

        tests_ok, test_output = run_tests()
        (round_dir / "test-output.txt").write_text(test_output)
        if not tests_ok:
            print("\nTests failed after fixes. Rolling back.")
            _run(["git", "checkout", "."])
            return 4
        print("Tests pass.")

        if args.auto_commit:
            if not commit_and_push(round_num, len(blocking)):
                print("Commit/push failed -- stopping.")
                return 5

    print(f"\nMAX ROUNDS ({args.max_rounds}) reached without convergence.")
    return 6


if __name__ == "__main__":
    sys.exit(main())
