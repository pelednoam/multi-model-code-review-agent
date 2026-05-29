"""Build reviewer prompts and launch the 4 reviewers in parallel."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import REVIEWER_TIMEOUT

_LENSES = [
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


def _hermes_cmd(prompt_path: Path, model: str) -> list[str]:
    return [
        "hermes",
        "-z",
        f"@{prompt_path}",
        "-m",
        model,
        "--provider",
        "bedrock",
        "-t",
        "file",
        "--yolo",
    ]


def _claude_cmd(slot: int) -> list[str]:
    model = "opus" if slot in (1, 4) else "sonnet" if slot == 3 else "haiku"
    return [
        "claude",
        "-p",
        "--model",
        model,
        "--allowedTools",
        "Read Grep Glob",
        "--output-format",
        "json",
    ]


def _choose_command(
    slot: int,
    prompt_path: Path,
    round_dir: Path,
    backends: dict[str, bool],
    rebuild_prompt: callable,
) -> list[str] | None:
    """Pick the command for a reviewer slot, with fallbacks across backends."""
    if slot in (1, 4) and backends["hermes"]:
        return _hermes_cmd(prompt_path, "us.anthropic.claude-opus-4-6-v1")
    if slot == 2 and backends["codex"]:
        result_path = round_dir / f"result-{slot}.json"
        return ["codex", "exec", "-o", str(result_path), "-"]
    if slot == 2 and backends["hermes"]:
        rebuild_prompt("claude-haiku-4.5")
        return _hermes_cmd(prompt_path, "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    if slot == 3 and backends["gemini"]:
        return ["gemini", "--approval-mode", "plan", "--skip-trust"]
    if slot == 3 and backends["hermes"]:
        return _hermes_cmd(prompt_path, "us.anthropic.claude-sonnet-4-6-v1")
    if backends["claude"]:
        return _claude_cmd(slot)
    return None


def launch_reviewers(
    round_dir: Path,
    diff: str,
    audit: str,
    context: str,
    backends: dict[str, bool],
) -> None:
    """Launch 4 reviewers in parallel, write raw outputs to round_dir."""
    procs = []
    for i, (lens, focus, rv, ml) in enumerate(_LENSES, 1):
        prompt = build_reviewer_prompt(lens, focus, rv, ml, diff, audit, context)
        prompt_path = round_dir / f"prompt-{i}.txt"
        prompt_path.write_text(prompt)
        out_path = round_dir / f"raw-{i}.txt"
        err_path = round_dir / f"stderr-{i}.txt"

        # Closure so _choose_command can rebuild the prompt with a different
        # model label without us threading it through every branch.
        prompt_holder = {"text": prompt}

        def rebuild(label: str, _i=i, _lens=lens, _focus=focus, _rv=rv) -> None:
            new_prompt = build_reviewer_prompt(
                _lens, _focus, _rv, label, diff, audit, context
            )
            prompt_path.write_text(new_prompt)
            prompt_holder["text"] = new_prompt

        cmd = _choose_command(i, prompt_path, round_dir, backends, rebuild)
        if cmd is None:
            print(f"  R{i}: SKIPPED (no backend)")
            continue
        out_f = open(out_path, "w")
        err_f = open(err_path, "w")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if cmd[0] in ("codex", "gemini", "claude") else None,
            stdout=out_f,
            stderr=err_f,
            text=True,
        )
        if cmd[0] in ("codex", "gemini", "claude"):
            proc.stdin.write(prompt_holder["text"])
            proc.stdin.close()
        procs.append((i, proc, cmd[0], out_f, err_f))
        print(f"  R{i}: launched via {cmd[0]}")
    for i, proc, _, out_f, err_f in procs:
        try:
            proc.wait(timeout=REVIEWER_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            print(f"  R{i}: TIMEOUT after {REVIEWER_TIMEOUT}s")
        finally:
            out_f.close()
            err_f.close()
