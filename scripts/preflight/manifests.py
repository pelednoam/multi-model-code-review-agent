"""Audit signed-manifest JSON files.

For each path in ``SIGNED_MANIFESTS``, report the file's SHA-256 prefix
and a flattened summary of its top-level fields. Used by the spec-
contract reviewer to spot drift between code and signed artifacts.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, SIGNED_MANIFESTS
from .git_state import sha256_file


def _safe_read_json(path: Path) -> Any | None:
    """Read and parse a JSON file, returning None on failure.

    Returns whatever the JSON document decodes to (dict, list, scalar);
    callers must check the type before using it.
    """
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def audit_signed_manifests() -> list[dict[str, Any]]:
    """Read each signed manifest and report its SHA + top-level fields."""
    results = []

    for name, rel_path in SIGNED_MANIFESTS.items():
        path = REPO_ROOT / rel_path
        if not path.exists():
            results.append(
                {"manifest": name, "warning": f"configured but missing: {rel_path}"}
            )
            continue
        data = _safe_read_json(path)
        if data is None:
            results.append({"manifest": name, "warning": "failed to parse JSON"})
            continue
        if not isinstance(data, dict):
            results.append(
                {
                    "manifest": name,
                    "warning": f"not a JSON object (got {type(data).__name__})",
                }
            )
            continue
        entry: dict[str, Any] = {
            "manifest": name,
            "sha256": sha256_file(path)[:16],
        }
        for key in itertools.islice(data.keys(), 20):
            val = data[key]
            if isinstance(val, (str, int, float, bool)):
                entry[key] = val
            elif isinstance(val, (list, dict)):
                entry[f"n_{key}"] = len(val)
        results.append(entry)

    return results
