"""The Implement verb's types: the request, the report, and the per-run session.

The adapters that serve `Implement` are the strategies themselves — `Oneshot` and `TDD` in this
package — bound directly: `lockstep.bind(Implement, TDD(...))`. Which approach runs is a bind-time
code decision made in `lockstep.py`; there is no dispatcher, no registry string, and no selection a
request or a ticket label can steer.

The capability declaration on each strategy is the load-bearing line. `WRITES_FILES` and
`EXECUTES_CODE` beside `SPENDS_BUDGET` is what makes the framework treat an implementing session as
an agent with agency rather than a read-only reviewer: `ApprovalGate` becomes a startup
requirement, egress enforcement becomes mandatory before the first call, and `Retry` refuses to
re-invoke it. None of that is configured per strategy — it is all keyed off the declaration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ...ai.builtins import ToolRunnerImpl, Workspace
from ...ai.context import ContextCurator, ContextItem, ContextNeed, ContextPackage
from ...ai.invoker import AiInvoker, InvokePolicy
from ...ai.prompt import PromptLayers
from ...ai.tools import ToolSet
from ...core.changes import ChangeGuard
from ...core.types import ChangeSet
from ...prompts.implement import ImplementPrompt


@dataclass(frozen=True)
class Implement:
    """The Implement request. Workflows do `ctx.do(Implement(...))`; the binding decides which
    strategy runs it.

    Frozen because it is hashed for step identity and serialized into checkpoints, like every
    other request type — a mutation after dispatch would change a key already written down.
    """

    #: The ticket, whose text is untrusted by construction: anyone who can file one can write
    #: into this prompt. `Ticket.as_context` is what tags it, and it is the only route in.
    ticket: Any
    token_budget: int = 60_000


@dataclass(frozen=True)
class ImplementReport:
    """What a session produced. The change set is the deliverable; the rest is its cover note."""

    changeset: ChangeSet = field(default_factory=ChangeSet)
    summary: str = ""
    notes: tuple[str, ...] = ()
    #: What the model says it could not do. Carried as data rather than left in prose, because a
    #: partial change that names its gap is reviewable and one that reads as complete is not.
    unfinished: tuple[str, ...] = ()
    #: Which strategy ran — the bound adapter's `id`, so an eval subject and a ledger record can
    #: key on the approach that produced this change.
    strategy: str = ""
    turns: int = 0

    @property
    def empty(self) -> bool:
        return not self.changeset.changes


@dataclass
class ImplementSession:
    """Everything one run needs, assembled per invoke by `AiStrategy._session`.

    Per-run state, deliberately: the workspace accumulates staged writes, and the invoker is built
    against this run's spend and transcript — which is why the bundle is constructed fresh each
    `invoke` rather than held on the long-lived bound adapter.
    """

    invoker: AiInvoker
    workspace: Workspace
    tools: ToolSet
    run_tool: ToolRunnerImpl
    policy: InvokePolicy
    layers: PromptLayers
    prompts: Mapping[str, type[ImplementPrompt]]
    curator: ContextCurator
    guard: ChangeGuard
    repo_root: str = "."

    def context(self, request: Implement) -> ContextPackage:
        """The ticket, curated. Provenance comes from `Ticket.as_context`, not from here."""
        items: list[ContextItem] = list(request.ticket.as_context())
        return self.curator.curate(items, ContextNeed(token_budget=request.token_budget))
