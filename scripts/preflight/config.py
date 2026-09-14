"""Configuration constants for the preflight audit.

Edit ``SIGNED_MANIFESTS`` and ``ARTIFACT_DIRS`` to point at your
project's signed JSON artifacts and the directories whose changed JSON
files should be parsed.

**A host project should not edit this file.** ``install.sh`` copies it into
every host, so an edit here is overwritten the next time the agent is
re-vendored -- silently, and the only symptom is a check that stops covering
anything. Put the overrides in ``review-config.json`` at the repo root
instead; see :data:`HOST_CONFIG`. Found by a reviewer that noticed the
coverage gate was configured for ``src/`` in a monorepo whose code is under
``packages/*/src`` and ``services/*/src``, so it had evaluated none of the
changes it was reporting on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: A host project's overrides, read from the repo root. Optional, and every
#: key in it is optional: what is not there keeps the default below.
#:
#: Survives re-vendoring, which is the whole point -- the file the installer
#: copies is not the file a project edits.
HOST_CONFIG = REPO_ROOT / "review-config.json"


def _overrides() -> dict[str, object]:
    """Whatever the host project put in ``review-config.json``.

    Empty on anything unreadable rather than raising: this is configuration for
    a review, and a review that refuses to start over a stray comma is worse
    than one that runs with the defaults and says so.
    """
    try:
        loaded = json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _dirs(name: str, fallback: list[str]) -> list[str]:
    """A list-of-directories setting, from the host config or the default."""
    found = _overrides().get(name)
    if not isinstance(found, list) or not found:
        return fallback
    return [str(entry) for entry in found if isinstance(entry, str) and entry]

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
SOURCE_DIRS: list[str] = _dirs("source_dirs", ["src/", "research/"])

# Directories that hold tests. Used by the test/impl alignment warning.
TEST_DIRS: list[str] = _dirs("test_dirs", ["tests/"])

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
