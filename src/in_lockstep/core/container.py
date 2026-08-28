"""Inversion of control.

Resolution order is deterministic and stated: explicit binds in your module, then plugins
registered through entry points, then shipped defaults. Because binding is ordinary code, an
organisation can publish a package that binds its defaults and a repository overrides one line of
it — and `in-lockstep ls` prints the resolved result, which is the "what will actually run" answer
that a YAML user gets by reading their file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

T = TypeVar("T")


class Scope(Enum):
    SINGLETON = "singleton"  # clients: SCM, provider pools
    CALL = "call"  # stateful adapters


class ResolutionError(Exception):
    """Nothing is bound for this interface, or a name does not exist."""


class Tier(Enum):
    """Where a binding came from. Lower ordinal wins."""

    EXPLICIT = 0  # lockstep.bind(...) in your module
    PLUGIN = 1  # entry points
    DEFAULT = 2  # shipped


@dataclass
class Binding:
    iface: type[Any]
    impl: Any
    name: str | None
    scope: Scope
    tier: Tier

    @property
    def key(self) -> tuple[type[Any], str | None]:
        return (self.iface, self.name)


class Container:
    def __init__(self) -> None:
        self._bindings: dict[tuple[type[Any], str | None], Binding] = {}
        self._singletons: dict[tuple[type[Any], str | None], Any] = {}

    def bind(
        self,
        iface: type[T],
        impl: T | type[T] | Callable[[], T],
        *,
        name: str | None = None,
        scope: Scope = Scope.SINGLETON,
        tier: Tier = Tier.EXPLICIT,
    ) -> None:
        binding = Binding(iface=iface, impl=impl, name=name, scope=scope, tier=tier)
        existing = self._bindings.get(binding.key)
        # A lower tier wins, and re-binding at the same tier is a deliberate override rather than
        # an error: that is how a repository overrides one line of an organisation's package.
        if existing is not None and existing.tier.value < tier.value:
            return
        self._bindings[binding.key] = binding
        self._singletons.pop(binding.key, None)

    def resolve(self, iface: type[T], name: str | None = None) -> T:
        key = (iface, name)
        binding = self._bindings.get(key)
        if binding is None and name is not None:
            raise ResolutionError(
                f"no binding named {name!r} for {iface.__name__}; "
                f"bound names: {sorted(n for i, n in self._bindings if i is iface and n)}"
            )
        if binding is None:
            raise ResolutionError(f"nothing bound for {iface.__name__}")

        if binding.scope is Scope.SINGLETON and key in self._singletons:
            return self._singletons[key]  # type: ignore[no-any-return]

        instance = binding.impl
        if isinstance(instance, type) or (callable(instance) and not hasattr(instance, "invoke")):
            instance = instance()

        if binding.scope is Scope.SINGLETON:
            self._singletons[key] = instance
        return instance  # type: ignore[no-any-return]

    def has(self, iface: type[Any], name: str | None = None) -> bool:
        return (iface, name) in self._bindings

    def resolved(self) -> list[Binding]:
        """Every binding, for `in-lockstep ls`. Config-as-code needs a reader."""
        return sorted(
            self._bindings.values(),
            key=lambda b: (b.iface.__name__, b.name or "", b.tier.value),
        )
