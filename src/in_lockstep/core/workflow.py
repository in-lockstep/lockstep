"""Workflow registration.

`@workflow` registers a plain async function; control flow is native Python. A workflow carries a
**stable id** from the start, and park records will reference that id rather than a function name —
renaming a function must not strand work that is already waiting on a human.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, overload

Workflow = Callable[..., Awaitable[Any]]
F = TypeVar("F", bound=Workflow)


class DuplicateWorkflow(Exception):
    """Two workflows claim the same id. Silently keeping one would strand the other."""


@dataclass(frozen=True)
class Registered:
    id: str
    fn: Workflow
    module: str

    @property
    def name(self) -> str:
        return getattr(self.fn, "__name__", self.id)


_REGISTRY: dict[str, Registered] = {}
# Reverse lookup, kept beside the registry rather than stamped onto the function:
# a park record references a workflow id, so resolving a function back to its id has
# to work without mutating user code.
_IDS_BY_FN: dict[Workflow, str] = {}


@overload
def workflow(fn: F) -> F: ...


@overload
def workflow(*, id: str) -> Callable[[F], F]: ...


def workflow(fn: F | None = None, *, id: str | None = None) -> F | Callable[[F], F]:
    """Register a workflow. `@workflow` or `@workflow(id="fix-ci/after-review")`."""

    def register(func: F) -> F:
        workflow_id = id or func.__name__
        existing = _REGISTRY.get(workflow_id)
        if existing is not None and not _same_declaration(existing.fn, func):
            raise DuplicateWorkflow(
                f"workflow id {workflow_id!r} is already registered by {existing.module}.{existing.name}"
            )
        _REGISTRY[workflow_id] = Registered(id=workflow_id, fn=func, module=getattr(func, "__module__", ""))
        _IDS_BY_FN[func] = workflow_id
        return func

    if fn is not None:
        return register(fn)
    return register


def inject_ports(fn: Workflow, ctx: Any, provided: dict[str, Any]) -> dict[str, Any]:
    """Resolve container-bound ports into a workflow's typed parameters.

    A workflow signature is allowed to name what it needs — `tickets: TicketSource` — and the
    dispatcher fills it from `ctx.container`, so the body starts with the port instead of with
    `ctx.container.resolve(TicketSource)`. Only a parameter whose annotation is a class the
    container has a binding for is filled; everything else (CLI strings, defaults) is left alone,
    and a parameter the caller already supplied is never overridden. A workflow stays a plain
    function: a test that passes its ports explicitly bypasses this entirely.
    """
    container = getattr(ctx, "container", None)
    if container is None:
        return dict(provided)

    import inspect
    import typing

    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 - an unresolvable annotation is a signature problem, and the
        # TypeError the call itself raises names the workflow; guessing here would not.
        return dict(provided)

    out = dict(provided)
    for name in inspect.signature(fn).parameters:
        if name == "ctx" or name in out:
            continue
        hint = hints.get(name)
        if isinstance(hint, type) and container.has(hint):
            out[name] = container.resolve(hint)
    return out


def injectable_parameters(fn: Workflow, ctx: Any) -> set[str]:
    """Which of a workflow's parameters `inject_ports` would fill — for error messages that list
    only the arguments a caller actually supplies."""
    container = getattr(ctx, "container", None)
    if container is None:
        return set()

    import inspect
    import typing

    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 - same contract as inject_ports
        return set()
    return {
        name
        for name in inspect.signature(fn).parameters
        if name != "ctx" and isinstance(hints.get(name), type) and container.has(hints[name])
    }


def _same_declaration(existing: Workflow, incoming: Workflow) -> bool:
    """Whether these are the same `def`, not merely the same object.

    Object identity was the test, and it made re-importing a module a conflict with itself. Any
    host that loads `lockstep.py` more than once in a process hits that: a long-lived worker, a
    test harness, two CLI invocations sharing an interpreter. Re-executing a declaration is not two
    workflows claiming one id; it is one declaration evaluated twice, and refusing it turns
    "configuration is code" into "configuration is code you may import exactly once".

    The real conflict — two different functions claiming one id — still raises, because that is the
    case where silently keeping one strands the other.
    """
    if existing is incoming:
        return True
    return getattr(existing, "__module__", None) == getattr(incoming, "__module__", object()) and getattr(
        existing, "__qualname__", None
    ) == getattr(incoming, "__qualname__", object())


def registered() -> list[Registered]:
    return sorted(_REGISTRY.values(), key=lambda r: r.id)


def get(workflow_id: str) -> Registered | None:
    return _REGISTRY.get(workflow_id)


def id_of(fn: Workflow) -> str | None:
    """The stable id a function was registered under, if any."""
    return _IDS_BY_FN.get(fn)


def clear() -> None:
    """Empty the registry. Almost always the wrong tool — prefer `snapshot`/`restore`.

    The framework's own `selfcheck` is registered when `in_lockstep.cli` is imported, so clearing
    removes it for the rest of the process and every later test finds an empty registry. Both
    callers this ever had were wrong in exactly that way, and the symptom was invisible until the
    dispatcher stopped special-casing `selfcheck` — at which point it became
    `unknown workflow 'selfcheck'` in tests that had nothing to do with workflows.
    """
    _REGISTRY.clear()
    _IDS_BY_FN.clear()


def snapshot() -> tuple[dict[str, Registered], dict[Workflow, str]]:
    """What is registered right now, for a test that is about to register more.

    `clear()` is the wrong tool for that: the framework's own `selfcheck` is registered when
    `cli` is imported, so clearing after a test removes it for the rest of the process and every
    later test finds an empty registry. Restoring a snapshot removes what the test added and
    leaves what it found.
    """
    return dict(_REGISTRY), dict(_IDS_BY_FN)


def restore(state: tuple[dict[str, Registered], dict[Workflow, str]]) -> None:
    registry, by_fn = state
    _REGISTRY.clear()
    _REGISTRY.update(registry)
    _IDS_BY_FN.clear()
    _IDS_BY_FN.update(by_fn)
