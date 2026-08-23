"""What reaches a pull request is decided here, so it is decided in code and tested."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "assemble_fixes", Path(__file__).parent.parent / "scripts" / "assemble-fixes.py"
)
assert spec and spec.loader
assemble = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assemble)

REVIEW = {
    "fixes": [
        {"key": "APP-1", "verdict": "approve", "summary": "Guard against a null price"},
        {"key": "APP-2", "verdict": "reject", "summary": "Suppresses the symptom"},
        {"key": "APP-3", "summary": "Reviewer said nothing"},
    ]
}


def test_only_explicitly_approved_fixes_are_carried():
    """Silence is not approval; a fix the reviewer did not judge must not reach a pull request."""
    assert [entry["key"] for entry in assemble.approved(REVIEW)] == ["APP-1"]


def test_a_rejected_fix_is_not_carried():
    assert "APP-2" not in [entry["key"] for entry in assemble.approved(REVIEW)]


def test_an_empty_review_carries_nothing():
    assert assemble.approved({"fixes": []}) == []
    assert assemble.approved({}) == []
