"""Configuration constants for the preflight audit.

``SIGNED_MANIFESTS`` and ``ARTIFACT_DIRS`` -- the project's signed JSON
artifacts and the directories whose changed JSON files are parsed -- come from
``review-config.json`` (``signed_manifests``, ``artifact_dirs``), as the layout
does.

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
import os
import re
import sys
from pathlib import Path

#: The repository audited: where this preflight is installed, unless the loop names
#: another -- run from a trusted checkout with --repo, this copy audited and tested
#: the checkout instead of the branch under review.
_INSTALLED_IN = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = Path(os.environ.get("MMCRA_PREFLIGHT_REPO") or _INSTALLED_IN).resolve()

#: A host project's overrides, read from the repo root. Optional, and every
#: key in it is optional: what is not there keeps the default below.
#:
#: Survives re-vendoring, which is the whole point -- the file the installer
#: copies is not the file a project edits.
HOST_CONFIG = REPO_ROOT / "review-config.json"


def _overrides() -> dict[str, object]:
    """Whatever the host project put in ``review-config.json``.

    Defaults on anything unreadable rather than raising: this is configuration
    for a review, and one that refuses to start over a stray comma is worse
    than one that runs with the defaults.

    But it *says so*, on stderr, and that is the whole difference. A host
    writes this file precisely because the defaults do not cover its layout, so
    falling back to them in silence recreates the blind coverage gate the file
    exists to prevent -- which is the failure this whole mechanism was written
    for, one level up.
    """
    if not HOST_CONFIG.exists():
        return {}
    try:
        loaded = json.loads(HOST_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _complain(f"could not be read ({type(exc).__name__}: {exc})")
        return {}
    if not isinstance(loaded, dict):
        _complain(f"is a {type(loaded).__name__}, not an object")
        return {}
    return loaded


#: What ``_complain`` said, for the audit: on stderr alone it reached no reviewer, and
#: a contract check the config named and this skipped looked like none configured.
CONFIG_WARNINGS: list[str] = []


def _complain(
    problem: str,
    outcome: str = "using the agent's defaults, which probably do not cover this "
    "project's layout",
) -> None:
    """Say on stderr, and in the audit, that the host config is being ignored, and
    why."""
    message = f"WARNING: {HOST_CONFIG.name} {problem}; {outcome}."
    CONFIG_WARNINGS.append(message)
    print(message, file=sys.stderr)


def _dirs(name: str, fallback: list[str]) -> list[str]:
    """A list-of-directories setting, from the host config or the default."""
    found = _overrides().get(name)
    if found is None:
        return fallback
    if not isinstance(found, list):
        _complain(f"has a {name!r} that is not a list")
        return fallback
    kept = [entry for entry in found if isinstance(entry, str) and entry.strip()]
    if not kept:
        # An empty list, or one filtered down to nothing, is not a setting --
        # and returning it would leave the gate matching no directory at all,
        # which is worse than the wrong directories.
        _complain(f"has no usable entries in {name!r}")
        return fallback
    return kept


#: For a per-entry complaint, where the rest of the setting stands.
_SKIPPED = "that entry is skipped; the others are kept"


def _manifests(name: str) -> dict[str, str]:
    """A name-to-path mapping setting, from the host config, or none."""
    found = _overrides().get(name)
    if found is None:
        return {}
    if not isinstance(found, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in found.items()
    ):
        _complain(f"has a {name!r} that is not an object of names to paths")
        return {}
    # Inside the repository only: the file is the branch's to write, and a path out of
    # it (absolute, or with "..") would read a host's JSON into the audit, which goes
    # to every reviewer.
    root = REPO_ROOT.resolve()
    kept = {}
    for key, value in found.items():
        try:
            target = (root / value).resolve()
        except (OSError, RuntimeError, ValueError):  # a NUL, a link loop
            _complain(f"has an unusable {name!r} path: {value!r}", _SKIPPED)
            continue
        if Path(value).is_absolute() or not target.is_relative_to(root):
            _complain(f"has a {name!r} entry outside the repository: {value!r}",
                      _SKIPPED)  # fmt: skip
            continue
        kept[key] = value
    return kept


# Map logical names to repo-relative paths of JSON artifacts whose contents should
# be audited: "signed_manifests" in review-config.json, e.g.
# {"column_manifest": "path/to/manifest.json"}. Not set here, which install.sh
# overwrites.
SIGNED_MANIFESTS: dict[str, str] = _manifests("signed_manifests")

# Directories containing JSON artifacts. Changed files under these directories are
# parsed and key fields extracted ("artifact_dirs" in review-config.json). Uses
# os.sep-aware prefix matching.
ARTIFACT_DIRS: list[str] = _dirs("artifact_dirs", ["data/", "docs/"])

# Directories that hold source code in this project. Drives the coverage gap
# detector and the test/impl alignment check. Set "source_dirs" in
# review-config.json for monorepos or non-src layouts (e.g. ["backend/",
# "frontend/src/", "lib/"]); this file is overwritten by install.sh. The defaults
# match the conventional "src layout" plus a research workspace.
SOURCE_DIRS: list[str] = _dirs("source_dirs", ["src/", "research/"])

# Directories that hold tests. The coverage gate runs pytest on these (those that exist
# inside the repository), and the test/impl alignment warning reads them.
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
