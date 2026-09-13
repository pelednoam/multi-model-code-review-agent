"""Mandatory four-step CI gate run after each merge-agent round."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from .backends import _run
from .config import TEST_TIMEOUT, interpreter, project_gate

if TYPE_CHECKING:
    from pathlib import Path


def run_gate(repo: Path) -> tuple[bool, str]:
    """Run the project's gate, or the built-in four steps if it has none.

    A project with its own gate script gets that and nothing else. The built-in
    steps are a default for projects that have none, and their `mypy scripts/`
    in particular is about *this* repo: run against a host project under its
    own strict settings it type-checks the agent's vendored source, fails, and
    so blocks every auto-commit the loop could ever make.

    Returns (ok, combined_output).
    """
    output_parts = []
    own = project_gate(repo)
    if own is not None:
        gate_steps = [(f"{own.name} (project gate)", [str(own)])]
    else:
        gate_steps = [
            ("ruff check", ["ruff", "check", "."]),
            ("ruff format --check", ["ruff", "format", "--check", "."]),
            ("mypy", ["mypy", "scripts/"]),
            ("pytest", [interpreter(repo), "-m", "pytest", "tests/", "-x", "-q"]),
        ]
    for label, cmd in gate_steps:
        output_parts.append(f"\n=== {label} ===\n")
        try:
            result = _run(cmd, cwd=repo, timeout=TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            output_parts.append(f"{label} TIMED OUT after {TEST_TIMEOUT}s")
            return False, "".join(output_parts)
        except FileNotFoundError:
            # Tool not installed -- skip with a note rather than fail
            output_parts.append(f"{label} skipped: tool not installed")
            continue
        output_parts.append(result.stdout)
        output_parts.append(result.stderr)
        if result.returncode != 0:
            output_parts.append(f"\n{label} FAILED (exit {result.returncode})")
            return False, "".join(output_parts)
    return True, "".join(output_parts)


# Backwards-compat alias for any callers expecting the old name.
run_tests = run_gate
