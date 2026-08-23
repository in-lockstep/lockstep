"""The extension's own tests.

`pr-feedback` used to be tested here. It moved into `pipeline-exec` when a second pipeline needed
it, and its tests moved with it — see `packages/pipeline-exec/tests/test_feedback.py`.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from issue_ext.commands import issue_fetch


def test_issue_fetch_reduces_an_issue_to_what_an_agent_needs(tmp_path):
    source = tmp_path / "raw.json"
    source.write_text(
        json.dumps(
            {
                "key": "APP-412",
                "fields": {
                    "summary": "Orders with no price fail",
                    "description": "Long description",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "orders"}],
                    "customfield_10101": "Given an item with no price\nThen the total skips it",
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "issue.json"
    result = CliRunner().invoke(
        issue_fetch, ["--issue=APP-412", f"--output={output}", f"--from-file={source}"]
    )

    assert result.exit_code == 0
    document = json.loads(output.read_text())
    assert document["key"] == "APP-412"
    assert document["components"] == ["orders"]
    assert document["acceptance_criteria"][0].startswith("Given an item")


def test_a_description_is_truncated_before_it_reaches_a_prompt(tmp_path):
    source = tmp_path / "raw.json"
    source.write_text(json.dumps({"key": "A", "fields": {"description": "x" * 20000}}), encoding="utf-8")
    output = tmp_path / "issue.json"
    CliRunner().invoke(issue_fetch, ["--issue=A", f"--output={output}", f"--from-file={source}"])
    assert len(json.loads(output.read_text())["description"]) == 12000
