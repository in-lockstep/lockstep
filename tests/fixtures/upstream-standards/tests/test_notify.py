"""The fan-out. It says "go look" and nothing else."""

from __future__ import annotations

from pathlib import Path

import yaml

SOURCE = (Path(__file__).parent.parent / ".github" / "workflows" / "notify-consumers.yml").read_text()
WORKFLOW = yaml.safe_load(SOURCE)


def test_nothing_fires_until_this_repositorys_own_checks_pass():
    triggers = WORKFLOW.get("on") or WORKFLOW.get(True)
    assert triggers["workflow_run"]["workflows"] == ["pipeline-ci"]
    assert "success" in WORKFLOW["jobs"]["dispatch"]["if"]


def test_it_only_fires_from_the_default_branch():
    triggers = WORKFLOW.get("on") or WORKFLOW.get(True)
    assert triggers["workflow_run"]["branches"] == ["main"]


def test_the_signal_carries_no_payload():
    """A payload naming a ref would be a payload that can redirect a consumer."""
    assert "client_payload" not in SOURCE
    assert "-f event_type=upstream-moved" in SOURCE


def test_the_consumer_list_is_the_apps_installations():
    """A second list in a file here would poke repositories that uninstalled and miss new ones."""
    assert "/installation/repositories" in SOURCE


def test_one_unreachable_consumer_does_not_stop_the_rest():
    assert "::warning::could not notify" in SOURCE


def test_this_workflow_holds_no_write_permission_of_its_own():
    assert WORKFLOW["permissions"] == {"contents": "read"}
