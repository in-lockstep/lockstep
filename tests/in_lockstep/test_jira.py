"""The Jira TicketSource, exercised against a mock transport rather than a live site.

Jira is the system of record most adopting orgs use, so the point of these is that the REST
shapes map onto the framework's tracker-agnostic `Ticket` correctly — status categories to
`TicketState`, issue types to `TicketType`, a description's task list to acceptance criteria —
and that the write paths send the payloads Jira expects.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from in_lockstep.core.ports import Unsupported
from in_lockstep.platform.conformance import assert_ticket_source
from in_lockstep.platform.tickets import JiraSource, TicketDraft, TicketState, TicketType

BASE = "https://acme.atlassian.net"


def _issue(key: str = "PROJ-1", *, status="In Progress", category="indeterminate", itype="Bug") -> dict:
    return {
        "key": key,
        "self": f"{BASE}/rest/api/2/issue/10001",
        "fields": {
            "summary": "Checkout 500s",
            "description": "It is broken.\n\n## Acceptance criteria\n- [ ] returns 200\n- [ ] logs id",
            "status": {"name": status, "statusCategory": {"key": category}},
            "issuetype": {"name": itype},
            "labels": ["prod", "payments"],
            "assignee": {"displayName": "Dana"},
            "comment": {"comments": [{"body": "confirmed on prod"}]},
            "fixVersions": [{"name": "2.1.0"}],
            "versions": [{"name": "2.0.0"}],
        },
    }


def _source(handler, **kwargs) -> JiraSource:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return JiraSource(base_url=BASE, client=client, **kwargs)


def test_get_maps_a_jira_issue_onto_the_framework_ticket() -> None:
    src = _source(lambda req: httpx.Response(200, json=_issue()))
    ticket = asyncio.run(src.get("PROJ-1"))
    assert ticket.key == "PROJ-1" and ticket.title == "Checkout 500s"
    assert ticket.state is TicketState.IN_PROGRESS, "the status category, not the site-specific name"
    assert ticket.raw_state == "In Progress"
    assert ticket.type is TicketType.BUG
    assert ticket.acceptance_criteria == ("returns 200", "logs id")
    assert ticket.labels == ("prod", "payments")
    assert ticket.assignees == ("Dana",)
    assert ticket.comments == ("confirmed on prod",)
    assert ticket.fix_versions == ("2.1.0",) and ticket.affects_versions == ("2.0.0",)
    assert ticket.url == f"{BASE}/browse/PROJ-1", "the human browse URL, not the REST self link"


@pytest.mark.parametrize(
    "category,state",
    [("new", TicketState.OPEN), ("indeterminate", TicketState.IN_PROGRESS), ("done", TicketState.DONE)],
)
def test_status_categories_are_the_cross_site_invariant(category, state) -> None:
    src = _source(lambda req: httpx.Response(200, json=_issue(category=category)))
    assert asyncio.run(src.get("PROJ-1")).state is state


def test_create_posts_the_fields_and_reads_the_issue_back() -> None:
    posted = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/rest/api/2/issue":
            posted.update(json.loads(req.content))
            return httpx.Response(201, json={"key": "PROJ-7"})
        return httpx.Response(200, json=_issue("PROJ-7"))

    src = _source(handler, project="PROJ")
    ticket = asyncio.run(src.create(TicketDraft(title="new bug", type=TicketType.BUG, labels=("triaged",))))
    assert ticket.key == "PROJ-7"
    assert posted["fields"]["project"] == {"key": "PROJ"}
    assert posted["fields"]["issuetype"] == {"name": "Bug"}
    assert posted["fields"]["labels"] == ["triaged"]


def test_create_without_a_project_is_unsupported() -> None:
    src = _source(lambda req: httpx.Response(200, json={}))
    with pytest.raises(Unsupported, match="project"):
        asyncio.run(src.create(TicketDraft(title="x")))


def test_search_runs_jql_and_maps_each_issue() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"issues": [_issue("PROJ-1"), _issue("PROJ-2")]})

    # Data Center (no email) uses the classic endpoint.
    src = _source(handler)
    found = asyncio.run(src.search("project = PROJ AND status = Done", limit=5))
    assert [t.key for t in found] == ["PROJ-1", "PROJ-2"]
    assert seen["path"] == "/rest/api/2/search"
    assert seen["body"]["jql"] == "project = PROJ AND status = Done"
    assert seen["body"]["maxResults"] == 5


def test_search_on_cloud_uses_the_enhanced_jql_endpoint() -> None:
    """Cloud removed the unbounded /search; an email (Cloud) routes to /search/jql."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json={"issues": []})

    src = _source(lambda r: None, email="me@acme.com")  # client injected below
    src.client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    asyncio.run(src.search("project = PROJ"))
    assert seen["path"] == "/rest/api/2/search/jql"


