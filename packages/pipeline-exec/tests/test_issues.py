"""Reducing one GitHub issue into something an implementing agent can build from.

The interesting behaviour is where the acceptance criteria come from. A tracker keeps them in a
named field; GitHub keeps them wherever the author put them, and the two places authors actually
put them are a task list and a heading. Reading neither means an agent implements a title.
"""

from __future__ import annotations

import io
import json

import pytest
from click.testing import CliRunner
from pipeline_exec.cli import main
from pipeline_exec.issues import criteria_from, reduce_issue

BODY = """\
Totalling an order containing an item with a null price raises a TypeError.

## Acceptance criteria

- Given an item with no price
- Then the total skips it
* And a warning is logged

## Notes

- Not in scope: currency conversion
"""

TASKS = """\
Some prose.

- [ ] Reject a null price
- [x] Add the regression test
- not a task
"""

ISSUE = {
    "number": 412,
    "title": "Order total fails when an item has no price",
    "body": BODY,
    "state": "open",
    "html_url": "https://github.com/acme/web/issues/412",
    "labels": [{"name": "bug"}, {"name": "area/billing"}],
    "assignees": [{"login": "ana"}],
}
COMMENTS = [
    {"user": {"login": "bo"}, "body": "It is the `/orders/total` endpoint specifically."},
    {"user": {"login": "ana"}, "body": "Agreed."},
]


# --- where the criteria come from -------------------------------------------


def test_a_criteria_heading_is_read_as_criteria():
    assert criteria_from(BODY) == [
        "Given an item with no price",
        "Then the total skips it",
        "And a warning is logged",
    ]


def test_the_section_stops_at_the_next_heading():
    """Otherwise 'Not in scope' arrives as a requirement, which is the opposite of what it says."""
    assert "Not in scope: currency conversion" not in criteria_from(BODY)


def test_a_task_list_is_read_when_there_is_no_heading():
    assert criteria_from(TASKS) == ["Reject a null price", "Add the regression test"]


def test_a_checked_box_is_still_a_requirement():
    """In an issue being implemented, checked usually means agreed rather than already built."""
    assert "Add the regression test" in criteria_from(TASKS)


def test_a_heading_wins_over_a_task_list():
    """A task list can be anything — affected files, a rollout plan. A section was written to be this."""
    both = BODY + "\n- [ ] unrelated checklist item\n"
    assert "unrelated checklist item" not in criteria_from(both)


@pytest.mark.parametrize("heading", ["Acceptance Criteria", "AC", "Definition of done"])
def test_the_headings_people_actually_write(heading):
    assert criteria_from(f"## {heading}\n\n- One\n") == ["One"]


def test_prose_under_the_heading_is_not_dropped():
    """Somebody writes a paragraph instead of bullets; silently returning nothing is worse."""
    assert criteria_from("## Acceptance criteria\n\nThe total must skip priced-less items.\n") == [
        "The total must skip priced-less items."
    ]


def test_an_issue_with_no_criteria_says_so_by_being_empty():
    assert criteria_from("Just a sentence.") == []


# --- the document ------------------------------------------------------------


def test_the_shared_keys_match_what_an_analyst_already_reads():
    """`key`, `summary`, `description`, `acceptance_criteria` — the same four a tracker emits."""
    document = reduce_issue(ISSUE, [], repo="acme/web")
    assert document["key"] == "#412"
    assert document["summary"].startswith("Order total fails")
    assert document["description"].startswith("Totalling an order")
    assert len(document["acceptance_criteria"]) == 3


def test_labels_travel_verbatim_rather_than_becoming_a_type():
    """Mapping labels onto an issue type would be this runtime deciding what a repo's labels mean."""
    assert reduce_issue(ISSUE, [])["labels"] == ["bug", "area/billing"]


def test_labels_from_a_webhook_payload_are_strings_not_objects():
    assert reduce_issue({"number": 1, "labels": ["bug"]}, [])["labels"] == ["bug"]


def test_the_discussion_is_included_because_that_is_where_the_requirement_ends_up():
    document = reduce_issue(ISSUE, COMMENTS)
    assert document["discussion"][0]["author"] == "bo"
    assert "/orders/total" in document["discussion"][0]["body"]


