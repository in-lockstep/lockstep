"""Item loading, keying, coverage and slicing."""

from __future__ import annotations

import json

import pytest
from pipeline_exec.errors import ExecError, TooManyItems
from pipeline_exec.items import (
    as_shards,
    covered,
    enforce_cap,
    load_items,
    shard_of,
)


def write(tmp_path, data, name="items.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_loads_an_array_of_objects(tmp_path):
    items = load_items(write(tmp_path, [{"key": "A-1"}, {"key": "A-2"}]), "key")
    assert [item.key for item in items] == ["A-1", "A-2"]


def test_loads_an_array_of_scalars(tmp_path):
    items = load_items(write(tmp_path, ["one", "two"]), "key")
    assert [item.key for item in items] == ["one", "two"]
    assert items[0].value == {"key": "one"}


def test_unwraps_an_object_holding_one_array(tmp_path):
    items = load_items(write(tmp_path, {"total": 2, "issues": [{"key": "A"}, {"key": "B"}]}), "key")
    assert [item.key for item in items] == ["A", "B"]


def test_an_object_with_two_arrays_is_ambiguous(tmp_path):
    with pytest.raises(ExecError) as excinfo:
        load_items(write(tmp_path, {"a": [1], "b": [2]}), "key")
    assert "exactly one" in str(excinfo.value)


def test_keys_are_made_safe_for_file_names(tmp_path):
    items = load_items(write(tmp_path, [{"key": "feature/ABC 123"}]), "key")
    assert items[0].key == "feature-ABC-123"


def test_duplicate_keys_are_disambiguated(tmp_path):
    """Keys become file names; a silent collision would lose one item's output."""
    items = load_items(write(tmp_path, [{"key": "A"}, {"key": "A"}]), "key")
    assert [item.key for item in items] == ["A", "A-2"]


def test_missing_key_field_falls_back_to_position(tmp_path):
    items = load_items(write(tmp_path, [{"name": "x"}]), "key")
    assert items[0].key == "0"
    assert items[0].value["key"] == "0"


def test_a_custom_key_field_is_honoured(tmp_path):
    items = load_items(write(tmp_path, [{"storyId": "S1"}]), "storyId")
    assert items[0].key == "S1"


def test_missing_input_is_an_error(tmp_path):
    with pytest.raises(ExecError):
        load_items(tmp_path / "nope.json", "key")


def test_malformed_json_is_an_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ExecError) as excinfo:
        load_items(path, "key")
    assert "not valid JSON" in str(excinfo.value)


def test_coverage_detects_existing_outputs(tmp_path):
    items = load_items(write(tmp_path, [{"key": "A"}, {"key": "B"}]), "key")
    out = tmp_path / "out"
    out.mkdir()
    (out / "A.json").write_text("{}", encoding="utf-8")
    assert covered(items, out, "{key}.json") == {"A"}


def test_coverage_is_empty_without_an_output_dir(tmp_path):
    items = load_items(write(tmp_path, [{"key": "A"}]), "key")
    assert covered(items, None, "{key}.json") == set()


def test_the_matrix_cap_is_enforced_before_the_run_starts():
    with pytest.raises(TooManyItems) as excinfo:
        enforce_cap(300, 256)
    assert "shard the step" in str(excinfo.value)


def test_shards_partition_every_item_exactly_once(tmp_path):
    items = load_items(write(tmp_path, [{"key": f"K{n}"} for n in range(10)]), "key")
    slices = [shard_of(items, index, 3) for index in range(3)]
    assert sorted(item.key for group in slices for item in group) == sorted(i.key for i in items)
    assert sum(len(group) for group in slices) == 10


def test_shard_slicing_is_round_robin_not_contiguous(tmp_path):
    """Round-robin spreads work; contiguous slicing clusters slow items into one shard."""
    items = load_items(write(tmp_path, [{"key": f"K{n}"} for n in range(6)]), "key")
    assert [item.key for item in shard_of(items, 0, 3)] == ["K0", "K3"]


def test_shard_descriptors_carry_their_position():
    assert as_shards(2) == [
        {"shard": 0, "shards": 2, "key": "shard-0"},
        {"shard": 1, "shards": 2, "key": "shard-1"},
    ]
