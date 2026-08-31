"""The wayfinder example, exercised.

An example nothing runs is documentation that compiles. This one extends a verb the framework does
not ship an adapter for, so it is also the closest thing the repository has to a test that the
extension story works from outside — `examples/pr-review` binds a shipped adapter; this one builds
its own.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from in_lockstep.core.outcome import Status
from in_lockstep.core.verbs import Capability, Verb
from in_lockstep.platform.tickets.base import Ticket, TicketState

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "wayfinder-implement"


@pytest.fixture(autouse=True)
def _importable() -> None:
    sys.path.insert(0, str(EXAMPLE))
    yield
    sys.path.remove(str(EXAMPLE))
    for name in ("wayfinder",):
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _no_global_leakage():
    """Both registries are process-global, and loading the example touches both.

    `Verb` interns and `@workflow` registers by id, so a second load of the same module raises
    `DuplicateWorkflow` rather than quietly winning — which is the right behaviour and means a
    test that loads it twice has to say so.
    """
    # Snapshot and restore, not clear: `selfcheck` is registered when `cli` is imported, so
    # clearing after a test removes it for the rest of the process.
    from in_lockstep.core.workflow import restore, snapshot

    state = snapshot()
    yield
    Verb.forget_custom()
    restore(state)


def _map() -> tuple[Ticket, ...]:
    return (
        Ticket(key="MAP-1", title="Destination", description="the point of the effort"),
        Ticket(key="MAP-2", title="A decision", description="d", acceptance_criteria=("recorded",)),
        Ticket(key="MAP-3", title="Caching, somehow"),
    )


def _spec(target: str, request=None, **kw):
    from wayfinder import Implement

    request = request or Implement
    return request(target=target, tickets=_map(), blocked_by={"MAP-1": ("MAP-2", "MAP-3")}, **kw)


def test_charting_succeeds_and_delivers_nothing() -> None:
    """ "Plan, don't deliver" — a run that succeeded, decided something, and wrote no change.

    That combination is why `decided` is separate from `status`: without it, a charting session
    has to be reported as either a failure or a no-op, and it is neither.
    """
    from wayfinder import Chart, WayfinderChart

    outcome = asyncio.run(WayfinderChart().invoke(None, _spec("MAP-1", request=Chart)))
    assert outcome.status is Status.SUCCEEDED
    assert outcome.decided is True
    assert outcome.cost.usd == 0.0, "charting is deterministic here and must not spend"
    assert outcome.value.frontier == ("MAP-2", "MAP-3")


def test_charting_does_not_require_the_destination_to_be_reachable() -> None:
    """The destination of a map is blocked by definition; that is what makes it worth charting."""
    from wayfinder import Chart, WayfinderChart

    outcome = asyncio.run(WayfinderChart().invoke(None, _spec("MAP-1", request=Chart)))
    assert outcome.status is Status.SUCCEEDED


def test_fog_is_read_off_the_ticket_not_guessed() -> None:
    """A ticket with no description and no acceptance criteria has not been specified."""
    from wayfinder import Chart, WayfinderChart

    outcome = asyncio.run(WayfinderChart().invoke(None, _spec("MAP-1", request=Chart)))
    assert outcome.value.fog == ("MAP-3",)
    fog = [f for f in outcome.findings if f.id == "wayfinder.fog"]
    assert [f.message for f in fog] == ["MAP-3 is not sharp enough to phrase precisely yet; left as fog."]


def test_charting_reports_the_frontier_as_findings() -> None:
    """A charting session whose map lived only in a return value would say nothing.

    Findings are the framework's channel for what a run noticed: they print, and they reach the
    ledger, which stores findings rather than a count of them.
    """
    from wayfinder import Chart, WayfinderChart

    outcome = asyncio.run(WayfinderChart().invoke(None, _spec("MAP-1", request=Chart)))
    by_id = {f.id: f.message for f in outcome.findings}
    assert "MAP-2" in by_id["wayfinder.frontier"]
    assert "blocked by" in by_id["wayfinder.destination"]


def test_a_ticket_behind_the_frontier_is_refused() -> None:
    """The frontier as a check rather than a sentence in a prompt."""
    from wayfinder import WayfinderImplement

    outcome = asyncio.run(WayfinderImplement().invoke(None, _spec("MAP-1")))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "wayfinder.not_on_frontier"
    assert "MAP-2" in outcome.findings[0].message


def test_a_closed_blocker_no_longer_blocks() -> None:
    """Otherwise the frontier never advances and the map is a list."""
    from wayfinder import Implement, WayfinderImplement

    tickets = tuple(
        t if t.key != "MAP-2" else Ticket(key="MAP-2", title="done", state=TicketState.CLOSED) for t in _map()
    )
    spec = Implement(target="MAP-1", tickets=tickets, blocked_by={"MAP-1": ("MAP-2",)})
    outcome = asyncio.run(WayfinderImplement().invoke(None, spec))
    assert outcome.reason != "wayfinder.not_on_frontier"


def test_one_ticket_per_session_is_a_number_not_a_rule_of_thumb() -> None:
    from wayfinder import Implement, WayfinderImplement

    busy = tuple(
        Ticket(key=k, title=k, description="d", state=TicketState.IN_PROGRESS) for k in ("MAP-2", "MAP-3")
    )
    spec = Implement(target="MAP-2", tickets=busy)
    outcome = asyncio.run(WayfinderImplement(max_tickets_per_session=1).invoke(None, spec))
    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "wayfinder.session_scope"


def test_a_ticket_not_on_the_map_is_refused_by_name() -> None:
    """Wayfinder refers to tickets by name; a name nobody charted is not a target."""
    from wayfinder import WayfinderImplement

    outcome = asyncio.run(WayfinderImplement().invoke(None, _spec("NOPE")))
    assert outcome.reason == "wayfinder.unknown_ticket"


def test_charting_declares_that_it_does_not_spend() -> None:
    """The capability drives egress, approval and retry. Getting it wrong is not cosmetic."""
    from wayfinder import WayfinderChart, WayfinderImplement

    assert Capability.SPENDS_BUDGET not in WayfinderChart.capabilities
    assert Capability.SPENDS_BUDGET in WayfinderImplement.capabilities


def test_charting_has_a_verb_of_its_own() -> None:
    """Sharing `implement` would file both sessions under one heading in every span and metric."""
    from wayfinder import CHART, WayfinderChart, WayfinderImplement

    assert WayfinderChart.verb is CHART
    assert WayfinderImplement.verb is Verb.IMPLEMENT
    assert CHART is not Verb.IMPLEMENT


def test_the_example_config_binds_both_verbs() -> None:
    """`ls` prints a verb that is defined and unbound; the example must not leave one."""
    from in_lockstep.loader import load, lockstep_from

    module, _ = load(EXAMPLE)
    lockstep = lockstep_from(module)
    bound = {b.iface.__name__ for b in lockstep.container.resolved()}
    assert bound == {"Chart", "Implement"}


def test_the_example_denies_the_tools_that_would_let_charting_deliver() -> None:
    """ "Plan, don't deliver" survives the model, because the model is not offered the tools."""
    from in_lockstep.loader import load, lockstep_from

    module, _ = load(EXAMPLE)
    resolved = lockstep_from(module).policy.resolve()
    assert "write_file" in resolved.deny_tools
    assert resolved.scan_input == "block"


# -- the examples track the layout the framework actually reads ---------------------------------


def test_every_example_keeps_its_configuration_where_the_loader_looks() -> None:
    """An example demonstrating a layout the framework no longer reads teaches the wrong thing.

    It would still WORK — a root `lockstep.py` is loaded with a deprecation notice rather than
    refused — which is exactly why this needs a test: nothing would fail, and the examples would
    quietly become the documentation for the previous arrangement.
    """
    from pathlib import Path

    from in_lockstep.loader import LEGACY_MODULE_FILE, MODULE_FILE

    root = Path(__file__).resolve().parents[2] / "examples"
    examples = [
        d
        for d in root.iterdir()
        if d.is_dir()
        and not d.name.startswith("__")
        # An installable package (acme-standards, acme-review-prompts) is not a repository: it is
        # applied by being a dependency, so it has an entry point where a repository has a
        # lockstep.py, and demanding the latter would teach the wrong thing in the other direction.
        and not (d / "pyproject.toml").exists()
        # A catalog (lockstep-index) is a third kind of example and not a repository either: it is
        # a static file somebody publishes, read at search and accept time and never during a run.
        # It has no lifecycle because nothing in it ever executes, which is the property that makes
        # it safe to fetch — so demanding a lockstep.py here would be demanding the one thing a
        # catalog must not have.
        and not (d / "index.toml").exists()
    ]
    assert examples, "no examples found, so this test asserts nothing"
    for example in examples:
        assert (example / MODULE_FILE).is_file(), f"{example.name} has no {MODULE_FILE}"
        assert not (example / LEGACY_MODULE_FILE).exists(), (
            f"{example.name} still has a root {LEGACY_MODULE_FILE}"
        )


def test_the_example_binds_the_same_verb_the_framework_ships_an_adapter_for() -> None:
    """The contrast that makes this example worth keeping now that `implement/oneshot` exists.

    Two adapters, same verb, neither aware of the other: `oneshot` is handed a ticket and builds
    it; wayfinder refuses to build anything the map says is not claimable. A verb interface being a
    marker class is what makes both possible at once.
    """
    import wayfinder

    from in_lockstep.core.verbs import Verb

    assert wayfinder.WayfinderImplement.verb is Verb.IMPLEMENT
    assert wayfinder.WayfinderChart.verb is Verb("chart")
    assert wayfinder.WayfinderChart.verb is not Verb.IMPLEMENT