def test_a_pasted_log_cannot_push_the_issue_out_of_context():
    document = reduce_issue(
        {"number": 1, "body": "x" * 50_000},
        [{"user": {"login": "a"}, "body": "y" * 50_000}] * 200,
    )
    assert len(document["description"]) == 12000
    assert len(document["discussion"]) == 30
    assert len(document["discussion"][0]["body"]) == 4000


# --- the command -------------------------------------------------------------


@pytest.fixture
def fixtures(tmp_path):
    directory = tmp_path / "gh"
    directory.mkdir()
    (directory / "issue.json").write_text(json.dumps(ISSUE), encoding="utf-8")
    (directory / "comments.json").write_text(json.dumps(COMMENTS), encoding="utf-8")
    return directory


def run(*args):
    return CliRunner().invoke(main, ["gh-issue-fetch", *args])


def test_the_command_writes_the_document(tmp_path, fixtures):
    output = tmp_path / "issue.json"
    result = run("--issue=412", "--repo=acme/web", f"--output={output}", f"--from-dir={fixtures}")
    assert result.exit_code == 0, result.output
    document = json.loads(output.read_text())
    assert document["number"] == 412
    assert document["repo"] == "acme/web"
    assert len(document["discussion"]) == 2


def test_the_counts_are_published_for_a_later_step(tmp_path, fixtures):
    result = run(
        "--issue=412", "--repo=acme/web", f"--output={tmp_path / 'i.json'}", f"--from-dir={fixtures}"
    )
    assert "number=412" in result.output
    assert "state=open" in result.output
    assert "criteria=3" in result.output


def test_the_discussion_can_be_left_out(tmp_path, fixtures):
    output = tmp_path / "issue.json"
    run(
        "--issue=412",
        "--repo=acme/web",
        f"--output={output}",
        f"--from-dir={fixtures}",
        "--no-discussion",
    )
    assert json.loads(output.read_text())["discussion"] == []


@pytest.mark.parametrize(
    "given",
    ["412", "#412", "acme/web#412", "https://github.com/acme/web/issues/412", "  412 "],
)
def test_every_way_somebody_names_an_issue(tmp_path, fixtures, given):
    result = run(
        f"--issue={given}", "--repo=acme/web", f"--output={tmp_path / 'i.json'}", f"--from-dir={fixtures}"
    )
    assert result.exit_code == 0, result.output


def test_something_that_is_not_an_issue_number_fails_loudly(tmp_path, fixtures):
    """A model handed an empty document will write something, and it will look plausible."""
    result = run(
        "--issue=the-login-one",
        "--repo=acme/web",
        f"--output={tmp_path / 'i.json'}",
        f"--from-dir={fixtures}",
    )
    assert result.exit_code == 1
    assert "cannot read an issue number" in result.output


