"""Parse changed JSON artifacts and extract key fields for reviewers."""

from __future__ import annotations

import json
from typing import Any

from .config import ARTIFACT_DIRS, MAX_ARTIFACT_BYTES, REPO_ROOT
from .git_state import is_safe_relpath

_TOP_LEVEL_SCALAR_KEYS = (
    "n_features",
    "n_v10_features",
    "n_rounds",
    "seeds",
    "status",
    "step",
    "score_transform",
)

_PER_CELL_LEN_KEYS = ("train_pids", "dev_pids", "test_pids")

_CELL_ARRAY_KEYS = (
    "cell_name",
    "n_features",
    "n_train",
    "n_sf",
    "n_po",
    "d_a0",
    "d_v10_v4",
    "delta",
    "pass_threshold",
)


def _summarize_per_cell(per_cell: dict) -> dict[str, dict[str, Any]]:
    cells = {}
    for cell_name, cell_data in per_cell.items():
        cell_info: dict[str, Any] = {}
        for role in _PER_CELL_LEN_KEYS:
            if role in cell_data:
                cell_info[role] = len(cell_data[role])
        if "test_final" in cell_data:
            cell_info["n_test_final"] = len(cell_data["test_final"])
        if "per_patient" in cell_data:
            cell_info["n_per_patient"] = len(cell_data["per_patient"])
        cells[cell_name] = cell_info
    return cells


def _summarize_cells_array(cells_list: list) -> list[dict[str, Any]]:
    out = []
    for cell in cells_list:
        if not isinstance(cell, dict):
            continue
        cell_summary: dict[str, Any] = {}
        for key in _CELL_ARRAY_KEYS:
            if key in cell:
                cell_summary[key] = cell[key]
        out.append(cell_summary)
    return out


def collect_changed_artifacts(
    changed_files: list[str],
) -> list[dict[str, Any]]:
    """Parse changed JSON artifacts and extract key fields."""
    artifacts = []
    for f in changed_files:
        if not is_safe_relpath(f):
            continue
        path = REPO_ROOT / f
        if path.suffix != ".json" or not path.exists():
            continue

        is_artifact = any(f == d.rstrip("/") or f.startswith(d) for d in ARTIFACT_DIRS)
        if not is_artifact:
            continue

        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            artifacts.append(
                {"file": f, "error": f"exceeds {MAX_ARTIFACT_BYTES} byte limit"}
            )
            continue

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            artifacts.append({"file": f, "error": "failed to parse"})
            continue

        summary: dict[str, Any] = {"file": f}
        for key in _TOP_LEVEL_SCALAR_KEYS:
            if key in data:
                summary[key] = data[key]

        if "per_cell" in data and isinstance(data["per_cell"], dict):
            summary["cells"] = _summarize_per_cell(data["per_cell"])

        if "cells" in data and isinstance(data["cells"], list):
            summary["cells_array"] = _summarize_cells_array(data["cells"])

        if "admitted_csv_columns" in data:
            summary["n_admitted_columns"] = len(data["admitted_csv_columns"])

        artifacts.append(summary)

    return artifacts
