"""CLI backend detection and a thin subprocess wrapper."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import REPO_ROOT


def _run(
    cmd: list[str], cwd: Path | None = None, **kwargs
) -> subprocess.CompletedProcess[str]:
    """Run a command with sensible defaults."""
    return subprocess.run(
        cmd,
        cwd=cwd if cwd is not None else REPO_ROOT,
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
