"""Finding representation, validation, and fingerprinting."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FindingKey:
    """Stable identity for a finding across rounds."""

    file: str
    issue: str

    @classmethod
    def from_dict(cls, f: dict) -> FindingKey:
        return cls(file=f.get("file", ""), issue=f.get("issue", ""))


def validate_result(
    d: object, i: int, expected_reviewers: dict[int, str]
) -> dict | None:
    """Validate result dict has required keys and a findings list. Pin reviewer ID."""
    if not isinstance(d, dict):
        return None
    required = {"reviewer", "model", "findings", "overall_assessment"}
    if not required.issubset(d.keys()):
        return None
    d["reviewer"] = expected_reviewers[i]
    if not isinstance(d["findings"], list):
        return None
    if not all(isinstance(f, dict) for f in d["findings"]):
        return None
    return d


def collect_blocking_findings(results: list[dict | None]) -> list[dict]:
    """Return all blocking findings (explicit blocking=true or critical severity)."""
    blocking = []
    for r in results:
        if r is None:
            continue
        for f in r.get("findings", []):
            if not isinstance(f, dict):
                continue
            if f.get("blocking", False) or f.get("severity") == "critical":
                blocking.append({**f, "_reviewer": r.get("reviewer", "?")})
    return blocking


def fingerprint(findings: list[dict]) -> set[FindingKey]:
    """Stable fingerprint of a set of findings."""
    return {FindingKey.from_dict(f) for f in findings}
