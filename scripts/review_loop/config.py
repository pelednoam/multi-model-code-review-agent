"""Configuration constants for the convergence loop."""

from __future__ import annotations

from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent

REVIEWER_TIMEOUT = 600
MERGE_TIMEOUT = 900
TEST_TIMEOUT = 300