def test_an_empty_fixture_directory_fails_rather_than_writing_an_empty_issue(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run("--issue=1", "--repo=acme/web", f"--output={tmp_path / 'i.json'}", f"--from-dir={empty}")
    assert result.exit_code == 1
    assert "no issue found" in result.output


# --- Jira, reduced to the same shape ---------------------------------------
#
# A pipeline reading `summary`, `description` and `acceptance_criteria` should not have to know which
# tracker delivered them. What differs genuinely — an issue type, a component — stays alongside
# rather than being flattened onto something GitHub-shaped.


def jira(**fields):
    base = {"summary": "s", "description": "", "status": {"name": "Open"}}
    base.update(fields)
    return {"key": "PLAT-1", "self": "https://jira.example/rest/api/2/issue/1", "fields": base}


def test_a_jira_issue_reduces_to_the_github_shape():
    from pipeline_exec.issues import reduce_issue, reduce_jira_issue

    shared = {"key", "summary", "description", "acceptance_criteria", "labels", "assignees", "state"}
    github = set(reduce_issue({"number": 1, "title": "t", "body": ""}, []))
    assert shared <= set(reduce_jira_issue(jira())) & github


def test_a_configured_criteria_field_wins():
    from pipeline_exec.issues import jira_criteria

    fields = {
        "customfield_500": "Given a user\nWhen they log in",
        "description": "## Acceptance criteria\n- other",
    }
    criteria, source = jira_criteria(fields, field_id="customfield_500")
    assert criteria == ["Given a user", "When they log in"]
    assert source == "field customfield_500"


def test_a_configured_field_that_is_empty_does_not_send_it_guessing():
    """Somebody said where these live. The answer is that this issue has none."""
    from pipeline_exec.issues import jira_criteria

    criteria, source = jira_criteria(
        {"customfield_500": "", "description": "## Acceptance criteria\n- something"},
        field_id="customfield_500",
    )
    assert criteria == []
    assert "empty" in source


def test_an_unconfigured_instance_guesses_and_says_that_it_guessed():
    from pipeline_exec.issues import jira_criteria

    criteria, source = jira_criteria({"customfield_733": "Given a cart\nWhen it is empty"})
    assert criteria == ["Given a cart", "When it is empty"]
    assert source == "guessed from customfield_733"


def test_falling_back_to_the_description_uses_the_same_parser_as_github():
    from pipeline_exec.issues import jira_criteria

    criteria, source = jira_criteria({"description": "## Acceptance criteria\n- Returns 400\n- Logs it"})
    assert criteria == ["Returns 400", "Logs it"]
    assert source == "description"


def test_no_criteria_anywhere_says_none_rather_than_guessing():
    from pipeline_exec.issues import jira_criteria

    assert jira_criteria({"description": "It is broken."}) == ([], "none")


def test_tracker_native_fields_are_kept_rather_than_mapped():
    """A Jira issue type is a real thing and is not a GitHub label."""
    from pipeline_exec.issues import reduce_jira_issue

    document = reduce_jira_issue(
        jira(
            issuetype={"name": "Bug"},
            components=[{"name": "reports"}],
            priority={"name": "High"},
            labels=["billing"],
            assignee={"displayName": "Dana Ruiz"},
        )
    )
    assert document["type"] == "Bug"
    assert document["components"] == ["reports"]
    assert document["priority"] == "High"
    assert document["labels"] == ["billing"]
    assert document["assignees"] == ["Dana Ruiz"]


def test_the_command_reads_either_tracker_through_one_flag(tmp_path):
    import json as _json

    from click.testing import CliRunner
    from pipeline_exec.cli import main

    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "issue.json").write_text(_json.dumps(jira(description="## Acceptance criteria\n- Works")))
    out = tmp_path / "issue.json"
    result = CliRunner().invoke(
        main, ["issue-fetch", "--source=jira", "--issue=PLAT-1", f"--from-dir={fixtures}", f"--output={out}"]
    )
    assert result.exit_code == 0, result.output
    assert "criteria_source=description" in result.output
    assert _json.loads(out.read_text())["acceptance_criteria"] == ["Works"]


def test_an_issue_that_is_not_there_is_an_error_not_an_empty_document(tmp_path):
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    empty = tmp_path / "fx"
    empty.mkdir()
    result = CliRunner().invoke(
        main,
        [
            "issue-fetch",
            "--source=jira",
            "--issue=NOPE-1",
            f"--from-dir={empty}",
            f"--output={tmp_path / 'o.json'}",
        ],
    )
    assert result.exit_code != 0
    assert "no issue found" in result.output


def test_jira_without_credentials_says_which_ones(tmp_path):
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    result = CliRunner().invoke(
        main, ["issue-fetch", "--source=jira", "--issue=P-1", f"--output={tmp_path / 'o.json'}"], env={}
    )
    assert result.exit_code != 0
    assert "JIRA_BASE_URL" in result.output


# --- writing back to Jira ---------------------------------------------------
#
# On GitHub an agent's conclusions reach the issue through gh-aw's safe outputs: the agent emits a
# request and machinery it does not control performs it. Jira has no equivalent, so this reproduces
# the shape — the agent writes a file, a deterministic step writes to the tracker — and the rules
# the safe-output caps enforce on the other side have to be enforced here in code.


def test_labels_are_added_never_replaced():
    """The single most destructive thing a write-back could do, and the easiest by accident.

    `fields.labels` would replace the list and silently delete whatever a person put on the issue.
    """
    from pipeline_exec.issues import label_update

    payload = label_update(["reporting", "triaged"])
    assert payload == {"update": {"labels": [{"add": "reporting"}, {"add": "triaged"}]}}
    assert "fields" not in payload


def test_more_labels_than_the_cap_is_refused():
    """A model that decided on forty labels has misunderstood the task."""
    from pipeline_exec.issues import JiraWriteError, label_update

    with pytest.raises(JiraWriteError, match="cap of 5"):
        label_update([f"l{n}" for n in range(6)])


