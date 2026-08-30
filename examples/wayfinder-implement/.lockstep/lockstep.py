"""Implement, charted before it is built.

The whole configuration. `Implement` is served by a strategy that refuses to claim a ticket behind
the frontier and refuses more than one ticket a session; `Chart` produces decisions and no change
at all.

Nothing here is special-cased by the framework. A verb request is a plain dataclass, a strategy is
whatever is bound to serve its type, and the constraints are checks in an adapter rather than
sentences in a prompt.
"""

import json
import sys
from pathlib import Path

# The example's own modules sit beside `.lockstep/`, not inside it: this file is configuration,
# and the code it configures is the project. `parent.parent` rather than `parent` for that reason.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wayfinder import (  # noqa: E402
    Chart,
    Implement,
    WayfinderChart,
    WayfinderImplement,
)

from in_lockstep import (  # noqa: E402
    Lockstep,
    Policy,
    RunContext,
    Ticket,
    TicketState,
    workflow,
)
from in_lockstep.core.spend import Budget  # noqa: E402
from in_lockstep.middleware import CostBudget, otel  # noqa: E402


def load_map(path: str, target: str = "", *, request: type[Chart] | type[Implement]) -> Chart | Implement:
    """Read a map from JSON. Tickets are data; the tracker is where they live in earnest.

    A real deployment would read these from `TicketSource` instead — the shape is the same, and
    `Ticket` is the framework's own type either way. A file keeps the example runnable without a
    tracker account.
    """
    raw = json.loads(Path(path).read_text())
    tickets = tuple(
        Ticket(
            key=t["key"],
            title=t.get("title", ""),
            description=t.get("description", ""),
            state=TicketState(t.get("state", "open")),
            acceptance_criteria=tuple(t.get("acceptance_criteria", ())),
        )
        for t in raw.get("tickets", [])
    )
    return request(
        target=target or raw.get("destination", ""),
        tickets=tickets,
        blocked_by={k: tuple(v) for k, v in raw.get("blocked_by", {}).items()},
    )


lockstep = Lockstep.detect()

# Charting is cheap and working a ticket is not, so the ceiling is sized for the expensive one.
lockstep.budget = Budget(usd=1.00, wall_seconds=600)
lockstep.middleware += [otel(), CostBudget(usd=1.00)]

# "Plan, don't deliver" as a constraint rather than an instruction. A charting session that could
# write files would eventually write some, whatever the prompt said.
lockstep.contribute(
    Policy(
        name="wayfinder-floor",
        source="example",
        deny_tools=("write_file", "delete_file", "shell", "apply_patch"),
        scan_input="block",
        max_turns=6,
    )
)

# Two bindings, because wayfinder has two sessions and they are different activities.
# Charting is deterministic and free; working a ticket spends. Giving them separate
# verbs is what keeps them apart in every span, metric and step id.
lockstep.bind(Chart, WayfinderChart())
lockstep.bind(Implement, WayfinderImplement(max_tickets_per_session=1))


@workflow(id="wayfinder/chart-github")
async def chart_github(ctx: RunContext, label: str = "wayfinder", target: str = ""):
    """Chart a map made of GitHub issues carrying a label.

    Needs `gh` authenticated and nothing else — no key, no spend, because charting is
    deterministic here. Blocking comes from a `Blocked by: #12, #13` line in the issue body, which
    is a convention rather than a GitHub feature, and greppable by the people working the map.
    """
    from github_map import load_map as load_github_map

    return await ctx.do(load_github_map(label=label, destination=target, request=Chart))


@workflow(id="wayfinder/work-github")
async def work_github(ctx: RunContext, label: str = "wayfinder", target: str = ""):
    """Claim one unblocked issue from the labelled map."""
    from github_map import load_map as load_github_map

    return await ctx.do(load_github_map(label=label, destination=target, request=Implement))


@workflow(id="wayfinder/chart")
async def chart(ctx: RunContext, map: str = "map.json", target: str = ""):
    """The first session: name the destination, map the frontier, stop.

    Takes a path rather than tickets, because `--arg` values arrive from the command line as
    strings. That constraint is a useful one: a workflow meant to be run by name has to be
    expressible by name.

    It returns an `Outcome` that succeeded and wrote nothing, which is the shape wayfinder needs
    and the reason `decided` is separate from `status` — a framework without that distinction has
    to call this either a failure or a no-op.
    """
    return await ctx.do(load_map(map, target, request=Chart))


@workflow(id="wayfinder/work")
async def work(ctx: RunContext, map: str = "map.json", target: str = ""):
    """A later session: claim one unblocked ticket, resolve it, and nothing else."""
    return await ctx.do(load_map(map, target, request=Implement))


# A name for the strategy, so an eval subject can key on it and `ls` can print it. In-lockstep's
# `StrategyRegistry` is a catalogue at 1.0 rather than a dispatcher — nothing selects from it yet —
# so the bindings above are what actually decide behaviour. Said here rather than left for someone
# to discover after registering into it.
STRATEGY_ID = "implement/wayfinder"
