"""GitHub issues, reusing the SCM client rather than a second auth path."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .base import Ticket, TicketDraft, TicketSource, TicketState, TicketType, criteria_from

MAX_BODY = 12_000
MAX_COMMENTS = 40


@dataclass
class GitHubIssues(TicketSource):
    root: Path = Path(".")
    token: str = ""

    def _gh_raw(self, *args: str) -> tuple[int, str, str]:
        """The single subprocess seam: exit code, stdout, stderr. Every other helper is built on
        it, so the env dance and the timeout live in exactly one place."""
        import os

        env = {**os.environ, "GH_TOKEN": self.token} if self.token else None
        result = subprocess.run(
            ["gh", *args], cwd=self.root, capture_output=True, text=True, timeout=60, env=env
        )
        return result.returncode, result.stdout, result.stderr

    def _gh(self, *args: str) -> str:
        code, out, err = self._gh_raw(*args)
        if code != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed: {err.strip()}")
        return out

    def _gh_json(self, *args: str) -> object:
        out = self._gh(*args)
        return json.loads(out) if out.strip() else None

    async def get(self, key: str) -> Ticket:
        raw = self._gh_json(
            "issue",
            "view",
            key.lstrip("#"),
            "--json",
            "number,title,body,state,labels,assignees,comments,url",
        )
        data = raw if isinstance(raw, dict) else {}
        body = str(data.get("body") or "")[:MAX_BODY]
        labels = tuple(str(label.get("name", "")) for label in data.get("labels", []) or [])
        return Ticket(
            key=f"#{data.get('number', key)}",
            title=str(data.get("title") or ""),
            description=body,
            state=_state(str(data.get("state") or "")),
            type=_type(labels),
            url=str(data.get("url") or ""),
            labels=labels,
            assignees=tuple(str(a.get("login", "")) for a in data.get("assignees", []) or []),
            acceptance_criteria=criteria_from(body),
            comments=tuple(
                str(c.get("body", ""))[:4000] for c in (data.get("comments") or [])[:MAX_COMMENTS]
            ),
            raw_state=str(data.get("state") or ""),
        )

    async def comment(self, ticket: Ticket, body: str) -> None:
        # Best-effort like it always was: a failed comment must not sink a run that otherwise
        # produced a change, so this swallows a non-zero exit rather than raising.
        self._gh_raw("issue", "comment", ticket.key.lstrip("#"), "--body", body)

    async def create(self, draft: TicketDraft) -> Ticket:
        from ..scm.github import _number_from

        args = ["issue", "create", "--title", draft.title, "--body", draft.description]
        for label in draft.labels:
            args += ["--label", label]
        out = self._gh(*args)
        # `gh issue create` prints the new issue's URL. Read the ticket back rather than
        # reconstructing it from the draft, so what returns is what the tracker actually holds.
        url = out.strip().splitlines()[-1] if out.strip() else ""
        number = _number_from(url)
        if number is not None:
            return await self.get(f"#{number}")
        return Ticket(key="", title=draft.title, url=url)

    async def search(self, query: str, *, limit: int = 20) -> tuple[Ticket, ...]:
        raw = self._gh_json(
            "issue",
            "list",
            "--search",
            query,
            "--limit",
            str(limit),
            "--json",
            "number,title,state,labels,url",
        )
        rows = raw if isinstance(raw, list) else []
        out = []
        for row in rows:
            labels = tuple(str(label.get("name", "")) for label in row.get("labels", []) or [])
            out.append(
                Ticket(
                    key=f"#{row.get('number', '')}",
                    title=str(row.get("title") or ""),
                    state=_state(str(row.get("state") or "")),
                    type=_type(labels),
                    url=str(row.get("url") or ""),
                    labels=labels,
                    raw_state=str(row.get("state") or ""),
                )
            )
        return tuple(out)

    async def add_labels(self, ticket: Ticket, *labels: str) -> None:
        if not labels:
            return
        args = ["issue", "edit", ticket.key.lstrip("#")]
        for label in labels:
            args += ["--add-label", label]
        self._gh(*args)

    async def transition(self, ticket: Ticket, state: TicketState, *, raw: str = "") -> None:
        """GitHub issues have two states, so this maps coarse and refuses what it cannot mean."""
        from ...core.ports import Unsupported

        if raw:
            # The caller named a tracker-specific state. GitHub has no arbitrary states to move to,
            # so honouring `raw` is impossible — and silently closing/reopening instead would do
            # something other than what was asked. A Jira adapter is where `raw` means something.
            raise Unsupported(f"GitHub issues have no state named {raw!r}; they are only open or closed")
        number = ticket.key.lstrip("#")
        if state in (TicketState.CLOSED, TicketState.DONE):
            self._gh("issue", "close", number)
            return
        if state is TicketState.OPEN:
            self._gh("issue", "reopen", number)
            return
        raise Unsupported(f"GitHub issues cannot represent {state.value!r}; only open and closed exist")


def _state(raw: str) -> TicketState:
    return {"OPEN": TicketState.OPEN, "CLOSED": TicketState.CLOSED}.get(raw.upper(), TicketState.OTHER)


def _type(labels: tuple[str, ...]) -> TicketType:
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