def test_a_label_with_a_space_is_refused_here_rather_than_by_a_400():
    from pipeline_exec.issues import JiraWriteError, label_update

    with pytest.raises(JiraWriteError, match="cannot contain spaces"):
        label_update(["needs triage"])


def test_setting_a_priority_is_a_field_edit_not_a_transition():
    from pipeline_exec.issues import priority_update

    assert priority_update("High") == {"fields": {"priority": {"name": "High"}}}


def test_a_comment_carries_a_marker_so_the_next_run_can_find_it():
    from pipeline_exec.issues import find_marked_comment, with_marker

    body = with_marker("Placed as a bug.", name="triage")
    assert body.endswith("[lockstep:triage]")
    found = find_marked_comment([{"id": "1", "body": body}], name="triage")
    assert found is not None and found["id"] == "1"


def test_the_marker_is_not_added_twice_when_a_comment_is_revised():
    from pipeline_exec.issues import with_marker

    once = with_marker("Placed as a bug.", name="triage")
    assert with_marker(once, name="triage").count("[lockstep:triage]") == 1


def test_another_pipelines_comment_is_not_mistaken_for_this_one():
    """Editing somebody else's comment is the kind of thing a bot only has to do once."""
    from pipeline_exec.issues import find_marked_comment, with_marker

    theirs = with_marker("A review.", name="review")
    assert find_marked_comment([{"id": "1", "body": theirs}], name="triage") is None
    assert find_marked_comment([{"id": "1", "body": "A human wrote this."}], name="triage") is None


def test_the_latest_marked_comment_wins():
    from pipeline_exec.issues import find_marked_comment, with_marker

    comments = [
        {"id": "1", "body": with_marker("old", name="triage")},
        {"id": "2", "body": with_marker("new", name="triage")},
    ]
    assert find_marked_comment(comments, name="triage")["id"] == "2"


def triage_result(**overrides):
    base = {"kind": "bug", "priority": "High", "comment": "Placed as a bug.", "labels": ["reporting"]}
    base.update(overrides)
    return base


def run_update(tmp_path, result, *flags):
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    path = tmp_path / "triage.json"
    path.write_text(json.dumps(result))
    return CliRunner().invoke(main, ["jira-update", "--issue=P-1", f"--from={path}", "--dry-run", *flags])


def test_nothing_is_written_unless_something_was_asked_for(tmp_path):
    result = run_update(tmp_path, triage_result())
    assert result.exit_code != 0
    assert "nothing asked for" in result.output


def test_only_what_was_asked_for_is_written(tmp_path):
    result = run_update(tmp_path, triage_result(), "--labels")
    assert result.exit_code == 0
    assert "labels" in result.output
    assert "priority" not in result.output


def test_an_agent_that_wrote_nothing_is_an_error_not_a_silent_pass(tmp_path):
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    result = CliRunner().invoke(
        main, ["jira-update", "--issue=P-1", f"--from={tmp_path / 'nope.json'}", "--comment", "--dry-run"]
    )
    assert result.exit_code != 0
    assert "wrote nothing to write back" in result.output


def test_an_empty_comment_is_not_posted(tmp_path):
    result = run_update(tmp_path, triage_result(comment="   "), "--comment")
    assert result.exit_code == 0
    assert "nothing to write" in result.output


def test_a_dry_run_says_it_would_write_rather_than_that_it_did(tmp_path):
    result = run_update(tmp_path, triage_result(), "--comment", "--labels")
    assert "would write" in result.output


def test_credentials_are_required_for_a_real_write(tmp_path):
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    path = tmp_path / "t.json"
    path.write_text(json.dumps(triage_result()))
    result = CliRunner().invoke(
        main, ["jira-update", "--issue=P-1", f"--from={path}", "--comment"], env={"JIRA_API_TOKEN": ""}
    )
    assert result.exit_code != 0
    assert "JIRA_BASE_URL" in result.output


def test_the_fetch_says_what_it_left_outstanding(tmp_path):
    """The write-back step gates on this, so it has to answer for the tracker that actually ran."""
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "issue.json").write_text(json.dumps(jira()))
    out = tmp_path / "issue.json"
    result = CliRunner().invoke(
        main, ["issue-fetch", "--source=jira", "--issue=P-1", f"--from-dir={fixtures}", f"--output={out}"]
    )
    assert 'writeback=["jira"]' in result.output


