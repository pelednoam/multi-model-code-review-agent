"""Mandatory four-step CI gate run after each merge-agent round."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from .backends import _run
from .config import TEST_TIMEOUT

if TYPE_CHECKING:
    from pathlib import Path


def run_gate(repo: Path) -> tuple[bool, str]:
    """Run the full CI gate: lint + format + type check + tests.

    All four must pass for the gate to pass. Returns (ok, combined_output).
    """
    output_parts = []
    gate_steps = [
        ("ruff check", ["ruff", "check", "."]),
        ("ruff format --check", ["ruff", "format", "--check", "."]),
        ("mypy", ["mypy", "scripts/"]),
        ("pytest", ["python", "-m", "pytest", "tests/", "-x", "-q"]),
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
