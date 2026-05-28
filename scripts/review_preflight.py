"""Deterministic pre-review checklist and runtime artifact audit.

Runs cheap, non-LLM checks before the ensemble review pipeline.
Collects git state, artifact metadata, and manifest comparisons
into a machine-readable JSON file that every reviewer consumes.

Usage:
    python scripts/review_preflight.py --output audit.json
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Configure these for your project. Map logical names to repo-relative
# paths of JSON artifacts whose contents should be audited.
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

_SUSPICIOUS_COMPILED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"fill_value\s*=\s*0"), "silent zero-fill"),
    (re.compile(r"\.fillna\s*\(\s*0"), "silent NaN fill with 0"),
    (re.compile(r"\.reindex\(.*fill_value"), "reindex with fill_value"),
    (re.compile(r"except\s+Exception"), "broad except Exception"),
    (re.compile(r"except\s*:"), "bare except"),
    (re.compile(r'"/home/'), "hardcoded absolute path"),
    (re.compile(r"Path\(\"/home/"), "hardcoded absolute Path"),
]

# Excluded from suspicious-pattern scanning to avoid false positives
# from its own regex definitions.
_SELF_PATH = str(Path(__file__).resolve().relative_to(REPO_ROOT))

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024


def _run_git(
    args: list[str],
    warnings: list[str],
) -> str:
    """Run a git command and return stdout.

    Args:
        args: Arguments to pass after ``git -C <repo>``.
        warnings: Mutable list to append warning messages to
            if the command fails.

    Returns:
        Stripped stdout. Empty string on failure or timeout.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        cmd = " ".join(["git", *args])
        warnings.append(f"git timed out (30s): {cmd}")
        return ""
    if result.returncode != 0:
        cmd = " ".join(["git", *args])
        stderr_stripped = result.stderr.strip()
        stderr_lines = stderr_stripped.splitlines()
        first = stderr_lines[0] if stderr_lines else "(no stderr)"
        warnings.append(
            f"git failed (exit {result.returncode}): {cmd} -- {first}"
        )
    return result.stdout.strip()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_safe_relpath(relpath: str) -> bool:
    """Check that a git-reported path stays inside the repo root."""
    resolved = (REPO_ROOT / relpath).resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return False
    return True


def collect_git_state(
    warnings: list[str],
) -> dict[str, Any]:
    """Collect comprehensive git state beyond just the diff.

    Args:
        warnings: Mutable list for git command failure messages.
    """
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], warnings)
    commit = _run_git(["rev-parse", "HEAD"], warnings)
    status_short = _run_git(["status", "--short"], warnings)
    cached = _run_git(["diff", "--cached", "--name-only"], warnings)
    untracked = _run_git(
        ["ls-files", "--others", "--exclude-standard"], warnings
    )
    changed_vs_main = _run_git(
        ["diff", "--merge-base", "origin/main", "--name-only"], warnings
    )

    return {
        "branch": branch,
        "commit": commit[:12],
        "status_short": status_short.splitlines() if status_short else [],
        "cached_files": cached.splitlines() if cached else [],
        "untracked_files": untracked.splitlines() if untracked else [],
        "changed_vs_main": changed_vs_main.splitlines()
        if changed_vs_main
        else [],
    }