def test_a_custom_acceptance_field_is_requested_and_read() -> None:
    """The `fields` allow-list must name the custom field, or Jira omits it and the feature is
    dead. And when present, criteria come from it, not the description."""
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["fields"] = req.url.params.get("fields", "")
        issue = _issue()
        issue["fields"]["customfield_10101"] = "- [ ] returns 201\n- [ ] emits an event"
        issue["fields"]["description"] = "plain prose, no criteria"
        return httpx.Response(200, json=issue)

    src = _source(handler, acceptance_field="customfield_10101")
    ticket = asyncio.run(src.get("PROJ-1"))
    assert "customfield_10101" in seen["fields"], "the custom field must be requested"
    assert ticket.acceptance_criteria == ("returns 201", "emits an event")


def test_add_labels_sends_a_jira_update_op() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(204)

    src = _source(handler)
    asyncio.run(
        src.add_labels(
            asyncio.run(_source(lambda r: httpx.Response(200, json=_issue())).get("PROJ-1")), "a", "b"
        )
    )
    assert seen["body"] == {"update": {"labels": [{"add": "a"}, {"add": "b"}]}}


def test_transition_matches_the_target_status_category() -> None:
    posted = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/transitions"):
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "11", "name": "Start", "to": {"statusCategory": {"key": "indeterminate"}}},
                        {"id": "31", "name": "Done", "to": {"statusCategory": {"key": "done"}}},
                    ]
                },
            )
        posted.update(json.loads(req.content))
        return httpx.Response(204)

    src = _source(handler)
    ticket = asyncio.run(_source(lambda r: httpx.Response(200, json=_issue())).get("PROJ-1"))
    asyncio.run(src.transition(ticket, TicketState.DONE))
    assert posted == {"transition": {"id": "31"}}


def test_transition_matches_a_raw_name_when_given() -> None:
    posted = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/transitions") and req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {"id": "42", "name": "In Review", "to": {"statusCategory": {"key": "indeterminate"}}}
                    ]
                },
            )
        posted.update(json.loads(req.content))
        return httpx.Response(204)

    src = _source(handler)
    ticket = asyncio.run(_source(lambda r: httpx.Response(200, json=_issue())).get("PROJ-1"))
    asyncio.run(src.transition(ticket, TicketState.OTHER, raw="In Review"))
    assert posted == {"transition": {"id": "42"}}


def test_closed_matches_a_done_category_transition_like_github() -> None:
    """GitHub maps a closed issue to CLOSED, so a tracker-agnostic close(CLOSED) must find a
    done-category transition rather than being refused where DONE would have matched."""
    posted = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/transitions"):
            return httpx.Response(
                200,
                json={
                    "transitions": [{"id": "31", "name": "Close", "to": {"statusCategory": {"key": "done"}}}]
                },
            )
        posted.update(json.loads(req.content))
        return httpx.Response(204)

    src = _source(handler)
    ticket = asyncio.run(_source(lambda r: httpx.Response(200, json=_issue())).get("PROJ-1"))
    asyncio.run(src.transition(ticket, TicketState.CLOSED))
    assert posted == {"transition": {"id": "31"}}


def test_a_raw_state_read_back_matches_the_target_status_name() -> None:
    """The transition edge is named differently from the status it lands on; a caller feeding back
    a `raw_state` (a status name) must still match."""
    posted = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/transitions"):
            return httpx.Response(
                200,
                json={
                    "transitions": [
                        {
                            "id": "5",
                            "name": "Submit for review",
                            "to": {"name": "In Review", "statusCategory": {"key": "indeterminate"}},
                        }
                    ]
                },
            )
        posted.update(json.loads(req.content))
        return httpx.Response(204)

    src = _source(handler)
    ticket = asyncio.run(_source(lambda r: httpx.Response(200, json=_issue())).get("PROJ-1"))
    asyncio.run(src.transition(ticket, TicketState.OTHER, raw="In Review"))
    assert posted == {"transition": {"id": "5"}}, "matched the target status name, not just the edge name"


