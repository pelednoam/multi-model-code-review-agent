"""Shared by every test file."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _agy_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """agy is on by default wherever it is installed, and switching it on runs the real
    agy: its sandbox check, and ``agy models`` over the network. Tests that fake
    ``shutil.which`` would otherwise reach a real agy on any machine that has one;
    tests/test_agy.py switches it back on where that is the point."""
    monkeypatch.setenv("REVIEW_USE_AGY", "0")
