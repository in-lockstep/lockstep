"""The semantic diff is what a reviewer reads instead of thousands of lines of YAML."""

from __future__ import annotations

import pytest

from lockstep.emit import compile_spec
from lockstep.emit.semantic_diff import (
    AcknowledgementError,
    Delta,
    SemanticDiff,
    against_disk,
    diff_surfaces,
    parse_acknowledgements,
    surfaces,
)
from lockstep.emit.writer import write_plan


def test_no_deltas_when_nothing_changed(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    assert against_disk(basic_root, compile_spec(basic_root)).deltas == []


def test_widened_mcp_allow_list_is_blocking(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    guardrail = basic_root / "guardrails" / "common.md"
    guardrail.write_text(guardrail.read_text().replace("  permissions: read-all\n", ""))

    diff = against_disk(basic_root, compile_spec(basic_root))
    categories = {d.category for d in diff.blocking}
    assert "mcp-tools" in categories
    assert "create_issue" in diff.render()


def test_new_egress_host_is_blocking(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("'*.atlassian.net'", "'*.atlassian.net', '*.internal'"))

    diff = against_disk(basic_root, compile_spec(basic_root))
    assert any(d.category == "network" and d.blocking for d in diff.deltas)


def test_new_trigger_is_blocking(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    command = basic_root / "commands" / "discover.md"
    command.write_text(
        command.read_text().replace(
            "    workflow_dispatch: true", "    workflow_dispatch: true\n    push: {}"
        )
    )
    diff = against_disk(basic_root, compile_spec(basic_root))
    assert any(d.category == "triggers" and d.blocking for d in diff.deltas)


def test_pin_bumps_are_informational_not_blocking(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    pins = basic_root / ".pipeline" / "pins.lock"
    pins.write_text(pins.read_text().replace("8c44e0d2ab19f3c5d7e6b4a2091f83cc55d1e470", "a" * 40))

    diff = against_disk(basic_root, compile_spec(basic_root))
    assert any(d.category == "uses" for d in diff.deltas)
    assert not diff.blocking


def test_surface_covers_workflows_only(basic_spec_dir):
    """Prompt fragments and generated docs carry no security surface and would only add noise."""
    covered = set(surfaces(compile_spec(basic_spec_dir).files))
    assert not any("/shared/" in path for path in covered)
    assert "SECRETS.md" not in covered
    assert ".github/workflows/aw-story-extractor.md" in covered
    assert ".github/workflows/generate-tests.yml" in covered


def test_added_workflow_is_reported():
    diff = diff_surfaces({}, {"a.yml": {"permissions": None}})
    assert diff.deltas[0].category == "new-file"


# --- acknowledging a security-surface change ------------------------------------------------------
#
# The module promised deltas would block "until explicitly acknowledged" and nothing implemented
# acknowledging, so every legitimate widening was permanently red and clearable only by an admin
# bypass. A security gate nobody can clear is one people learn to bypass, which is worse than not
# having it — the same argument `publish-history` makes for not failing a run over bookkeeping.


def _diff(*categories: str, acknowledged: set[str] | None = None) -> SemanticDiff:
    return SemanticDiff(
        deltas=[Delta(category, "w.yml", "was -> now") for category in categories],
        acknowledged=acknowledged or set(),
    )


def test_a_blocking_delta_nobody_named_still_fails():
    assert [d.category for d in _diff("sandbox").unacknowledged] == ["sandbox"]


def test_naming_the_category_clears_it():
    assert _diff("sandbox", acknowledged={"sandbox"}).unacknowledged == []


def test_acknowledging_one_category_does_not_clear_another():
    """The failure this exists to prevent: one trailer waving through a second, unrelated widening."""
    diff = _diff("sandbox", "permissions", acknowledged={"sandbox"})
    assert [d.category for d in diff.unacknowledged] == ["permissions"]


def test_an_informational_delta_never_needed_acknowledging():
    assert _diff("uses").unacknowledged == []
    assert _diff("uses").blocking == []


def test_the_trailer_is_read_from_a_commit_message():
    message = "Add the meter job\n\nSome prose.\n\nSecurity-Surface: sandbox, permissions\n"
    assert parse_acknowledgements(message) == {"sandbox", "permissions"}


def test_the_trailer_key_is_case_insensitive():
    """Git trailers conventionally are, and a gate refused over capitalization is infuriating."""
    assert parse_acknowledgements("security-surface: sandbox") == {"sandbox"}


def test_categories_with_hyphens_survive_the_split():
    """`mcp-tools` and `safe-output-caps` contain the character a naive parser would split on."""
    assert parse_acknowledgements("Security-Surface: mcp-tools, safe-output-caps") == {
        "mcp-tools",
        "safe-output-caps",
    }


@pytest.mark.parametrize("blanket", ["all", "*", "any", "everything"])
def test_a_blanket_acknowledgement_is_refused(blanket):
    """A gate that can be cleared without reading it is one people clear without reading it."""
    with pytest.raises(AcknowledgementError):
        parse_acknowledgements(f"Security-Surface: {blanket}")


def test_no_trailer_acknowledges_nothing():
    assert parse_acknowledgements("Add the meter job\n\nNo trailer here.\n") == set()
    assert parse_acknowledgements("") == set()


def test_an_acknowledgement_for_a_category_that_did_not_move_is_reported():
    """An acknowledgment copied forward from an earlier change is how the trailer stops meaning
    anything, and this is the only moment anybody would notice."""
    diff = _diff("sandbox", acknowledged={"sandbox", "network"})
    assert diff.unacknowledged == []
    assert diff.stale_acknowledgements == ["network"]
