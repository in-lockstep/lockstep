"""GitHub issues, reusing the SCM client rather than a second auth path."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .base import Ticket, TicketSource, TicketState, TicketType, criteria_from

MAX_BODY = 12_000
MAX_COMMENTS = 40


@dataclass
class GitHubIssues(TicketSource):
    root: Path = Path(".")
    token: str = ""

    def _gh_json(self, *args: str) -> object:
        import os

        env = {**os.environ, "GH_TOKEN": self.token} if self.token else None
        result = subprocess.run(
            ["gh", *args], cwd=self.root, capture_output=True, text=True, timeout=60, env=env
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else None

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
        import os

        env = {**os.environ, "GH_TOKEN": self.token} if self.token else None
        subprocess.run(
            ["gh", "issue", "comment", ticket.key.lstrip("#"), "--body", body],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )


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
