"""This script decides what gets tested, so a regression in it silently shrinks coverage."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "list_endpoints", Path(__file__).parent.parent / "scripts" / "list-endpoints.py"
)
assert spec and spec.loader
list_endpoints = importlib.util.module_from_spec(spec)
spec.loader.exec_module(list_endpoints)


def test_every_endpoint_carries_the_key_the_matrix_fans_out_on():
    assert all("key" in endpoint for endpoint in list_endpoints.ENDPOINTS)


def test_keys_are_unique():
    keys = [endpoint["key"] for endpoint in list_endpoints.ENDPOINTS]
    assert len(set(keys)) == len(keys)


def test_every_endpoint_tells_the_agent_what_correct_looks_like():
    """The agent has no tools; everything it needs must be in the item."""
    for endpoint in list_endpoints.ENDPOINTS:
        assert endpoint["describes"]
        assert endpoint["expects"]
        assert endpoint["method"]


def test_selecting_narrows_the_surface():
    assert [e["key"] for e in list_endpoints.select("uuid")] == ["uuid"]
    assert [e["key"] for e in list_endpoints.select("uuid, headers")] == ["uuid", "headers"]


def test_selecting_nothing_returns_everything():
    assert list_endpoints.select("") == list_endpoints.ENDPOINTS


def test_an_unknown_key_selects_nothing():
    assert list_endpoints.select("no-such-endpoint") == []
