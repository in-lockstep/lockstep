"""Tool exposure.

`ToolSet` IS the dispatch table. That is the whole design: the upstream loop handed whatever name
the model emitted straight to an MCP manager which resolved it across whichever server happened to
provide it, so a name collision between two servers was a silent hijack and there was no point at
which an allowlist could be consulted. Here, a name the set does not contain cannot be dispatched,
because there is nothing to dispatch it to.

Keys are `(server, tool)`. Ambiguous bare names are refused at construction rather than resolved
at call time — the moment two servers offer `read_file`, the question of which one the model meant
has no good answer.

Defaults are read-only. Anything that writes or executes is deny-by-default, and a tool whose
capability was never declared is treated as the most dangerous thing it could be, not the least.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..core.verbs import Capability
from ..llm.types import ToolDefinition

BUILTIN_SERVER = "builtin"


@dataclass(frozen=True)
class Tool:
    server: str
    name: str
    description: str = ""
    parameters: dict[str, object] = field(default_factory=dict)
    # Declared by the repository, never taken from a server's own hint: a server asserting it is
    # read-only is a claim by the thing being constrained.
    capabilities: frozenset[Capability] = frozenset()

    @property
    def key(self) -> tuple[str, str]:
        return (self.server, self.name)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=dict(self.parameters))


class AmbiguousTool(Exception):
    """Two servers offer the same bare name. Which one the model meant is undecidable."""


class ToolNotAllowed(Exception):
    """The model asked for something the set does not contain."""


@dataclass
class ToolSet:
    tools: dict[tuple[str, str], Tool] = field(default_factory=dict)

    @classmethod
    def of(cls, *tools: Tool) -> ToolSet:
        set_ = cls()
        for tool in tools:
            set_.add(tool)
        return set_

    @classmethod
    def none(cls) -> ToolSet:
        return cls()

    def add(self, tool: Tool) -> None:
        clash = [t for t in self.tools.values() if t.name == tool.name and t.server != tool.server]
        if clash:
            raise AmbiguousTool(
                f"tool {tool.name!r} is offered by both {clash[0].server!r} and {tool.server!r}; "
                "a model emits a bare name, so this cannot be resolved at call time"
            )
        self.tools[tool.key] = tool

    def __or__(self, other: ToolSet) -> ToolSet:
        merged = ToolSet(dict(self.tools))
        for tool in other.tools.values():
            merged.add(tool)
        return merged

    def allow(self, *names: str) -> ToolSet:
        """Narrow to an explicit allowlist."""
        return ToolSet({k: v for k, v in self.tools.items() if v.name in names})

    def deny(self, *names: str) -> ToolSet:
        return ToolSet({k: v for k, v in self.tools.items() if v.name not in names})

    def resolve(self, name: str) -> Tool:
        """What the model asked for, or a refusal. There is no third outcome."""
        matches = [t for t in self.tools.values() if t.name == name]
        if not matches:
            available = ", ".join(sorted(t.name for t in self.tools.values())) or "(none)"
            raise ToolNotAllowed(f"tool {name!r} is not in this set; available: {available}")
        if len(matches) > 1:  # pragma: no cover - prevented at construction
            raise AmbiguousTool(name)
        return matches[0]

    def definitions(self) -> list[ToolDefinition]:
        return [t.definition() for t in sorted(self.tools.values(), key=lambda t: t.name)]

    def capabilities(self) -> frozenset[Capability]:
        caps: set[Capability] = set()
        for tool in self.tools.values():
            caps |= tool.capabilities
        return frozenset(caps)

    @property
    def read_only(self) -> bool:
        """Read-only means it does not mutate. It does NOT mean it cannot transmit."""
        dangerous = {
            Capability.WRITES_FILES,
            Capability.EXECUTES_CODE,
            Capability.REACHES_NETWORK,
        }
        return not (self.capabilities() & dangerous)

    def names(self) -> list[str]:
        return sorted(t.name for t in self.tools.values())


def undeclared_is_dangerous(tool: Tool) -> Tool:
    """A tool with no declared capability is assumed to reach the network.

    Failing closed here is the difference between a policy and a suggestion: an MCP server that
    simply never declared itself would otherwise be classified as harmless and drop a whole run
    below the threshold at which egress control becomes mandatory.
    """
    if tool.capabilities:
        return tool
    return replace(tool, capabilities=frozenset({Capability.REACHES_NETWORK}))
