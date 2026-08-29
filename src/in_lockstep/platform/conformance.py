"""A conformance kit for third-party ports.

The protocols were widened before any third party implemented them, precisely because
retrofitting a Protocol is a breaking change — and committing a shape without shipping the thing
that checks an implementation against it invites every adapter to interpret the edge cases its
own way. GitLab, Jira, and every community tracker should agree on what `search` returns before
three of them exist, not after.

Use it from the adapter's own test suite:

    from in_lockstep.platform.conformance import assert_scm, assert_ticket_source

    def test_conformance() -> None:
        assert_ticket_source(JiraTickets(base_url="https://jira.example.test"))
        assert_scm(GitLabScm())

These are structural checks: the methods exist, are async where the protocol says so, and accept
the parameters workflows will pass. What they deliberately do not check is behaviour against a
live tracker — that needs credentials and fixtures only the adapter's author can supply, and a
kit that needed them would never run in anyone's CI. The framework's own `GitHubIssues`,
`GitHubScm` and `GitLocal` pass this kit, and a test holds them to it.
"""

from __future__ import annotations

import inspect

from .scm.base import Scm
from .tickets.base import TicketSource


class Nonconformant(AssertionError):
    """The implementation does not satisfy the protocol; the message lists every miss at once."""


def _method(impl: object, name: str, *, must_be_async: bool, problems: list[str]) -> object | None:
    method: object | None = getattr(impl, name, None)
    if method is None or not callable(method):
        problems.append(f"missing method {name}()")
        return None
    if must_be_async and not inspect.iscoroutinefunction(method):
        problems.append(f"{name}() must be `async def`; workflows await it")
    if not must_be_async and inspect.iscoroutinefunction(method):
        problems.append(f"{name}() must be synchronous; callers do not await it")
    return method


def _accepts(method: object, keyword: str, problems: list[str], owner: str) -> None:
    try:
        signature = inspect.signature(method)  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        return
    parameters = signature.parameters
    if keyword not in parameters and not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    ):
        problems.append(f"{owner}() does not accept {keyword}=, which workflows pass")


def assert_ticket_source(impl: object) -> None:
    """Structurally a `TicketSource`: required methods present and async, optional methods
    either implemented or inherited as `Unsupported`-raising defaults — never absent."""
    problems: list[str] = []
    methods = {
        name: _method(impl, name, must_be_async=True, problems=problems)
        for name in ("get", "comment", "create", "search", "add_labels", "transition")
    }
    if methods["search"] is not None:
        _accepts(methods["search"], "limit", problems, "search")
    if methods["transition"] is not None:
        _accepts(methods["transition"], "raw", problems, "transition")
    if not isinstance(impl, TicketSource):
        problems.append("does not satisfy the TicketSource protocol")
    if problems:
        raise Nonconformant(f"{type(impl).__name__}: " + "; ".join(problems))


def assert_scm(impl: object) -> None:
    """Structurally an `Scm`: `diff` synchronous, `open_change` async and accepting `base=` —
    the parameter a backport passes, committed before third parties implemented without it."""
    problems: list[str] = []
    _method(impl, "diff", must_be_async=False, problems=problems)
    open_change = _method(impl, "open_change", must_be_async=True, problems=problems)
    if open_change is not None:
        for keyword in ("title", "body", "ticket", "workflow", "run_id", "base"):
            _accepts(open_change, keyword, problems, "open_change")
    if not isinstance(impl, Scm):
        problems.append("does not satisfy the Scm protocol")
    if problems:
        raise Nonconformant(f"{type(impl).__name__}: " + "; ".join(problems))
