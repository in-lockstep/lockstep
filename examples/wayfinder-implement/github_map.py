"""Build a wayfinder map out of GitHub issues.

`TicketSource` declares `get` and `comment` — no listing, because a map is a wayfinder idea rather
than a framework one. So this is the example doing what an extender does: reaching for `gh` for
the part the framework does not model, and reusing `GitHubIssues` for the part it does, so tickets
arrive as the framework's own `Ticket` with their text already tagged untrusted.

**Blocking.** GitHub has no general "blocked by" between issues, so this reads a convention from
the issue body:

    Blocked by: #12, #13

One line, greppable, and visible to a human reading the issue — which matters more than elegance,
because the frontier is only useful if the people working from it can see the same graph the
framework does.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from wayfinder import ImplementSpec

from in_lockstep.platform.tickets.base import Ticket, TicketState

#: `Blocked by: #12, #13` — case-insensitive, comma or space separated.
BLOCKED_BY = re.compile(r"blocked\s*by\s*:?\s*((?:#\d+[,\s]*)+)", re.IGNORECASE)


def _gh(root: Path, *args: str) -> object:
    result = subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def blockers_in(body: str) -> tuple[str, ...]:
    """Issue keys this body says it is blocked by."""
    found: list[str] = []
    for match in BLOCKED_BY.finditer(body or ""):
        found.extend(f"#{n}" for n in re.findall(r"#(\d+)", match.group(1)))
    return tuple(dict.fromkeys(found))


def load_map(
    *,
    label: str = "wayfinder",
    destination: str = "",
    root: Path | str = ".",
    mode: str = "chart",
    limit: int = 50,
) -> ImplementSpec:
    """Every issue carrying `label`, as a map.

    A label rather than a milestone or a project, because a label is the cheapest thing to add to
    an issue that already exists — and wayfinder's charting session is mostly recognising that
    work you already filed belongs to one effort.
    """
    root = Path(root)
    raw = _gh(
        root,
        "issue",
        "list",
        "--label",
        label,
        "--state",
        "all",
        "--limit",
        str(limit),
        "--json",
        "number,title,body,state,labels",
    )
    issues = raw if isinstance(raw, list) else []

    tickets = tuple(
        Ticket(
            key=f"#{i['number']}",
            title=i.get("title", ""),
            description=i.get("body") or "",
            state=TicketState.CLOSED if i.get("state") == "CLOSED" else TicketState.OPEN,
            labels=tuple(label_.get("name", "") for label_ in i.get("labels", [])),
            # Acceptance criteria as a task list, which is how GitHub issues carry them in
            # practice. Their presence is what tells fog from a specified ticket.
            acceptance_criteria=tuple(
                line.strip()[6:].strip()
                for line in (i.get("body") or "").splitlines()
                if line.strip().startswith(("- [ ]", "- [x]"))
            ),
            url=f"issue #{i['number']}",
        )
        for i in issues
    )
    blocked_by = {t.key: blockers_in(t.description) for t in tickets}
    return ImplementSpec(
        target=destination or (tickets[0].key if tickets else ""),
        tickets=tickets,
        blocked_by={k: v for k, v in blocked_by.items() if v},
        mode=mode,
    )


__all__ = ["BLOCKED_BY", "blockers_in", "load_map"]
