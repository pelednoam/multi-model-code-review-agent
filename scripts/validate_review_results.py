"""Validate ensemble review result JSON files against the strict schema.

Uses jsonschema.Draft202012Validator against
docs/ensemble_review_result_schema.json. The schema has
additionalProperties: false at both top and finding level, so extra
fields are rejected.

Usage:
    python scripts/validate_review_results.py result-1.json result-2.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "ensemble_review_result_schema.json"
)


def _load_schema() -> dict:
    """Load the JSON schema from the repo."""
    return json.loads(SCHEMA_PATH.read_text())


def validate_result(
    path: Path,
    validator: Draft202012Validator,
    data: dict | None = None,
) -> list[str]:
    """Validate one result JSON file.

    Args:
        path: Path to the result file (used for error messages).
        validator: Pre-built schema validator instance.
        data: Pre-parsed JSON data. If None, reads from path.

    Returns:
        List of error messages. Empty means valid.
    """
    if data is None:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            return [f"{path.name}: failed to parse: {e}"]

    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        field_path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{path.name}:{field_path}: {error.message}")
    return errors


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <result.json> [result2.json ...]")
        sys.exit(2)

    schema = _load_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    all_errors: list[str] = []
    n_valid = 0

    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.exists():
            all_errors.append(f"{arg}: file not found")
            continue

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            all_errors.append(f"{arg}: failed to parse: {e}")
            continue
        errors = validate_result(path, validator, data)
        if errors:
            all_errors.extend(errors)
        else:
            n_valid += 1
            n_findings = len(data.get("findings", []))
            print(f"  PASS: {path.name} ({n_findings} findings)")

    if all_errors:
        print(f"\n{len(all_errors)} validation error(s):")
        for e in all_errors:
            print(f"  FAIL: {e}")
        sys.exit(1)
    else:
        print(f"\nAll {n_valid} file(s) valid.")
        sys.exit(0)


if __name__ == "__main__":
    main()
