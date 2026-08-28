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
        if existing is not None and existing.fn is not func:
            raise DuplicateWorkflow(
                f"workflow id {workflow_id!r} is already registered by {existing.module}.{existing.name}"
            )
        _REGISTRY[workflow_id] = Registered(id=workflow_id, fn=func, module=getattr(func, "__module__", ""))
        _IDS_BY_FN[func] = workflow_id
        return func

    if fn is not None:
        return register(fn)
    return register


def registered() -> list[Registered]:
    return sorted(_REGISTRY.values(), key=lambda r: r.id)


def get(workflow_id: str) -> Registered | None:
    return _REGISTRY.get(workflow_id)


def id_of(fn: Workflow) -> str | None:
    """The stable id a function was registered under, if any."""
    return _IDS_BY_FN.get(fn)


def clear() -> None:
    """Test helper. The registry is process-global by design; tests must not leak between them."""
    _REGISTRY.clear()
    _IDS_BY_FN.clear()