def collect_changed_artifacts(
    changed_files: list[str],
) -> list[dict[str, Any]]:
    """Parse changed JSON artifacts and extract key fields."""
    artifacts = []
    for f in changed_files:
        if not _is_safe_relpath(f):
            continue
        path = REPO_ROOT / f
        if path.suffix != ".json" or not path.exists():
            continue

        is_artifact = any(
            f == d.rstrip("/") or f.startswith(d) for d in ARTIFACT_DIRS
        )
        if not is_artifact:
            continue

        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            artifacts.append(
                {
                    "file": f,
                    "error": f"exceeds {MAX_ARTIFACT_BYTES} byte limit",
                }
            )
            continue

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            artifacts.append({"file": f, "error": "failed to parse"})
            continue

        summary: dict[str, Any] = {"file": f}

        for key in (
            "n_features",
            "n_v10_features",
            "n_rounds",
            "seeds",
            "status",
            "step",
            "score_transform",
        ):
            if key in data:
                summary[key] = data[key]

        if "per_cell" in data and isinstance(data["per_cell"], dict):
            cells = {}
            for cell_name, cell_data in data["per_cell"].items():
                cell_info: dict[str, Any] = {}
                for role in ("train_pids", "dev_pids", "test_pids"):
                    if role in cell_data:
                        cell_info[role] = len(cell_data[role])
                if "test_final" in cell_data:
                    cell_info["n_test_final"] = len(
                        cell_data["test_final"]
                    )
                if "per_patient" in cell_data:
                    cell_info["n_per_patient"] = len(
                        cell_data["per_patient"]
                    )
                cells[cell_name] = cell_info
            summary["cells"] = cells

        if "cells" in data and isinstance(data["cells"], list):
            cells_list = []
            for cell in data["cells"]:
                if not isinstance(cell, dict):
                    continue
                cell_summary: dict[str, Any] = {}
                for key in (
                    "cell_name",
                    "n_features",
                    "n_train",
                    "n_sf",
                    "n_po",
                    "d_a0",
                    "d_v10_v4",
                    "delta",
                    "pass_threshold",
                ):
                    if key in cell:
                        cell_summary[key] = cell[key]
                cells_list.append(cell_summary)
            summary["cells_array"] = cells_list

        if "admitted_csv_columns" in data:
            summary["n_admitted_columns"] = len(
                data["admitted_csv_columns"]
            )

        artifacts.append(summary)

    return artifacts


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
            results.append({
                "manifest": name,
                "warning": f"configured but missing: {rel_path}",
            })
            continue
        data = _safe_read_json(path)
        if data is None:
            results.append({
                "manifest": name,
                "warning": "failed to parse JSON",
            })
            continue
        if not isinstance(data, dict):
            results.append({
                "manifest": name,
                "warning": (
                    f"not a JSON object (got {type(data).__name__})"
                ),
            })
            continue
        entry: dict[str, Any] = {
            "manifest": name,
            "sha256": _sha256_file(path)[:16],
        }
        for key in itertools.islice(data.keys(), 20):
            val = data[key]
            if isinstance(val, (str, int, float, bool)):
                entry[key] = val
            elif isinstance(val, (list, dict)):
                entry[f"n_{key}"] = len(val)
        results.append(entry)

    return results


def scan_suspicious_patterns(
    changed_files: list[str],
) -> list[dict[str, str]]:
    """Scan changed files for suspicious code patterns."""
    findings = []
    for f in changed_files:
        if f == _SELF_PATH or not _is_safe_relpath(f):
            continue
        path = REPO_ROOT / f
        if not path.exists() or path.suffix != ".py":
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue

        for i, line in enumerate(lines, 1):
            for compiled, description in _SUSPICIOUS_COMPILED:
                if compiled.search(line):
                    findings.append(
                        {
                            "file": f,
                            "line": i,
                            "pattern": description,
                            "text": line.strip()[:120],
                        }
                    )
                    break
    return findings


def check_test_coverage_alignment(
    changed_files: list[str],
) -> list[str]:
    """Check for implementation changes without test changes."""
    warnings = []
    impl_files = {
        f
        for f in changed_files
        if (f.startswith("src/") or f.startswith("research/"))
        and f.endswith(".py")
        and not f.split("/")[-1].startswith("test_")
        and "__init__" not in f
    }
    test_files = {
        f
        for f in changed_files
        if f.startswith("tests/") and f.endswith(".py")
    }

    if impl_files and not test_files:
        warnings.append(
            f"{len(impl_files)} implementation files changed "
            f"but no test files"
        )
    if test_files and not impl_files:
        warnings.append(
            f"{len(test_files)} test files changed "
            f"but no implementation files"
        )

    return warnings


def run_preflight() -> dict[str, Any]:
    """Run all preflight checks and return the audit dict."""
    git_warnings: list[str] = []
    git = collect_git_state(git_warnings)
    all_changed = list(
        dict.fromkeys(git["changed_vs_main"] + git["untracked_files"])
    )

    audit: dict[str, Any] = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "git": git,
        "git_command_warnings": git_warnings,
        "signed_manifests": audit_signed_manifests(),
        "changed_artifacts": collect_changed_artifacts(all_changed),
        "suspicious_patterns": scan_suspicious_patterns(all_changed),
        "test_coverage_alignment": check_test_coverage_alignment(
            all_changed
        ),
    }

    audit["n_warnings"] = (
        sum(1 for m in audit["signed_manifests"] if "warning" in m)
        + len(audit["suspicious_patterns"])
        + len(audit["test_coverage_alignment"])
        + len(audit["git_command_warnings"])
    )

    return audit


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write the audit JSON output",
    )
    args = parser.parse_args()

    audit = run_preflight()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(audit, f, indent=2, default=str)

    print(f"Audit written to {args.output}")
    print(f"  Branch: {audit['git']['branch']}")
    print(f"  Commit: {audit['git']['commit']}")
    print(f"  Changed files: {len(audit['git']['changed_vs_main'])}")
    print(f"  Untracked: {len(audit['git']['untracked_files'])}")
    print(f"  Artifacts: {len(audit['changed_artifacts'])}")
    print(f"  Suspicious: {len(audit['suspicious_patterns'])}")
    print(f"  Warnings: {audit['n_warnings']}")

    sys.exit(1 if audit["n_warnings"] > 0 else 0)


if __name__ == "__main__":
    main()
