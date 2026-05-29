"""Spawn the clean-context merge agent to apply reviewer fixes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import MERGE_TIMEOUT


def _format_fixes(findings: list[dict]) -> str:
    return "\n\n".join(
        f"### Fix {i} ({f['_reviewer']}): {f['file']}\n"
        f"Issue: {f.get('issue', '')}\n"
        f"Suggested fix: {f.get('suggested_fix', '(no fix provided)')}"
        for i, f in enumerate(findings, 1)
    )


def _build_prompt(findings: list[dict], repo: Path) -> str:
    return (
        "You are a code merge agent. You have NOT seen the development "
        "conversation. Apply these concrete code fixes from independent "
        "reviewers.\n\n"
        f"Repository: {repo}\n\n"
        f"Fixes to apply:\n\n{_format_fixes(findings)}\n\n"
        "Read each affected file using the Read tool. Apply each fix "
        "using the reviewer's code verbatim. Do NOT rewrite or improve "
        "fixes. If a fix doesn't apply cleanly, report which fix failed. "
        "After all fixes are applied, report what changed."
    )


def apply_fixes(
    findings: list[dict],
    round_dir: Path,
    backends: dict[str, bool],
    repo: Path,
) -> bool:
    """Spawn merge agent (claude -p) to apply fixes. Return True on success."""
    if not backends["claude"]:
        print("  Cannot apply fixes: claude CLI not installed")
        return False
    prompt = _build_prompt(findings, repo)
    (round_dir / "merge-prompt.txt").write_text(prompt)
    print(f"  Launching merge agent for {len(findings)} fixes...")
    out_f = open(round_dir / "merge-output.json", "w")
    err_f = open(round_dir / "merge-stderr.txt", "w")
    try:
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
            cwd=repo,
            stdin=subprocess.PIPE,
            stdout=out_f,
            stderr=err_f,
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
    finally:
        out_f.close()
        err_f.close()
    return proc.returncode == 0
