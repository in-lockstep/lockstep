"""GitLab issues, over the REST API.

The same normalized `Ticket` the other trackers produce, from the API GitLab actually serves:
issues addressed by iid within a project, notes as a second endpoint, labels as plain strings,
and exactly two states — `opened` and `closed` — which puts this adapter's `transition` in the
same honest place GitHub's is: it maps coarse and refuses what it cannot mean.

Transport and auth mirror `GitLabScm`: `httpx` with an injectable client, the token in the
`PRIVATE-TOKEN` header, and errors that carry GitLab's own JSON body rather than the credential.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...core.ports import Unsupported
from .base import Ticket, TicketDraft, TicketSource, TicketState, TicketType, criteria_from

MAX_BODY = 12_000
MAX_COMMENTS = 40

_STATE = {"opened": TicketState.OPEN, "closed": TicketState.CLOSED}


def _type(labels: tuple[str, ...]) -> TicketType:
    """From labels, the same way the GitHub adapter reads them: GitLab's built-in issue types are
    only issue/incident, so a bug is a label here too."""
    lowered = {name.lower() for name in labels}
    for label, kind in (
        ("bug", TicketType.BUG),
        ("epic", TicketType.EPIC),
        ("spike", TicketType.SPIKE),
        ("story", TicketType.STORY),
    ):
        if label in lowered:
            return kind
    return TicketType.TASK


@dataclass
class GitLabIssues(TicketSource):
    """A `TicketSource` backed by one GitLab project's issues.

    `base_url` and `project` default from the environment GitLab CI provides (`CI_SERVER_URL`,
    `CI_PROJECT_PATH`), so on a pipeline the zero-argument construction works; on a laptop pass
    them, or the first request says exactly which one is missing.
    """

    base_url: str = ""
    token: str = ""
    project: str = ""
    timeout: float = 30.0
    #: Injectable for tests, so the adapter is exercised without a network.
    client: Any = None

    def _http(self) -> Any:
        if self.client is not None:
            return self.client
        import httpx

        base = self.base_url or os.environ.get("CI_SERVER_URL", "")
        if not base:
            raise RuntimeError("no GitLab server to talk to: pass base_url= or set CI_SERVER_URL")
        token = self.token or os.environ.get("GITLAB_TOKEN", "")
        headers = {"PRIVATE-TOKEN": token} if token else {}
        self.client = httpx.Client(
            base_url=f"{base.rstrip('/')}/api/v4", headers=headers, timeout=self.timeout
        )
        return self.client

    def _request(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        import httpx

        try:
            response = self._http().request(method, path, json=json, params=params)
        except httpx.HTTPError as e:
            raise RuntimeError(f"gitlab {method} {path}: {type(e).__name__}: {e}") from e
        if response.status_code >= 400:
            raise RuntimeError(f"gitlab {method} {path} -> {response.status_code}: {response.text[:500]}")
        return response.json() if response.content else None

    def _project_path(self) -> str:
        project = self.project or os.environ.get("CI_PROJECT_PATH", "")
        if not project:
            raise RuntimeError("no GitLab project to address: pass project= or set CI_PROJECT_PATH")
        return quote(project, safe="")

    async def get(self, key: str) -> Ticket:
        iid = key.lstrip("#")
        data = self._request("GET", f"/projects/{self._project_path()}/issues/{iid}") or {}
        return _to_ticket(data, self._comments(iid))

    def _comments(self, iid: str) -> tuple[str, ...]:
        """The first `MAX_COMMENTS` things people actually wrote, oldest first.

        Notes are a second endpoint, and system notes — "changed the label", "mentioned in
        commit" — are machine narration interleaved with the conversation, not part of it. The
        cap therefore counts HUMAN comments, which is why this paginates instead of taking one
        page and filtering it: a busy issue's first page can be mostly narration, and capping
        before the filter silently drops the conversation the cap was meant to bound. GitHub's
        adapter caps the same set; its API just never interleaves the narration.
        """
        import httpx

        path = f"/projects/{self._project_path()}/issues/{iid}/notes"
        out: list[str] = []
        page = "1"
        # Bounded like the SCM adapter's note walk: enough pages to fill the cap from any thread
        # a human still reads, and a ceiling so a misbehaving server cannot loop this forever.
        for _ in range(10):
            try:
                response = self._http().request(
                    "GET", path, params={"per_page": 100, "sort": "asc", "page": page}
                )
            except httpx.HTTPError as e:
                raise RuntimeError(f"gitlab GET {path}: {type(e).__name__}: {e}") from e
            if response.status_code >= 400:
                raise RuntimeError(f"gitlab GET {path} -> {response.status_code}: {response.text[:500]}")
            rows = response.json() if response.content else []
            out += [
                str(note.get("body", ""))[:4000]
                for note in rows
                if isinstance(note, dict) and not note.get("system", False)
            ]
            page = str(response.headers.get("x-next-page", "") or "")
            if not page or len(out) >= MAX_COMMENTS:
                break
        return tuple(out[:MAX_COMMENTS])

    async def comment(self, ticket: Ticket, body: str) -> None:
        try:
            self._request(
                "POST",
                f"/projects/{self._project_path()}/issues/{ticket.key.lstrip('#')}/notes",
                json={"body": body},
            )
        except RuntimeError as e:
            # Best-effort, like every other tracker: a failed comment must not sink a run that
            # produced a change, but it is logged rather than swallowed whole.
            print(f"comment    could not post to {ticket.key}: {e}")

    async def create(self, draft: TicketDraft) -> Ticket:
        payload: dict[str, Any] = {"title": draft.title, "description": draft.description}
        if draft.labels:
            payload["labels"] = ",".join(draft.labels)
        created = self._request("POST", f"/projects/{self._project_path()}/issues", json=payload) or {}
        iid = created.get("iid")
        # Read the ticket back rather than reconstructing it from the draft, so what returns is
        # what the tracker actually holds.
        if isinstance(iid, int):
            return await self.get(f"#{iid}")
        return Ticket(key="", title=draft.title)

    async def search(self, query: str, *, limit: int = 20) -> tuple[Ticket, ...]:
        rows = (
            self._request(
                "GET",
                f"/projects/{self._project_path()}/issues",
                params={"search": query, "per_page": limit},
            )
            or []
        )
        return tuple(_to_ticket(row, ()) for row in rows if isinstance(row, dict))

    async def add_labels(self, ticket: Ticket, *labels: str) -> None:
        if not labels:
            return
        self._request(
            "PUT",
            f"/projects/{self._project_path()}/issues/{ticket.key.lstrip('#')}",
            json={"add_labels": ",".join(labels)},
        )

    async def transition(self, ticket: Ticket, state: TicketState, *, raw: str = "") -> None:
        """GitLab issues have two states, so this maps coarse and refuses what it cannot mean —
        the same shape as GitHub, because the trackers share the constraint."""
        if raw:
            raise Unsupported(f"GitLab issues have no state named {raw!r}; they are only opened or closed")
        iid = ticket.key.lstrip("#")
        if state in (TicketState.CLOSED, TicketState.DONE):
            self._request(
                "PUT", f"/projects/{self._project_path()}/issues/{iid}", json={"state_event": "close"}
            )
            return
        if state is TicketState.OPEN:
            self._request(
                "PUT", f"/projects/{self._project_path()}/issues/{iid}", json={"state_event": "reopen"}
            )
            return
        raise Unsupported(f"GitLab issues cannot represent {state.value!r}; only opened and closed exist")


def _to_ticket(data: dict[str, Any], comments: tuple[str, ...]) -> Ticket:
    description = str(data.get("description") or "")[:MAX_BODY]
    labels = tuple(str(label) for label in data.get("labels") or [])
    state = str(data.get("state") or "")
    milestone = data.get("milestone") or {}
    iid = data.get("iid")
    return Ticket(
        key=f"#{iid}" if iid is not None else "",
        title=str(data.get("title") or ""),
        description=description,
        state=_STATE.get(state, TicketState.OTHER),
        type=_type(labels),
        url=str(data.get("web_url") or ""),
        labels=labels,
        assignees=tuple(
            str(a.get("username", "")) for a in data.get("assignees") or [] if isinstance(a, dict)
        ),
        acceptance_criteria=criteria_from(description),
        comments=comments,
        raw_state=state,
        milestone=str(milestone.get("title", "")) if isinstance(milestone, dict) else "",
    )