def test_github_leaves_nothing_outstanding(tmp_path):
    """Safe outputs already did it; a second write would be a duplicate comment."""
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "issue.json").write_text(json.dumps({"number": 5, "title": "t", "body": "", "state": "open"}))
    result = CliRunner().invoke(
        main,
        [
            "issue-fetch",
            "--source=github",
            "--issue=5",
            "--repo=o/r",
            f"--from-dir={fixtures}",
            f"--output={tmp_path / 'o.json'}",
        ],
    )
    assert "writeback=[]" in result.output


# --- talking to Jira --------------------------------------------------------


class FakeResponse:
    def __init__(self, body=""):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def stub_jira(monkeypatch, handler):
    """Stand in for the network, recording what would have been sent."""
    import urllib.request

    sent = []

    def fake_urlopen(request, timeout=0):
        sent.append((request.get_method(), request.full_url, request.data))
        return handler(request)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent


def test_a_first_comment_is_posted(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from pipeline_exec.cli import main

    sent = stub_jira(monkeypatch, lambda request: FakeResponse('{"comments": []}'))
    path = tmp_path / "t.json"
    path.write_text(json.dumps(triage_result()))
    result = CliRunner().invoke(
        main,
        ["jira-update", "--issue=P-1", f"--from={path}", "--comment", "--base-url=https://jira.test"],
        env={"JIRA_API_TOKEN": "tok"},
    )
    assert result.exit_code == 0, result.output
    methods = [(method, url.rsplit("/rest/api/2/", 1)[-1]) for method, url, _ in sent]
    assert ("GET", "issue/P-1/comment") in methods
    assert ("POST", "issue/P-1/comment") in methods
    assert "wrote comment" in result.output


def test_a_second_run_revises_the_comment_it_left(tmp_path, monkeypatch):
    """Otherwise every run adds another identical comment to the issue."""
    from click.testing import CliRunner
    from pipeline_exec.cli import main
    from pipeline_exec.issues import with_marker

    existing = json.dumps({"comments": [{"id": "77", "body": with_marker("old", name="triage")}]})
    sent = stub_jira(monkeypatch, lambda request: FakeResponse(existing))
    path = tmp_path / "t.json"
    path.write_text(json.dumps(triage_result()))
    result = CliRunner().invoke(
        main,
        [
            "jira-update",
            "--issue=P-1",
            f"--from={path}",
            "--name=triage",
            "--comment",
            "--base-url=https://jira.test",
        ],
        env={"JIRA_API_TOKEN": "tok"},
    )
    assert result.exit_code == 0, result.output
    assert any(method == "PUT" and url.endswith("issue/P-1/comment/77") for method, url, _ in sent)
    assert "revised comment" in result.output


def test_a_rejected_write_reports_what_jira_said(tmp_path, monkeypatch):
    """The status alone names nothing; Jira puts the offending field in the body."""
    import urllib.error

    from click.testing import CliRunner
    from pipeline_exec.cli import main

    def reject(request):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"errors":{"labels":"not valid"}}')
        )

    stub_jira(monkeypatch, reject)
    path = tmp_path / "t.json"
    path.write_text(json.dumps(triage_result()))
    result = CliRunner().invoke(
        main,
        ["jira-update", "--issue=P-1", f"--from={path}", "--labels", "--base-url=https://jira.test"],
        env={"JIRA_API_TOKEN": "tok"},
    )
    assert result.exit_code != 0
    assert "not valid" in result.output


def test_unreadable_comments_mean_a_new_one_not_a_failed_run(tmp_path, monkeypatch):
    """A duplicate comment is a worse outcome than none, but it is not worth failing the run over."""
    import urllib.error

    from click.testing import CliRunner
    from pipeline_exec.cli import main

    def flaky(request):
        if request.get_method() == "GET":
            raise urllib.error.URLError("no route to host")
        return FakeResponse("{}")

    sent = stub_jira(monkeypatch, flaky)
    path = tmp_path / "t.json"
    path.write_text(json.dumps(triage_result()))
    result = CliRunner().invoke(
        main,
        ["jira-update", "--issue=P-1", f"--from={path}", "--comment", "--base-url=https://jira.test"],
        env={"JIRA_API_TOKEN": "tok"},
    )
    assert result.exit_code == 0, result.output
    assert any(method == "POST" for method, _, _ in sent)
