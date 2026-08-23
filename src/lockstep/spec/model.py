"""The authored spec, as data.

Everything the compiler reads lands in these dataclasses. They are a pure function of files on
disk: no environment lookups, no runtime state. That is what makes `compile --check` meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceFile:
    """A definition file, with the content hash that ends up in generated provenance headers."""

    path: Path
    rel: str
    sha: str
    metadata: dict[str, Any]
    body: str

    def stamp(self) -> str:
        return f"{self.rel}@{self.sha}"


class StepKind(StrEnum):
    AGENT = "agent"
    SCRIPT = "script"
    BUILTIN = "builtin"
    COMMAND = "command"


@dataclass(frozen=True)
class Condition:
    """`(if --flag)` / `(if not --flag)` on a step."""

    flag: str
    negated: bool

    @property
    def input_name(self) -> str:
        return self.flag.lstrip("-").replace("-", "_")

    def key(self) -> str:
        return f"{'!' if self.negated else ''}{self.input_name}"


@dataclass(frozen=True)
class Foreach:
    """`foreach: item in {output_dir}/data.json`."""

    var: str
    source: str
    key_field: str = "key"


@dataclass
class Step:
    number: int
    label: str
    kind: StepKind
    target: str
    id: str = ""
    explicit_id: bool = False
    args: dict[str, str] = field(default_factory=dict)
    input: str = ""
    output: str = ""
    context_files: list[str] = field(default_factory=list)
    foreach: Foreach | None = None
    parallel: int = 0
    condition: Condition | None = None
    pre: str = ""
    post: str = ""
    on_failure: str = ""
    # github: block on the step
    targets: list[str] = field(default_factory=list)
    job_boundary: bool = False
    min_success_rate: float | None = None
    max_iterations: int = 0
    # Shell command producing a hash of live target state, so a redeploy invalidates cached output.
    fingerprint: str = ""

    def applies_to(self, backend: str) -> bool:
        """A step with no `targets:` applies everywhere; otherwise only where listed."""
        return not self.targets or backend in self.targets

    @property
    def fusible(self) -> bool:
        """Deterministic steps with no fan-out can share a job with their neighbours."""
        return (
            self.kind in (StepKind.SCRIPT, StepKind.BUILTIN)
            and self.foreach is None
            and not self.job_boundary
        )


@dataclass
class Parameter:
    name: str
    description: str = ""
    default: str | None = None

    @property
    def input_name(self) -> str:
        return self.name.replace("-", "_")

    @property
    def is_flag(self) -> bool:
        return str(self.default).lower() in ("true", "false")


@dataclass
class CommandGithub:
    triggers: dict[str, Any] = field(default_factory=dict)
    runs_on: str = ""
    timeout_minutes: int | None = None
    max_iterations: int = 1
    concurrency: dict[str, Any] | None = None
    # Step id whose `converged` output this command exposes to callers that unroll it.
    converged_from: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Command:
    name: str
    description: str = ""
    parameters: list[Parameter] = field(default_factory=list)
    guardrails: list[str] = field(default_factory=list)
    state: str = ""
    steps: list[Step] = field(default_factory=list)
    github: CommandGithub = field(default_factory=CommandGithub)
    src: SourceFile | None = None


@dataclass
class AgentGithub:
    engine: str = ""
    model: str = ""
    max_ai_credits: int | None = None
    network: list[str] = field(default_factory=list)
    safe_outputs: dict[str, Any] = field(default_factory=dict)
    timeout_minutes: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Agent:
    name: str
    description: str = ""
    model: str = ""
    provider: str = ""
    max_tool_turns: int = 0
    guardrails: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp: list[str] = field(default_factory=list)
    body: str = ""
    github: AgentGithub = field(default_factory=AgentGithub)
    src: SourceFile | None = None


@dataclass
class Enforce:
    """The half of a guardrail the substrate can enforce, rather than merely request."""

    permissions: str = ""
    network: str = ""
    deny_tools: list[str] = field(default_factory=list)


@dataclass
class Fragment:
    """A guardrail, skill, or context — a prompt layer that flattens into shared/*.md."""

    name: str
    kind: str
    body: str
    enforce: Enforce = field(default_factory=Enforce)
    src: SourceFile | None = None


@dataclass
class ProfileDeploy:
    mode: str = "external"
    services: dict[str, Any] = field(default_factory=dict)
    cli: dict[str, Any] = field(default_factory=dict)
    healthcheck: str = ""


@dataclass
class ProfileGithub:
    environment: str = ""
    secrets: list[str] = field(default_factory=list)
    vars: list[str] = field(default_factory=list)
    deploy: ProfileDeploy = field(default_factory=ProfileDeploy)


@dataclass
class Profile:
    name: str
    description: str = ""
    contexts: list[str] = field(default_factory=list)
    exclude_guardrails: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    github: ProfileGithub = field(default_factory=ProfileGithub)
    src: SourceFile | None = None


@dataclass
class McpServer:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    container: str = ""
    inline: bool = False


@dataclass
class Capabilities:
    actions: str = ""
    exec: str = ""
    compiler: str = ""
    gh_aw: str = ""


@dataclass
class TargetConfig:
    out: str = ".github/workflows"
    fuse_script_steps: bool = True
    default_runs_on: str = "ubuntu-24.04"
    shard_threshold: int = 20
    profiles: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    """pipeline.yaml — capability pins and target config."""

    spec_version: int = 1
    name: str = ""
    capabilities: Capabilities = field(default_factory=Capabilities)
    target: TargetConfig = field(default_factory=TargetConfig)
    per_run_ai_credits: int | None = None
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    src: SourceFile | None = None


@dataclass
class Spec:
    """Everything the compiler reads, resolved and validated."""

    root: Path
    manifest: Manifest
    commands: dict[str, Command] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    guardrails: dict[str, Fragment] = field(default_factory=dict)
    skills: dict[str, Fragment] = field(default_factory=dict)
    contexts: dict[str, Fragment] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    mcp_servers: dict[str, McpServer] = field(default_factory=dict)

    def compiled_profiles(self) -> list[Profile]:
        """Profiles to compile a workflow set for, in declared order."""
        names = self.manifest.target.profiles or sorted(self.profiles)
        return [self.profiles[n] for n in names if n in self.profiles]
