"""Overlays are inputs to regeneration. An anchor that matches nothing must fail loudly."""

from __future__ import annotations

import pytest
import yaml

from lockstep.emit import compile_spec
from lockstep.emit.overlay import apply_mapping_ops, deep_merge, load_overlays, resolve
from lockstep.errors import OverlayAnchorNotFound, OverlayError

OVERLAY_FILE = "overlays/github/generate-tests.yml"


def test_overlay_merge_reaches_the_generated_job(basic_spec_dir):
    plan = compile_spec(basic_spec_dir)
    jobs = yaml.safe_load(plan.files[".github/workflows/generate-tests.yml"])["jobs"]
    assert jobs["fetch-issues"]["timeout-minutes"] == 30


def test_overlay_patches_agent_frontmatter_and_prompt(basic_spec_dir):
    text = compile_spec(basic_spec_dir).files[".github/workflows/aw-story-extractor.md"]
    assert "jira-mirror.acme.internal" in text
    assert "## Acme conventions" in text
    assert "ACME-" in text


def test_applied_overlays_are_recorded_in_provenance(basic_spec_dir):
    text = compile_spec(basic_spec_dir).files[".github/workflows/generate-tests.yml"]
    assert "# overlays: overlays/github/generate-tests.yml@" in text


def test_unmatched_anchor_names_the_nearest_candidate(basic_root):
    overlay = basic_root / OVERLAY_FILE
    overlay.write_text(overlay.read_text().replace("jobs[id=fetch-issues]", "jobs[id=fetch-issue]"))
    with pytest.raises(OverlayAnchorNotFound) as excinfo:
        compile_spec(basic_root)
    rendered = excinfo.value.render()
    assert "OVL404" in rendered
    assert "nearest: fetch-issues" in rendered


def test_overlay_targeting_an_ungenerated_file_fails(basic_root):
    overlay = basic_root / OVERLAY_FILE
    overlay.write_text(overlay.read_text().replace("workflows/generate-tests.yml", "workflows/nope.yml"))
    with pytest.raises(OverlayAnchorNotFound):
        compile_spec(basic_root)


def test_explicit_step_id_survives_a_label_rename(basic_root):
    """Anchors key on `id:`, so renaming a step's display name must not break the user's overlay."""
    command = basic_root / "commands" / "generate-tests.md"
    command.write_text(command.read_text().replace("**Fetch issues from Jira**", "**Pull the Jira backlog**"))
    jobs = yaml.safe_load(compile_spec(basic_root).files[".github/workflows/generate-tests.yml"])["jobs"]
    assert jobs["fetch-issues"]["timeout-minutes"] == 30


def test_resolve_walks_dicts_and_id_selected_lists():
    data = {"jobs": {"a": {"steps": [{"id": "one", "run": "x"}, {"id": "two", "run": "y"}]}}}
    container, key = resolve(data, "jobs[id=a].steps[id=two]", location="t")
    assert container[key]["run"] == "y"


def test_deep_merge_appends_lists_and_replaces_scalars():
    assert deep_merge({"a": [1]}, {"a": [2]}) == {"a": [1, 2]}
    assert deep_merge({"a": [1]}, {"a": [1]}) == {"a": [1]}
    assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}
    assert deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}


def test_insert_step_positions_relative_to_an_anchor():
    data = {"jobs": {"a": {"steps": [{"id": "one"}, {"id": "two"}]}}}
    apply_mapping_ops(
        data,
        [{"op": "insert-step", "at": "jobs[id=a].steps", "after": "one", "value": {"id": "mid"}}],
        location="t",
    )
    assert [s["id"] for s in data["jobs"]["a"]["steps"]] == ["one", "mid", "two"]


def test_delete_removes_a_job():
    data = {"jobs": {"a": {}, "b": {}}}
    apply_mapping_ops(data, [{"op": "delete", "at": "jobs[id=a]"}], location="t")
    assert list(data["jobs"]) == ["b"]


def test_unknown_operation_is_rejected():
    with pytest.raises(OverlayError):
        apply_mapping_ops({"jobs": {}}, [{"op": "frobnicate", "at": "jobs"}], location="t")


def test_operation_without_an_anchor_is_rejected():
    with pytest.raises(OverlayError):
        apply_mapping_ops({"jobs": {}}, [{"op": "merge", "value": {}}], location="t")


def test_overlay_documents_load_with_provenance(basic_spec_dir):
    overlays = load_overlays(basic_spec_dir)
    assert [o.target for o in overlays] == [
        "workflows/generate-tests.yml",
        "workflows/aw-story-extractor.md",
    ]
    assert all(o.sha and o.rel == OVERLAY_FILE for o in overlays)


def test_overlay_document_without_a_target_is_rejected(basic_root):
    (basic_root / OVERLAY_FILE).write_text("patches: []\n")
    with pytest.raises(OverlayError):
        compile_spec(basic_root)
