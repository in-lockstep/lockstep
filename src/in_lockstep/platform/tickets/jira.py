"""Jira, over the REST API.

The design's §7.2 target: a `TicketSource` for the system of record most adopting organisations
actually use, reachable from `implement --ticket PROJ-123`. Needs only `httpx`, already a core
dependency, so it adds no extra to install.

Two auth shapes, because Jira has two deployments. Cloud authenticates with an account email and
an API token as HTTP basic; Data Center takes a personal access token as a bearer. The token is
handed to the `httpx` client's auth, so it travels in the `Authorization` header and never in a
URL or an error string — the errors this raises carry Jira's own JSON body, which names the field
that was wrong, not the credential.

The REST v2 endpoints are used deliberately over v3: v2 returns `description` and comment bodies
as plain strings, where v3 returns Atlassian Document Format (a JSON tree) that would have to be
walked back into text before a model or the acceptance-criteria parser could read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...core.ports import Unsupported
from .base import Ticket, TicketDraft, TicketSource, TicketState, TicketType, criteria_from

MAX_BODY = 12_000
MAX_COMMENTS = 40

# Jira's status categories are the one cross-site invariant — individual status names vary per
# project, but every status rolls up to one of these three keys, so a workflow can branch on state
# without knowing a site's scheme. `raw_state` carries the site-specific name alongside.
_CATEGORY_STATE = {
    "new": TicketState.OPEN,
    "indeterminate": TicketState.IN_PROGRESS,
    "done": TicketState.DONE,
}

_TYPE_BY_NAME = {
    "bug": TicketType.BUG,
    "story": TicketType.STORY,
    "task": TicketType.TASK,
    "epic": TicketType.EPIC,
    "spike": TicketType.SPIKE,
}
_NAME_BY_TYPE = {
    TicketType.BUG: "Bug",
    TicketType.STORY: "Story",
    TicketType.TASK: "Task",
    TicketType.EPIC: "Epic",
    TicketType.SPIKE: "Spike",
}

_ISSUE_FIELDS = "summary,description,status,issuetype,labels,assignee,comment,fixVersions,versions"


@dataclass(frozen=True)
class JiraTypeRef:
    """A Jira issue type, by name or explicit id. Sites' type schemes differ, so a workflow that
    needs a custom type names it here rather than assuming an id."""

    name: str = ""
    id: str = ""

    def as_field(self) -> dict[str, str]:
        return {"id": self.id} if self.id else {"name": self.name}


@dataclass
class JiraSource(TicketSource):
    """A `TicketSource` backed by a Jira site.

    `email` set selects Cloud basic auth; empty selects a Data Center bearer token. `project` is
    the key new issues are created under. `acceptance_field` names a custom field (e.g.
    `customfield_10001`) to read acceptance criteria from; without it they are parsed from the
    description, the same `criteria_from` every other tracker's body goes through.
    """

    base_url: str
    email: str = ""
    token: str = ""
    project: str = ""
    acceptance_field: str = ""
    timeout: float = 30.0
    #: Injectable for tests, so the adapter is exercised without a network. Built from the auth
    #: above when absent.
    client: Any = None

    def _http(self) -> Any:
        if self.client is not None:
            return self.client
        import httpx

        auth = (self.email, self.token) if self.email else None
        headers = {"Authorization": f"Bearer {self.token}"} if not self.email and self.token else {}
        self.client = httpx.Client(
            base_url=self.base_url.rstrip("/"), auth=auth, headers=headers, timeout=self.timeout
        )
        return self.client

    def _request(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        import httpx

        try:
            response = self._http().request(method, path, json=json, params=params)
        except httpx.HTTPError as e:
            # A transport failure — connection refused, DNS, timeout — is not an `httpx` status
            # error and would otherwise escape as a raw traceback: `RuntimeError` is what every
            # caller already handles (the CLI's `except RuntimeError`, `comment`'s best-effort
            # guard), so all of them degrade the same whether Jira answered badly or not at all.
            raise RuntimeError(f"jira {method} {path}: {type(e).__name__}: {e}") from e
        if response.status_code >= 400:
            # Jira's body names the field or permission that failed; the token is in the request
            # header, not here, so this is safe to surface and to let reach the ledger.
            body = response.text[:500]
            raise RuntimeError(f"jira {method} {path} -> {response.status_code}: {body}")
        return response.json() if response.content else None

    def _fields(self) -> str:
        """The issue fields to request. A named `acceptance_field` is appended, or Jira's `fields`
        allow-list would omit it and the custom-field criteria could never be read."""
        return f"{_ISSUE_FIELDS},{self.acceptance_field}" if self.acceptance_field else _ISSUE_FIELDS

    async def get(self, key: str) -> Ticket:
        data = self._request("GET", f"/rest/api/2/issue/{key}", params={"fields": self._fields()})
        return _to_ticket(data or {}, self.acceptance_field)

    async def comment(self, ticket: Ticket, body: str) -> None:
        try:
            self._request("POST", f"/rest/api/2/issue/{ticket.key}/comment", json={"body": body})
        except RuntimeError as e:
            # Best-effort, like the GitHub adapter: a failed comment must not sink a run that
            # produced a change, but it is logged rather than swallowed whole.
            print(f"comment    could not post to {ticket.key}: {e}")

    async def create(self, draft: TicketDraft) -> Ticket:
        if not self.project:
            raise Unsupported("JiraSource needs a `project` to create issues under")
        fields = {
            "project": {"key": self.project},
            "summary": draft.title,
            "description": draft.description,
            "issuetype": {"name": _NAME_BY_TYPE.get(draft.type, "Task")},
        }
        if draft.labels:
            fields["labels"] = list(draft.labels)
        created = self._request("POST", "/rest/api/2/issue", json={"fields": fields})
        key = str((created or {}).get("key") or "")
        return await self.get(key) if key else Ticket(key="", title=draft.title)

    async def search(self, query: str, *, limit: int = 20) -> tuple[Ticket, ...]:
        # Cloud removed the unbounded `/rest/api/2/search` in favour of the token-paginated
        # `/search/jql`; Data Center keeps the old one. Both return an `issues` array this maps
        # the same way, and the deployment is the same signal the auth uses — an email means Cloud.
        path = "/rest/api/2/search/jql" if self.email else "/rest/api/2/search"
        data = self._request(
            "POST",
            path,
            json={"jql": query, "maxResults": limit, "fields": self._fields().split(",")},
        )
        issues = (data or {}).get("issues", []) if isinstance(data, dict) else []
        return tuple(_to_ticket(issue, self.acceptance_field) for issue in issues)

    async def add_labels(self, ticket: Ticket, *labels: str) -> None:
        if not labels:
            return
        self._request(
            "PUT",
            f"/rest/api/2/issue/{ticket.key}",
            json={"update": {"labels": [{"add": label} for label in labels]}},
        )

    async def transition(self, ticket: Ticket, state: TicketState, *, raw: str = "") -> None:
        """Jira transitions are workflow-specific, so this asks the issue which are available and
        matches one — when `raw` is given, against the transition's name OR its target status name
        (so a `raw_state` read back from a ticket round-trips, even where the two differ), else by
        the target status category the framework state maps to. No matching transition is
        `Unsupported`, not a silent no-op: the workflow does not permit that move from here."""
        available = self._request("GET", f"/rest/api/2/issue/{ticket.key}/transitions") or {}
        transitions = available.get("transitions", []) if isinstance(available, dict) else []
        chosen = _match_transition(transitions, state, raw)
        if chosen is None:
            offered = ", ".join(t.get("name", "") for t in transitions) or "(none)"
            raise Unsupported(
                f"no Jira transition from {ticket.key}'s current status to "
                f"{raw or state.value!r}; available: {offered}"
            )
        self._request(
            "POST", f"/rest/api/2/issue/{ticket.key}/transitions", json={"transition": {"id": chosen}}
        )


def _match_transition(transitions: list[dict[str, Any]], state: TicketState, raw: str) -> str | None:
    if raw:
        low = raw.lower()
        for t in transitions:
            to = t.get("to") or {}
            # The transition's own name, or the status it lands on — a Jira workflow names the
            # edge ("Start Progress") differently from the status ("In Progress"), and a caller
            # feeding back a `raw_state` means the status.
            if low in (str(t.get("name", "")).lower(), str(to.get("name", "")).lower()):
                return str(t.get("id"))
        return None
    for t in transitions:
        category = ((t.get("to") or {}).get("statusCategory") or {}).get("key", "")
        mapped = _CATEGORY_STATE.get(category)
        # DONE and CLOSED are both terminal, and Jira's `done` category serves both — GitHub maps a
        # closed issue to CLOSED, so a tracker-agnostic close(CLOSED) must find a done-category
        # transition here rather than refusing it.
        if mapped is state or (category == "done" and state in (TicketState.DONE, TicketState.CLOSED)):
            return str(t.get("id"))
    return None


def _to_ticket(data: dict[str, Any], acceptance_field: str) -> Ticket:
    fields = data.get("fields") or {}
    status = fields.get("status") or {}
    category = (status.get("statusCategory") or {}).get("key", "")
    issue_type = (fields.get("issuetype") or {}).get("name", "")
    description = str(fields.get("description") or "")[:MAX_BODY]

    criteria: tuple[str, ...] = ()
    if acceptance_field and fields.get(acceptance_field):
        raw_criteria = fields.get(acceptance_field)
        criteria = criteria_from(str(raw_criteria)) if isinstance(raw_criteria, str) else ()
    if not criteria:
        criteria = criteria_from(description)

    assignee = fields.get("assignee") or {}
    return Ticket(
        key=str(data.get("key") or ""),
        title=str(fields.get("summary") or ""),
        description=description,
        state=_CATEGORY_STATE.get(category, TicketState.OTHER),
        type=_TYPE_BY_NAME.get(issue_type.lower(), TicketType.OTHER),
        url=_browse_url(data),
        labels=tuple(str(label) for label in fields.get("labels") or []),
        assignees=tuple(a for a in (assignee.get("displayName") or assignee.get("accountId"),) if a),
        acceptance_criteria=criteria,
        comments=tuple(
            str(c.get("body", ""))[:4000]
            for c in ((fields.get("comment") or {}).get("comments") or [])[:MAX_COMMENTS]
        ),
        raw_state=str(status.get("name") or ""),
        fix_versions=tuple(str(v.get("name", "")) for v in fields.get("fixVersions") or []),
        affects_versions=tuple(str(v.get("name", "")) for v in fields.get("versions") or []),
    )


def _browse_url(data: dict[str, Any]) -> str:
    """The human URL, derived from the API `self` link so a ticket points at the board, not the
    REST endpoint. Empty when the shape is unfamiliar rather than guessed wrong."""
    self_link = str(data.get("self") or "")
    key = str(data.get("key") or "")
    if self_link and key and "/rest/" in self_link:
        return f"{self_link.split('/rest/', 1)[0]}/browse/{key}"
    return ""
