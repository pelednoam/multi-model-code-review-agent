"""Configuration constants for the preflight audit.

Edit ``SIGNED_MANIFESTS`` and ``ARTIFACT_DIRS`` to point at your
project's signed JSON artifacts and the directories whose changed JSON
files should be parsed.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Map logical names to repo-relative paths of JSON artifacts whose
# contents should be audited.
SIGNED_MANIFESTS: dict[str, str] = {
    # "column_manifest": "path/to/manifest.json",
}

# Directories containing JSON artifacts. Changed files under these
# directories are parsed and key fields extracted. Uses os.sep-aware
# prefix matching.
ARTIFACT_DIRS: list[str] = [
    "data/",
    "docs/",
]

# Directories that hold source code in this project. Drives the
# coverage gap detector and the test/impl alignment check. Override
# this for monorepos or non-standard layouts (e.g. ["backend/",
# "frontend/src/", "lib/"]). The defaults match the conventional
# "src layout" plus a research workspace; for a Django/FastAPI/Next
# project you almost certainly need to override.
SOURCE_DIRS: list[str] = [
    "src/",
    "research/",
]

# Directories that hold tests. Used by the test/impl alignment warning.
TEST_DIRS: list[str] = [
    "tests/",
]

SUSPICIOUS_COMPILED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"fill_value\s*=\s*0"), "silent zero-fill"),
    (re.compile(r"\.fillna\s*\(\s*0"), "silent NaN fill with 0"),
    (re.compile(r"\.reindex\(.*fill_value"), "reindex with fill_value"),
    (re.compile(r"except\s+Exception"), "broad except Exception"),
    (re.compile(r"except\s*:"), "bare except"),
    (re.compile(r'"/home/'), "hardcoded absolute path"),
    (re.compile(r"Path\(\"/home/"), "hardcoded absolute Path"),
]

# Paths excluded from suspicious-pattern scanning to avoid false
# positives from the scanner's own regex definitions.
SELF_SKIP_PREFIXES: tuple[str, ...] = (
    "scripts/review_preflight.py",
    "scripts/preflight/",
)

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

# Target coverage percent. Files below this in coverage_gaps audit.
COVERAGE_TARGET = 100
