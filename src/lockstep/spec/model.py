"""The authored spec, as data.

Everything the compiler reads lands in these dataclasses. They are a pure function of files on
disk: no environment lookups, no runtime state. That is what makes `compile --check` meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    # Run this step with the compiler installed, outside the executor container.
    #
    # The executor image deliberately does not contain `lockstep`: a runtime that could recompile
    # would be a runtime that could change what runs. Exactly one kind of pipeline legitimately
    # needs it — the one whose job is to re-pin an upstream and propose the recompile — and it must
    # put the result through a pull request rather than committing it.
    uses_compiler: bool = False

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
    # Push to one stable branch and update the open pull request rather than opening another. For
    # work that supersedes itself: three upstream bumps in a week should leave one pull request
    # showing the current state, and a reviewer who commented on it keeps their thread.
    reuse_branch: bool = False


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


# What a consumer of an inherited agent may move, and what it may never move.
#
# The rule is one sentence: a band governs cost and latency, it never governs capability. Credits,
# how long a job may take, which model, which runner — all dials on the same machine. Permissions,
# tools, network, turns, guardrails and the body are the surface upstream's evals were written
# against and a security review signed off; a consumer who needs a different one needs a different
# agent, and that conversation belongs upstream rather than in a config key.
#
# Three fields, not because three is a principled number but because these three demonstrably reach
# the emitted workflow. `runs-on` looked like an obvious fourth and is not: an agentic workflow's
# runner does not come from the agent, so banding it would have published a dial connected to
# nothing. Add the fourth when somebody asks and it can be shown to move something.
BANDABLE = ("max-ai-credits", "timeout-minutes", "model")


@dataclass
class Band:
    """How far an inherited field may be moved, as the publishing repository declared it."""

    default: Any
    minimum: int | None = None
    maximum: int | None = None
    allow: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.allow:
            return " or ".join(repr(value) for value in self.allow)
        if self.minimum is not None and self.maximum is not None:
            return f"{self.minimum}\u2013{self.maximum}"
        if self.maximum is not None:
            return f"at most {self.maximum}"
        if self.minimum is not None:
            return f"at least {self.minimum}"
        return "any value"

    def permits(self, value: Any) -> bool:
        if self.allow:
            return str(value) in self.allow
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False
        if self.minimum is not None and number < self.minimum:
            return False
        return not (self.maximum is not None and number > self.maximum)


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
    # The alias this arrived under, empty for an agent the repository wrote itself.
    inherited_from: str = ""
    body: str = ""
    github: AgentGithub = field(default_factory=AgentGithub)
    src: SourceFile | None = None
    # Fields this agent publishes as movable, keyed by the name a consumer would write.
    bands: dict[str, Band] = field(default_factory=dict)
    # What a consumer actually moved, recorded so a fleet can be read from the compile manifest.
    tuned: dict[str, Any] = field(default_factory=dict)


@dataclass
class Enforce:
    """The half of a guardrail the substrate can enforce, rather than merely request."""

    permissions: str = ""
    network: str = ""
    deny_tools: list[str] = field(default_factory=list)
    # Ceilings. A band asks how far a consumer may move a dial on an agent this organization wrote;
    # a ceiling asks how high a consumer's dial may go on an agent it has never seen. Different
    # question, so deliberately not a band — and the reason it lives here is that a sealed guardrail
    # reaches every agent in the repository, including the ones the consumer authored.
    #
    # `None` means unset, which is what lets the merge take the lowest of several guardrails rather
    # than the last one read.
    max_turns: int | None = None
    max_ai_credits: int | None = None
    # The one that actually bounds a bill: per-agent ceilings do not, because a consumer can add
    # more agents. Checked against `budgets.per_run_ai_credits`, which is the consumer's own number.
    per_run_ai_credits: int | None = None
    # A ceiling on repetition rather than on a single run. `per_run_ai_credits` bounds one execution
    # and says nothing about a chat-ops command somebody triggers four hundred times in an
    # afternoon; this is the axis that catches that. gh-aw enforces it per agent workflow per day,
    # before the agent starts.
    daily_ai_credits: int | None = None
    # Scan an agent's input for hidden instructions before the agent reads it: "warn" reports,
    # "block" fails the run. The enforced half of "treat input as data, never as instructions",
    # which every pipeline here has been carrying as a sentence in a prompt.
    scan_input: str = ""


@dataclass
class Fragment:
    """A guardrail, skill, or context — a prompt layer that flattens into shared/*.md."""

    name: str
    kind: str
    body: str
    enforce: Enforce = field(default_factory=Enforce)
    src: SourceFile | None = None
    # A sealed guardrail is a standard rather than a default: it reaches every agent without being
    # named, no profile may exclude it, and no local file may take its name. Only meaningful on an
    # inherited guardrail — a pipeline sealing its own is sealing it against itself.
    sealed: bool = False
    # The alias this arrived under, empty for definitions the repository wrote itself.
    inherited_from: str = ""


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
    # Where the executor image is published. A registry choice, so it belongs here rather than in
    # the lock file, which records only what was resolved. Any registry works: ghcr.io, quay.io, a
    # private one — the compiler writes it verbatim into `container:`.
    exec_image: str = ""
    compiler: str = ""
    gh_aw: str = ""


@dataclass
class InheritsAuth:
    """How this repository reads an upstream that is private.

    A consumer's own `GITHUB_TOKEN` can only read the repository it belongs to, so a private
    upstream needs a credential from somewhere else. Two shapes, and the App is the one to reach
    for: an installation token is minted per run, expires in an hour, and is scoped to the
    repositories the App was installed on. A PAT is somebody's account, does not expire on its own,
    and outlives their employment.
    """

    # A repository or environment secret holding a token.
    token: str = ""
    # A GitHub App: `app-id` names a variable, `private-key` a secret.
    app_id: str = ""
    private_key: str = ""

    @property
    def declared(self) -> bool:
        return bool(self.token or (self.app_id and self.private_key))

    @property
    def uses_app(self) -> bool:
        return bool(self.app_id and self.private_key)


@dataclass
class Sandbox:
    """What a deterministic step may do, beyond what it is asked to do.

    `enforce:` bounds an *agent* — its permissions, egress, tools, turns and credits. It bounds
    nothing about a `script:` step, which is the step actually running arbitrary code. A pipeline
    could therefore restrict what a model reaches and not what its own scripts do, which is the
    wrong way round.

    The floor is applied to every job that runs in the executor container and is not declared:
    capabilities dropped, no privilege escalation. Both are kernel-enforced and neither is something
    a correct script needs. What a pipeline declares here only ever *widens* that, which is why the
    semantic diff treats a change to it as a security-surface change.
    """

    # Linux capabilities to add back after dropping all of them. Named individually, because
    # `--cap-add=ALL` would be a way to write "no sandbox" that does not look like one.
    capabilities: list[str] = field(default_factory=list)
    memory: str = ""
    cpus: str = ""
    pids: int | None = None
    # The shipped executor image runs as root and GitHub mounts the workspace for root. Declaring a
    # user is supported and is not the default, because a default nobody has run is a guess.
    user: str = ""

    def options(self) -> str:
        """The `container.options` string. The floor first, then whatever was declared."""
        parts = ["--cap-drop=ALL", "--security-opt=no-new-privileges"]
        parts += [f"--cap-add={name}" for name in self.capabilities]
        if self.memory:
            parts.append(f"--memory={self.memory}")
        if self.cpus:
            parts.append(f"--cpus={self.cpus}")
        if self.pids is not None:
            parts.append(f"--pids-limit={self.pids}")
        if self.user:
            parts.append(f"--user={self.user}")
        return " ".join(parts)


@dataclass
class EvalConfig:
    """How this pipeline runs its eval suites.

    `judge` names an agent in this pipeline, not one the framework ships. A framework-provided
    prompt deciding whether your agents pass is a strong opinion to impose, and it could not be
    evaluated without evaluating the thing that evaluates it. Without a judge the deterministic
    half still runs and rubrics are reported as undecided, which is the honest answer.
    """

    judge: str = ""
    min_pass_rate: float | None = None
    # A floor on the mean of the scored rubrics a judge decided. Separate from `min_pass_rate`
    # because it catches the regression a pass rate cannot: every case still passing, all of them
    # answered less well than they were last month.
    min_score: float | None = None
    # Evals cost credits, so the suite is dispatched or triggered by a change to the prompt layers
    # it covers — never on every push. A prompt change is exactly what an eval exists to gate.
    on_prompt_change: bool = True


@dataclass
class OtelConfig:
    """Where a run's consumption goes, and what a credit costs.

    Off unless declared. A pipeline that has not been told a rate cannot report a cost, and one
    that reported $0.00 because nobody set a table would be worse than one that reported nothing.

    `pricing` maps a model to dollars per credit. Longest-prefix matched, so `claude-sonnet-4-6`
    prices `claude-sonnet-4-6-20260101` too — a table that had to name every dated snapshot would
    silently stop pricing things the day a provider published one.
    """

    export: str = ""  # "" (off) | "artifact" | "endpoint" | "both"
    # The collector's **base** URL, per the OTLP convention for `OTEL_EXPORTER_OTLP_ENDPOINT`: each
    # signal appends its own path. Two exporters read this — gh-aw's, for the agent spans, and the
    # meter's, for the run metrics — and giving them a signal-specific URL would send one of them
    # to the wrong place.
    endpoint: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # The collector's hostname, for the egress allow-list. An agent's network policy is computed and
    # closed, so a host nothing added is a host the firewall drops — silently, because an exporter
    # that cannot reach its collector does not fail a run. Derived from `endpoint` when that is a
    # literal URL; declared here when it is a `${NAME}` an organization would rather not inline.
    host: str = ""
    service_name: str = ""
    pricing: dict[str, float] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.export in ("artifact", "endpoint", "both")

    @property
    def to_artifact(self) -> bool:
        return self.export in ("artifact", "both")

    @property
    def to_endpoint(self) -> bool:
        return self.export in ("endpoint", "both")

    @property
    def collector_host(self) -> str:
        """The host to open egress to, from the endpoint when it is a literal."""
        if self.host:
            return self.host
        from urllib.parse import urlparse

        parsed = urlparse(self.endpoint)
        # A `${...}` endpoint parses to nothing useful, which is the case `host` exists for.
        return parsed.hostname or ""


@dataclass
class TargetConfig:
    out: str = ".github/workflows"
    fuse_script_steps: bool = True
    default_runs_on: str = "ubuntu-24.04"
    shard_threshold: int = 20
    profiles: list[str] = field(default_factory=list)
    sandbox: Sandbox = field(default_factory=Sandbox)
    # Repository paths, outside the pipeline's own directories, that can change the compiled output.
    # The drift gate triggers on the spec because normally the spec is the only input — the compiler
    # is a pinned release and cannot move under a pull request. A repository that builds its own
    # compiler, or keeps an extension elsewhere in the tree, has a second input, and a gate that
    # cannot see it passes on the change most worth checking. Written repository-relative, because
    # what they name is outside the pipeline.
    watch: list[str] = field(default_factory=list)


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
class CommandUse:
    """How this repository instantiates one command: its own, or one it inherits."""

    # `<alias>` or `<alias>/<command>`. Empty for a command this repository defines itself.
    source: str = ""
    add_guardrails: list[str] = field(default_factory=list)
    add_skills: list[str] = field(default_factory=list)
    # agent name -> {field: value}, checked against the bands that agent publishes.
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Manifest:
    """pipeline.yaml — capability pins and target config."""

    spec_version: int = 1
    name: str = ""
    # alias -> `github.com/<owner>/<repo>@<ref>`, or a path for local development.
    inherits: dict[str, str] = field(default_factory=dict)
    capabilities: Capabilities = field(default_factory=Capabilities)
    target: TargetConfig = field(default_factory=TargetConfig)
    per_run_ai_credits: int | None = None
    # Per agent workflow, per day — deliberately named for what gh-aw actually enforces. A repository
    # with seven agents under a 5000 ceiling can spend 35,000 in a day, and a key called
    # `daily_ai_credits` would have quietly said otherwise. `show-surface` prints that product.
    per_agent_daily_ai_credits: int | None = None
    evals: EvalConfig = field(default_factory=EvalConfig)
    otel: OtelConfig = field(default_factory=OtelConfig)
    inherits_auth: InheritsAuth = field(default_factory=InheritsAuth)
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    uses: dict[str, CommandUse] = field(default_factory=dict)
    extensions: Extensions = field(default_factory=Extensions)
    src: SourceFile | None = None


# Where `lockstep fetch` materializes what this repository inherits. Under `.pipeline/` because it
# is resolved state rather than authored definition, and gitignored for the same reason a virtualenv
# is: the lock file is the thing worth committing.
INHERITED_DIR = ".pipeline/inherited"


# A pipeline added to an existing repository keeps its definitions here rather than adding eight
# top-level directories beside somebody's source. Discovered, not configured: the directory either
# exists or it does not.
LOCKSTEP_DIR = ".lockstep"


@dataclass(frozen=True)
class CapabilityUse:
    """What a pipeline's output references, so readiness is asked about that and nothing else."""

    actions: bool
    executor: bool
    gh_aw: bool


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

    def sealed_guardrails(self) -> list[Fragment]:
        """Inherited standards, in the order this repository declares its upstreams.

        Every agent gets these whether it asks or not, so the order they arrive in is the order they
        are inlined in — and position is a property that carries meaning: a later instruction reads
        as a refinement of an earlier one. Sorting the aliases alphabetically made that meaning
        accidental, decided by a name. `inherits:` is a list the author writes, so it is the list
        that decides: declare the broadest standard first and it stays first.

        Within one upstream the order is by name, because a directory of guardrails has no declared
        order and only the repository publishing them can rename a file.
        """
        order = {alias: index for index, alias in enumerate(self.manifest.inherits)}
        return sorted(
            (fragment for fragment in self.guardrails.values() if fragment.sealed),
            key=lambda fragment: (order.get(fragment.inherited_from, len(order)), fragment.name),
        )

    def repo_path(self, relative: str) -> str:
        """A definition-relative path, expressed from the repository root.

        Generated workflows run at the repository root, so every path they carry — a script to run, a
        file to hash for a cache key — has to be written from there rather than from `.lockstep/`.
        """
        if not self.in_lockstep_dir or not relative:
            return relative
        return f"{LOCKSTEP_DIR}/{relative}"

    def capabilities_used(self) -> CapabilityUse:
        """Which capabilities this pipeline's compiled output will actually name.

        A pin is a promise about an artifact a workflow references. Requiring one for a capability
        no job mentions is a red gate with nothing behind it — and a pipeline is legitimately in
        that state when its work is all compiler steps: a repository whose only pipeline is its own
        drift gate pulls no container and calls no composite action.
        """
        use = CapabilityUse(actions=False, executor=False, gh_aw=False)
        for command in self.commands.values():
            if command.github.propose or command.github.command:
                # propose-pr and command-gate are composite actions.
                use = replace(use, actions=True)
            for step in command.steps:
                if step.kind is StepKind.AGENT:
                    use = replace(use, gh_aw=True)
                    if step.foreach:
                        # `fanout` is injected into the job that produces the items.
                        use = replace(use, actions=True)
                    continue
                # Every job built from steps restores and saves the workspace around them.
                use = replace(use, actions=True)
                if not step.uses_compiler:
                    use = replace(use, executor=True)
        return use

    def external_actions_used(self) -> set[str]:
        """Third-party actions this pipeline's output will reference.

        Checkout is in every generated workflow. The App-token action is in exactly one, and only
        when a repository declares that its upstreams are private — so requiring a pin for it
        everywhere would be the same red-gate-with-nothing-behind-it that `capabilities_used`
        exists to avoid.
        """
        used = {"actions/checkout"}
        if self.manifest.inherits_auth.uses_app:
            used.add("actions/create-github-app-token")
        if self.manifest.otel.enabled:
            # Read off the manifest alone, like the App-token line above. `lockstep pin` runs before
            # `fetch` and therefore sees a manifest without its agents loaded; a condition that also
            # asked about agents would answer differently in `pin` and in `doctor`, and the pin
            # would be missing exactly where it is needed.
            used.add("actions/download-artifact")
            if self.manifest.otel.to_artifact:
                used.add("actions/upload-artifact")
        return used

    def compiled_profiles(self) -> list[Profile]:
        """Profiles to compile a workflow set for, in declared order."""
        names = self.manifest.target.profiles or sorted(self.profiles)
        return [self.profiles[n] for n in names if n in self.profiles]
