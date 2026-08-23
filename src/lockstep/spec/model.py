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
    """When a step runs.

    Two forms. `(if --flag)` / `(if not --flag)` asks about a workflow input, which is known before
    anything runs. `(if security in {state.pending})` asks about a value an earlier step computed,
    which is the only way to gate on work the pipeline decided for itself.
    """

    flag: str = ""
    negated: bool = False
    # Membership form: is `value` among the items of `step_id`'s `output`?
    value: str = ""
    step_id: str = ""
    output: str = ""

    @property
    def is_membership(self) -> bool:
        return bool(self.step_id)

    @property
    def input_name(self) -> str:
        return self.flag.lstrip("-").replace("-", "_")

    def key(self) -> str:
        if self.is_membership:
            return f"{self.step_id}.{self.output}:{self.value}"
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
    # A named value this step publishes for later steps to condition on. The step writes it to
    # `$GITHUB_OUTPUT`; the compiler lifts it to a job output.
    emits: str = ""
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
class Propose:
    """Agent output that should reach the repository as a reviewed pull request."""

    source: str = ""
    destination: str = ""
    branch: str = "pipeline/generated"
    title: str = "Generated pipeline artifacts"
    labels: str = "pipeline,generated"
    # Where the pull request lands. Defaults to the branch the run happened on; set it to publish
    # onto a branch whose contents are unrelated to the one that generated them.
    base: str = ""


@dataclass
class ChatCommand:
    """A slash command that lets a reviewer re-run a pipeline from a comment.

    A comment trigger runs on behalf of the repository, not the commenter, so who may invoke it is
    part of the declaration rather than an afterthought.
    """

    name: str = ""
    events: list[str] = field(default_factory=lambda: ["issue_comment"])
    roles: list[str] = field(default_factory=lambda: ["admin", "maintain", "write"])
    # The commenter's relationship to the repository, checked alongside their permission. On a
    # public repository the permission API is not a reliable trust signal — everybody can read a
    # public repository — whereas the association distinguishes a maintainer from a passer-by.
    associations: list[str] = field(default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"])
    arguments: list[str] = field(default_factory=list)
    reaction: str = "eyes"

    @property
    def slug(self) -> str:
        return self.name.lstrip("/")


@dataclass
class CommandGithub:
    triggers: dict[str, Any] = field(default_factory=dict)
    runs_on: str = ""
    timeout_minutes: int | None = None
    max_iterations: int = 1
    concurrency: dict[str, Any] | None = None
    # Step id whose `converged` output this command exposes to callers that unroll it.
    converged_from: str = ""
    propose: Propose | None = None
    command: ChatCommand | None = None
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
class ProfileReports:
    """Where run reports are published so they outlive the artifacts they were built from."""

    branch: str = ""
    path: str = "runs"
    retain: int = 90


@dataclass
class ProfileGithub:
    environment: str = ""
    secrets: list[str] = field(default_factory=list)
    vars: list[str] = field(default_factory=list)
    deploy: ProfileDeploy = field(default_factory=ProfileDeploy)
    reports: ProfileReports = field(default_factory=ProfileReports)


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
class Extensions:
    """What this pipeline adds to the framework.

    The compiler cannot import `pipeline-exec` — a generated repo installs the runtime, never the
    compiler — so it cannot discover a third-party command by itself. Declaring the names here is
    what lets a `builtin:` step reference one without the compiler having to guess.
    """

    builtins: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    """pipeline.yaml — capability pins and target config."""

    spec_version: int = 1
    name: str = ""
    capabilities: Capabilities = field(default_factory=Capabilities)
    target: TargetConfig = field(default_factory=TargetConfig)
    per_run_ai_credits: int | None = None
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    extensions: Extensions = field(default_factory=Extensions)
    src: SourceFile | None = None


# A pipeline added to an existing repository keeps its definitions here rather than adding eight
# top-level directories beside somebody's source. Discovered, not configured: the directory either
# exists or it does not.
LOCKSTEP_DIR = ".lockstep"


@dataclass
class Spec:
    """Everything the compiler reads, resolved and validated."""

    root: Path
    manifest: Manifest
    # True when the definitions live in `.lockstep/` rather than at the repository root.
    in_lockstep_dir: bool = False
    commands: dict[str, Command] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    guardrails: dict[str, Fragment] = field(default_factory=dict)
    skills: dict[str, Fragment] = field(default_factory=dict)
    contexts: dict[str, Fragment] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    mcp_servers: dict[str, McpServer] = field(default_factory=dict)

    @property
    def home(self) -> Path:
        """Where the definitions live: `.lockstep/` when it exists, the repository root otherwise."""
        return self.root / LOCKSTEP_DIR if self.in_lockstep_dir else self.root

    def repo_path(self, relative: str) -> str:
        """A definition-relative path, expressed from the repository root.

        Generated workflows run at the repository root, so every path they carry — a script to run, a
        file to hash for a cache key — has to be written from there rather than from `.lockstep/`.
        """
        if not self.in_lockstep_dir or not relative:
            return relative
        return f"{LOCKSTEP_DIR}/{relative}"

    def compiled_profiles(self) -> list[Profile]:
        """Profiles to compile a workflow set for, in declared order."""
        names = self.manifest.target.profiles or sorted(self.profiles)
        return [self.profiles[n] for n in names if n in self.profiles]