def test_transition_with_no_available_move_is_unsupported() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "transitions": [
                    {"id": "1", "name": "Start", "to": {"statusCategory": {"key": "indeterminate"}}}
                ]
            },
        )

    src = _source(handler)
    ticket = asyncio.run(_source(lambda r: httpx.Response(200, json=_issue())).get("PROJ-1"))
    with pytest.raises(Unsupported, match="no Jira transition"):
        asyncio.run(src.transition(ticket, TicketState.DONE))


def test_an_http_error_names_jiras_body_not_the_token() -> None:
    src = _source(
        lambda req: httpx.Response(403, text='{"errorMessages":["no permission"]}'), token="secret-pat"
    )
    with pytest.raises(RuntimeError) as e:
        asyncio.run(src.get("PROJ-1"))
    assert "403" in str(e.value) and "no permission" in str(e.value)
    assert "secret-pat" not in str(e.value)


def test_a_transport_error_becomes_a_runtime_error_the_cli_can_catch() -> None:
    """A connection refused / DNS / timeout is not an httpx status error and would escape as a
    raw traceback; converting it to RuntimeError is what lets _load_ticket's guard turn it into a
    clean message, and comment's best-effort guard swallow it."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("host unreachable")

    from in_lockstep.platform.tickets import Ticket

    src = _source(handler)
    with pytest.raises(RuntimeError, match="ConnectError"):
        asyncio.run(src.get("PROJ-1"))
    # comment is best-effort: the transport error must not propagate out of it.
    asyncio.run(src.comment(Ticket(key="PROJ-1", title="t"), "hi"))  # no exception


def test_cloud_uses_basic_auth_and_data_center_uses_a_bearer() -> None:
    """The email selects which: set for Cloud basic, empty for a Data Center PAT."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization", "")
        return httpx.Response(200, json=_issue())

    # No injected client, so the adapter builds its own from the auth fields.
    cloud = JiraSource(base_url=BASE, email="me@acme.com", token="tok")
    cloud.client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=BASE, auth=("me@acme.com", "tok")
    )
    asyncio.run(cloud.get("PROJ-1"))
    assert seen["auth"].startswith("Basic ")

    dc = JiraSource(base_url=BASE, token="pat")
    dc.client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=BASE, headers={"Authorization": "Bearer pat"}
    )
    asyncio.run(dc.get("PROJ-1"))
    assert seen["auth"] == "Bearer pat"


def test_the_client_is_built_from_the_auth_fields_when_none_is_injected() -> None:
    """The construction path the mock tests skip: an absent client is built from base_url and the
    email/token, with the token in a header, never in the base URL."""
    cloud = JiraSource(base_url=f"{BASE}/", email="a@b.com", token="tok")._http()
    assert cloud.auth is not None and str(cloud.base_url).rstrip("/") == BASE

    dc = JiraSource(base_url="https://jira.local", token="pat")._http()
    assert dc.headers.get("Authorization") == "Bearer pat"


def test_jira_source_satisfies_the_conformance_kit() -> None:
    assert_ticket_source(JiraSource(base_url=BASE))


def test_the_cli_routes_a_ticket_key_through_a_bound_source() -> None:
    """`implement --ticket PROJ-123` reaches Jira when the module binds JiraSource, and falls back
    to GitHub when it binds nothing — the wiring that makes the adapter usable, not just present."""
    from in_lockstep.cli import _bound_ticket_source, _load_ticket
    from in_lockstep.lockstep import Lockstep
    from in_lockstep.platform.tickets import TicketSource

    jira = _source(lambda req: httpx.Response(200, json=_issue("PROJ-42")))
    lockstep = Lockstep()
    lockstep.bind(TicketSource, jira)

    resolved = _bound_ticket_source(lockstep)
    assert resolved is jira
    ticket = _load_ticket("PROJ-42", "", ".", source=resolved)
    assert ticket.key == "PROJ-42", "the key was fetched through the bound Jira source"

    # Nothing bound → the helper returns None, and _load_ticket would use the GitHub default.
    assert _bound_ticket_source(Lockstep()) is None
