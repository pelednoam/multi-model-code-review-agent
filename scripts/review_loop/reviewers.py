"""Build reviewer prompts and launch the 4 reviewers in parallel."""

from __future__ import annotations

import subprocess
from typing import IO, TYPE_CHECKING

from .config import REVIEWER_TIMEOUT

if TYPE_CHECKING:
    from pathlib import Path

ReviewerProc = tuple[int, "subprocess.Popen[str]", str, IO[str], IO[str]]

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


_OPUS_BEDROCK = "us.anthropic.claude-opus-4-6-v1"
_HAIKU_BEDROCK = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_SONNET_BEDROCK = "us.anthropic.claude-sonnet-4-6-v1"

# Gemini's free tier currently grants quota for gemini-2.5-flash only;
# gemini-2.5-pro returns HTTP 429 on first request. Default to Flash so
# the readability reviewer works for unpaid users out of the box; users
# with paid quota can override via the GEMINI_MODEL env var.
_GEMINI_MODEL_DEFAULT = "gemini-2.5-flash"


def _gemini_model() -> str:
    import os

    return os.environ.get("GEMINI_MODEL", _GEMINI_MODEL_DEFAULT)


def _choose_command(
    slot: int,
    prompt_path: Path,
    round_dir: Path,
    backends: dict[str, bool],
    prefer_hermes: bool = False,
) -> tuple[list[str], str | None] | None:
    """Pick the command for a reviewer slot.

    Default precedence (``prefer_hermes=False``): local CLIs first --
    they're free (existing subscriptions) and manage their own auth.
    Hermes/Bedrock is the paid API path; only chosen as a last resort.

    Bedrock opt-in (``prefer_hermes=True``): Hermes/Bedrock first for
    Anthropic reviewers. Used by regulated codebases that need
    data-at-rest in their AWS account.

    Returns ``(cmd, rebuild_label)``, where ``rebuild_label`` is set when
    the chosen backend needs the prompt to be rebuilt with a different
    model label (slot 2's hermes-haiku path). Returns ``None`` if no
    backend is available.
    """
    if prefer_hermes and backends["hermes"]:
        if slot in (1, 4):
            return _hermes_cmd(prompt_path, _OPUS_BEDROCK), None
        if slot == 2:
            return _hermes_cmd(prompt_path, _HAIKU_BEDROCK), "claude-haiku-4.5"
        if slot == 3:
            return _hermes_cmd(prompt_path, _SONNET_BEDROCK), None

    if slot == 2 and backends["codex"]:
        result_path = round_dir / f"result-{slot}.json"
        return ["codex", "exec", "-o", str(result_path), "-"], None
    if slot == 3 and backends["gemini"]:
        return [
            "gemini",
            "--model",
            _gemini_model(),
            "--approval-mode",
            "plan",
            "--skip-trust",
        ], None
    if backends["claude"]:
        return _claude_cmd(slot), None

    # Last-resort Hermes paths if no local CLI matched.
    if slot in (1, 4) and backends["hermes"]:
        return _hermes_cmd(prompt_path, _OPUS_BEDROCK), None
    if slot == 2 and backends["hermes"]:
        return _hermes_cmd(prompt_path, _HAIKU_BEDROCK), "claude-haiku-4.5"
    if slot == 3 and backends["hermes"]:
        return _hermes_cmd(prompt_path, _SONNET_BEDROCK), None
    return None


def _stdin_backends() -> tuple[str, ...]:
    """CLI backends that accept the prompt on stdin (not via filepath)."""
    return ("codex", "gemini", "claude")


def _launch_one(
    slot: int,
    lens_args: tuple[str, str, str, str],
    diff: str,
    audit: str,
    context: str,
    round_dir: Path,
    backends: dict[str, bool],
    prefer_hermes: bool = False,
) -> ReviewerProc | None:
    """Launch a single reviewer subprocess. Return tracking tuple or None."""
    lens, focus, rv, model_label = lens_args
    prompt = build_reviewer_prompt(lens, focus, rv, model_label, diff, audit, context)
    prompt_path = round_dir / f"prompt-{slot}.txt"
    prompt_path.write_text(prompt)

    choice = _choose_command(slot, prompt_path, round_dir, backends, prefer_hermes)
    if choice is None:
        print(f"  R{slot}: SKIPPED (no backend)")
        return None
    cmd, rebuild_label = choice
    if rebuild_label is not None:
        prompt = build_reviewer_prompt(
            lens, focus, rv, rebuild_label, diff, audit, context
        )
        prompt_path.write_text(prompt)

    # The Popen handles intentionally outlive these open() calls; they are
    # closed in launch_reviewers' wait loop after the subprocess finishes.
    out_f = open(round_dir / f"raw-{slot}.txt", "w")  # noqa: SIM115
    err_f = open(round_dir / f"stderr-{slot}.txt", "w")  # noqa: SIM115
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if cmd[0] in _stdin_backends() else None,
        stdout=out_f,
        stderr=err_f,
        text=True,
    )
    if cmd[0] in _stdin_backends():
        assert proc.stdin is not None  # PIPE guaranteed above
        proc.stdin.write(prompt)
        proc.stdin.close()
    print(f"  R{slot}: launched via {cmd[0]}")
    return slot, proc, cmd[0], out_f, err_f


def launch_reviewers(
    round_dir: Path,
    diff: str,
    audit: str,
    context: str,
    backends: dict[str, bool],
    prefer_hermes: bool = False,
) -> None:
    """Launch 4 reviewers in parallel, write raw outputs to round_dir."""
    procs: list[ReviewerProc] = []
    for i, lens_args in enumerate(_LENSES, 1):
        tracked = _launch_one(
            i, lens_args, diff, audit, context, round_dir, backends, prefer_hermes
        )
        if tracked is not None:
            procs.append(tracked)
    for slot, proc, _, out_f, err_f in procs:
        try:
            proc.wait(timeout=REVIEWER_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            print(f"  R{slot}: TIMEOUT after {REVIEWER_TIMEOUT}s")
        finally:
            out_f.close()
            err_f.close()
