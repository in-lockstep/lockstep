"""Deciding whether anything moved. The wrong answer either way is expensive."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("repin", Path(__file__).parent.parent / "scripts" / "repin.py")
repin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repin)


def lock(tmp_path, inherits):
    path = tmp_path / "pins.lock"
    path.write_text(json.dumps({"inherits": inherits}), encoding="utf-8")
    return path


def test_nothing_moved_when_every_commit_is_the_same():
    before = {"standards": "aaa", "review": "bbb"}
    assert repin.moved(before, dict(before)) == []


def test_a_changed_commit_is_reported():
    assert repin.moved({"standards": "aaa"}, {"standards": "ccc"}) == ["standards"]


def test_a_newly_declared_upstream_counts_as_moved():
    """Nothing was pinned before, so there is a recompile to propose."""
    assert repin.moved({}, {"review": "bbb"}) == ["review"]


def test_an_unresolved_upstream_is_not_reported_as_moved():
    """An empty commit means `pin` could not reach it — a failure, not a new version."""
    assert repin.moved({"standards": "aaa"}, {"standards": ""}) == []


def test_the_report_is_sorted_so_the_output_is_stable():
    before = {"a": "1", "b": "1", "c": "1"}
    assert repin.moved(before, {"a": "2", "b": "2", "c": "2"}) == ["a", "b", "c"]


def test_a_missing_lock_reads_as_nothing_pinned(tmp_path):
    assert repin.recorded(tmp_path / "absent.lock") == {}


def test_a_corrupt_lock_reads_as_nothing_pinned(tmp_path):
    path = tmp_path / "pins.lock"
    path.write_text("{not json", encoding="utf-8")
    assert repin.recorded(path) == {}


def test_the_lock_is_read_by_commit_not_by_ref(tmp_path):
    """A ref that was retagged onto a different commit is exactly what this has to notice."""
    path = lock(tmp_path, {"standards": {"repo": "acme/s", "ref": "v3", "sha": "aaa"}})
    assert repin.recorded(path) == {"standards": "aaa"}
