# in-lockstep — An Agentic SDLC Framework for Python

**Design document, v0.5** — the design below is the *post-review* state. Seven rounds of persona review were run: rounds 1–2 against the original draft; round 3 against human-boundary splitting, notifications, and the rename from the "ai-sdlc" working name; round 4 against fan-out/fan-in, verb strategies, skills, audience resolution, and multi-repo workspaces; and rounds 5-7 against an implementation plan, which forced the amendments in **§17**. Every finding, resolution, and sign-off is in the **Design Review Record** (§16) and the v0.5 record (§17.12).

**Naming.** The project is **in-lockstep**: distribution and CLI `in-lockstep`, import package `in_lockstep` (hyphens are illegal in Python module names). The name states the operating principle — the automation and the humans advance together, synchronizing at every human boundary (§13).

---

## 1. Philosophy and principles

in-lockstep treats the software development lifecycle itself as a program. Where Infrastructure-as-Code turned infrastructure into declarative text, in-lockstep goes one step further: the SDLC is not *described* in code, it *is* code — "code as code." There is no compilation target, no generated YAML, no intermediate configuration layer. The Python module that defines your lifecycle is the thing that runs.

This buys the full semantics of a programming language for process definition:

- **Versioning and diffing.** A change to your review policy is a reviewable diff in a PR, with blame, history, and rollback via git.
- **Type semantics.** You `extend` a verb by subclassing, `implement` an interface by satisfying a `Protocol`, `override` behavior by rebinding in the container, and `declare` workflows with decorators.
- **Patterns.** Inversion of control (a DI container resolves every verb), chain of actors (middleware pipelines around every invocation), adapters (every verb and every external system is behind an interface), singleton-scoped services (SCM clients, provider pools), strategy (model routing), and plain functions where a pattern would be ceremony.

Seven principles govern the design:

1. **Runnable, never rendered.** `python -m` or `in-lockstep run` executes your `lockstep.py` directly. The framework never emits YAML/JSON. (CI systems require a host-side YAML *trampoline* to invoke the CLI — see §11; that file belongs to the CI host, is ~5 lines, and contains zero lifecycle logic.)
2. **Deterministic by default, AI by declaration.** `build`/`test`/`validate`/`run` default to deterministic adapters over real tools. `implement`/`fix`/`review`/`triage`/`debug` default to AI-backed adapters — but any verb can be bound to either kind. The verb interface doesn't know or care.
3. **Everything invocable is interceptable.** All verb calls flow through one dispatch path (`ctx.do`), so cross-cutting concerns — tracing, budgets, approvals, retries, recording — are middleware, not features.
4. **Failure is data.** A red test run is a domain outcome, not an exception. *(changed in v0.2: full outcome taxonomy, §4.3.)*
5. **Untrusted by default.** Repo content, ticket text, and CI logs are attacker-influenced inputs to models with tools. The security model (§10) is structural, not advisory. *(added in v0.2.)*
6. **The framework improves through its own lifecycle.** Evaluation signals harvested from git/PR history drive a learning loop whose only output is a human-reviewed PR against your own repo (§8).
7. **Runs never wait on humans.** A workflow run ends at every human boundary: state externalizes to the ledger and the system of record, people are notified (§14), and a continuation workflow resumes on the human's event (§13). No durable-execution engine, no determinism rules on user code, no server. *(added in v0.3.)*

## 2. What it looks like to use

A project adopts in-lockstep by adding one module at the repo root. This example is the complete "configuration" — note that it is executable Python, not a manifest:

```python
# lockstep.py — the lifecycle definition for this repo
from in_lockstep import Lockstep, workflow, Verb, HumanBoundary, Resumption, Verdict
from in_lockstep.notify import Event, Notify
from datetime import timedelta
from in_lockstep.verbs import Build, Test, Validate, Debug, Fix, Review, Run
from in_lockstep.adapters.pytest import PytestTest
from in_lockstep.adapters.ruff import RuffValidate
from in_lockstep.adapters.uv import UvBuild
from in_lockstep.ai import OpenAICompatibleProvider, prompts
from in_lockstep.middleware import CostBudget, ApprovalGate, RecordReplay, otel

lockstep = Lockstep.detect()   # sniffs repo, SCM host, CI env, ticket source from the environment

# -- deterministic verbs: bind adapters over real tools ----------------------
lockstep.bind(Build, UvBuild())
lockstep.bind(Test, PytestTest(args=["-q", "--maxfail=25"]))
lockstep.bind(Validate, RuffValidate() | Validate.types("mypy"))   # chain of validators

# -- AI plumbing: providers and per-verb model routing -----------------------
lockstep.providers.register("local",
    OpenAICompatibleProvider(base_url="http://localhost:1234/v1"))  # LM Studio / Ollama / vLLM / gateway
lockstep.models.route(Verb.TRIAGE, "local:qwen3-8b")            # cheap, private, good enough
lockstep.models.route(Verb.IMPLEMENT, "anthropic:claude-sonnet-4-6")
lockstep.models.route(Verb.REVIEW, "anthropic:claude-sonnet-4-6")
lockstep.strategies.default(Verb.IMPLEMENT, "tdd")   # per-verb prompt strategies (§5.7)

# -- extend a shipped prompt: prompts are classes, versioned in git ----------
class OurReviewPrompt(prompts.ReviewPrompt):
    version = "team-3"
    emphasis = "data-layer invariants; SQLAlchemy 2.x session discipline; no bare excepts"

lockstep.bind_prompt(Review, OurReviewPrompt)

# -- cross-cutting behavior is middleware (chain of actors) ------------------
lockstep.middleware += [
    otel(),                                     # spans + metrics for every call
    CostBudget(usd=2.00, per="run"),            # hard stop on spend
    RecordReplay(mode="auto"),                  # cassette model IO for offline reruns
    ApprovalGate(when=lambda c: c.verb is Verb.RUN),  # humans gate execution of generated code
]

# -- notifications: route lifecycle events to channels (§14) -----------------
lockstep.notify.route(Event.PARKED, to=Notify.slack("#eng-ai"))
lockstep.notify.route(Event.PARK_EXPIRING, to=Notify.pagerduty(service="platform-oncall"))

# -- a workflow ends at the human boundary; a continuation resumes it (§13) --
@workflow
async def fix_ci(ctx, failure):
    diag = await ctx.do(Debug, failure.context())
    if diag.blocked:
        return diag
    change = await ctx.do(Fix, diag.value.as_fix_spec())
    report = await ctx.do(Test, change.value.test_spec())
    if report.failed:
        return await ctx.escalate(diag, change, report)   # ticket comment + human handoff
    pr = await ctx.scm.open_change(change.value,
                                   title=f"fix: {diag.value.summary}",
                                   ticket=failure.ticket)
    return await ctx.park(HumanBoundary.pr_review(pr),        # run ENDS here, status=PARKED;
                          resume="fix-ci/after-review",       # state → ledger + PR marker
                          payload={"diag": diag.value},
                          expires=timedelta(days=5))

@workflow(id="fix-ci/after-review")                            # fresh run, fired by the review event
async def after_review(ctx, r: Resumption):
    if r.verdict is Verdict.CHANGES_REQUESTED:
        change = await ctx.do(Fix, FixSpec.from_review(r.event, r.payload["diag"]))
        return await ctx.update_change(r.change_request, change.value)   # pushes, re-parks
    return Outcome.succeeded(r.change_request)                 # approved: branch protection merges
```

Run it anywhere:

```
in-lockstep run fix_ci --trigger ci:last-failure       # locally
in-lockstep run fix_ci --offline --replay run-8f31     # deterministic re-run from cassettes
```

The identical command runs in GitHub Actions or GitLab CI via a thin trampoline (§11).

## 3. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Workflows (user code: lockstep.py — plain async Python)               │
├────────────────────────────────────────────────────────────────────┤
│  Verbs (interfaces): build test validate run fix implement         │
│                      review triage debug                           │
├──────────────────────────┬─────────────────────────────────────────┤
│  Deterministic adapters  │  AI adapters → AiInvoker                │
│  (pytest, ruff, make,    │  ┌───────────────────────────────────┐  │
│   uv, docker, shell…)    │  │ Prompt · Context · ToolSet(MCP)   │  │
│                          │  │ ModelRouter → Provider ← Auth     │  │
│                          │  └───────────────────────────────────┘  │
├──────────────────────────┴─────────────────────────────────────────┤
│  Dispatch core: Container (IoC) · Middleware chain · Outcome ·     │
│  RunContext · Checkpoints                                          │
├────────────────────────────────────────────────────────────────────┤
│  Platform: Scm (git/GitHub/GitLab) · CiEnvironment · TicketSource  │
│  (Jira/GH/GL) · Ledger (traceability) · EvalStore · OTel           │
└────────────────────────────────────────────────────────────────────┘
```

Layering rule: arrows point down only. Workflows know verbs; verbs know nothing about workflows; adapters know their tool or the AI subsystem; the dispatch core knows nothing about any specific verb; the platform layer is reachable only through `RunContext`.

## 4. The dispatch core

### 4.1 RunContext

Every invocation receives an explicit `RunContext` — the single seam through which all capability flows (and therefore the single place to fake everything in tests):

```python
class RunContext:
    run_id: RunId
    repo: RepoInfo                 # root, head, branch, dirty state
    scm: Scm                       # §6
    ci: CiEnvironment | None       # §6.3
    tickets: TicketSource          # §7
    ledger: Ledger                 # §7.4 traceability writes
    tracer: Tracer                 # OTel
    secrets: SecretResolver
    container: Container

    async def do(self, verb: type[V], inp, *, using: str | None = None,
                 step: str | None = None) -> Outcome: ...
    async def escalate(self, *evidence) -> Outcome: ...
```

`ctx.do(Test, spec)` resolves the bound adapter from the container, wraps it in the middleware chain, records the step for checkpointing, and returns an `Outcome`. `using="name"` selects among multiple named bindings. Context is passed explicitly (v0.2 decision — see Review Record R2-S1); an ambient `current_context()` contextvar exists for library authors but user code is encouraged to thread `ctx`.

### 4.2 Verbs and adapters

Each verb is a small generic interface with a typed input and output:

```python
class Action(Protocol[I, O]):
    verb: ClassVar[Verb]
    capabilities: ClassVar[frozenset[Capability]]   # e.g. {WRITES_FILES, NEEDS_NET, EXECUTES_CODE}
    async def invoke(self, ctx: RunContext, inp: I) -> Outcome[O]: ...
```

| Verb | Input | Output value | Default adapter kind |
|---|---|---|---|
| `build` | `BuildSpec` | `BuildResult` (artifacts, logs) | deterministic (`uv`, `make`, `docker`, `shell`) |
| `test` | `TestSpec` | `TestReport` (cases, failures, flaky[], coverage) | deterministic (`pytest`, `tox`, junit-xml ingest) |
| `validate` | `ValidateSpec` | `ValidationReport` (findings by rule) | deterministic (`ruff`, `mypy`, policy checks) |
| `run` | `RunSpec` | `RunResult` / `RunHandle` | deterministic, **sandboxed** (§10.3) |
| `implement` | `ImplementSpec` (ticket/spec + constraints) | `ChangeSet` | AI |
| `fix` | `FixSpec` (diagnosis or failure ctx) | `ChangeSet` | AI (also deterministic e.g. `ruff --fix`) |
| `review` | `ChangeRequest \| ChangeSet` | `ReviewReport` (findings, verdict) | AI (also deterministic lint-gates) |
| `triage` | `Signal` (failure, alert, issue) | `TriageDecision` (class, severity, route) | AI or rules-based |
| `debug` | `FailureContext` | `Diagnosis` (root cause, evidence, repro) | AI |

The data types between verbs are the contract that makes workflows composable: `Diagnosis → FixSpec → ChangeSet → TestSpec → TestReport`. All are frozen dataclasses; all serialize losslessly for checkpointing and the ledger.

Adapters declare `capabilities`; policy middleware (§10) can veto or gate on them without knowing the adapter.

### 4.3 Outcome — failure is data *(changed in v0.2)*

```python
@dataclass(frozen=True)
class Outcome(Generic[O]):
    status: Status        # SUCCEEDED | FAILED | BLOCKED | ERRORED | SKIPPED | PARKED  (v0.3)
    value: O | None
    findings: tuple[Finding, ...]
    artifacts: tuple[ArtifactRef, ...]
    cost: Cost            # tokens, usd, wall time
```

The taxonomy exists because alerting and control flow need it (Review Record R1-SRE-3):

- `FAILED` — the domain said no (tests red, review rejected). Normal, routable data.
- `ERRORED` — infrastructure broke (provider 500, git auth). Retryable/alertable.
- `BLOCKED` — policy or approval gate stopped it. Neither failure nor error.
- `SKIPPED` — cache hit or conditional bypass.
- `PARKED` — the run ended at a human boundary with a continuation registered (§13). Distinct from BLOCKED because it alerts differently: PARKED notifies the designated humans and starts an SLA clock; BLOCKED pages policy owners or nobody. *(added in v0.3.)*

Exceptions are reserved for programmer error; workflows branch on `Outcome`, so a red test never unwinds the stack.

### 4.4 Middleware — the chain of actors

```python
class Middleware(Protocol):
    async def __call__(self, ctx: RunContext, call: ActionCall,
                       next: Next) -> Outcome: ...
```

Registered globally (`lockstep.middleware`), per-verb (`lockstep.middleware.for_verb(Verb.RUN, ...)`), or per-call (`ctx.do(..., middleware=[...])`). Because `next` is explicit, a middleware can act before, after, around, or instead — which is exactly the IoC hook requirement ("execute additional functionality before and after invocation of the action verbs") without a separate before/after registration API to keep in sync.

Shipped middleware: `otel()`, `CostBudget`, `Retry` (ERRORED only, jittered), `ApprovalGate`, `RecordReplay`, `Redact` (secret patterns out of prompts/logs), `Cache` (keyed on input hash for deterministic verbs), `KillSwitch` (env `IN_LOCKSTEP_DISABLE`, checked first — R1-SRE-4).

Implementation constraint (R2-DX-1): middleware composes via plain function calls, no decorator stacking or metaclass indirection, so `pdb` and stack traces stay legible; `in-lockstep run --no-middleware` exists for bisecting behavior.

### 4.5 Container — inversion of control

```python
class Container:
    def bind(self, iface: type[T], impl: T | type[T], *,
             name: str | None = None, scope: Scope = Scope.SINGLETON) -> None: ...
    def resolve(self, iface: type[T], name: str | None = None) -> T: ...
```

Resolution order: explicit `lockstep.bind` in your module → project plugins (Python entry points, group `in_lockstep.adapters`) → shipped defaults. `Scope.SINGLETON` for clients (SCM, providers), `Scope.CALL` for stateful adapters. Because binding is ordinary code, an org can publish `acme-lockstep-conventions` as a pip package that binds company defaults, and a repo overrides one line of it — the "paved road" pattern (R1-EM-3).

### 4.6 Workflows, steps, and resumability *(changed in v0.2)*

`@workflow` registers a plain async function; control flow is native Python. Each `ctx.do` is a *step*, keyed by an id derived from call site + input hash (overridable with `step=`). With a `StateStore` configured (default: filesystem under `.in-lockstep/runs/`, adapters for S3/GCS), completed step outcomes are checkpointed, and `in-lockstep run --recover <run-id>` replays completed steps from the store and continues — surviving CI timeouts and spot-instance death (R1-SRE-1). Checkpointing is opt-out-able; without it the model is "just a Python function," preserving the simplicity story. Recovery covers *machine* failure only — a run never waits on a person. Human waits end the run at a boundary (§13); `--recover` restarts the same interrupted run, while `resume` (§13.3) starts a new linked run from a human's event.

### 4.7 Fan-out / fan-in *(added in v0.4)*

Workflows can run branches concurrently and gate continuation on all of them completing. Branches are declared, not started:

```python
join = await ctx.fan_out(
    security = ctx.call(Review, spec, strategy="security"),      # §5.7 strategies
    standard = ctx.call(Review, spec, strategy="standard"),
    smells   = ctx.call(Review, spec, strategy="code-smells"),
    tim      = ctx.human(HumanBoundary.pr_review(pr, reviewer="tim")),
    resume   = "release/after-reviews",
    max_parallel = 3,
)
return join            # PARKED here — the "tim" branch is a human boundary
```

Machine branches execute concurrently, bounded by `max_parallel` and sharing the run's `CostBudget` — fan-out multiplies spend, so the budget is joint, never per-branch (R4-SRE-1). Each branch is its own step for checkpointing and recovery, its own cassette for replay (`--replay <run> --branch security`), and its own span nested under a fan-out span (R4-DX-1).

**The barrier.** Continuation is contingent on every branch reaching a *terminal* state — `SUCCEEDED`, `FAILED`, `ERRORED` (after retries), or `EXPIRED`. Completion is not success (R4-STAFF-1): the barrier answers "is everyone done"; what the mix *means* is the continuation's decision. If all branches are machine-only and none parks, `fan_out` returns the `JoinResult` inline and the function simply continues — pure in-run fan-in. If any branch is human, or parks internally, the framework writes a **barrier record** to the ledger (per-branch states, head SHA, per-branch expiry) and the run ends `PARKED`. Each human event then resumes a framework-supplied *barrier tick* — the §13.3 claim machine applied per branch — which records that branch's outcome; the tick whose ledger write completes the barrier launches the declared continuation with the full result, and exactly one write does, because ledger writes are ordered (§15.3, R4-SRE-2). Human branches notify their own audiences on park; an expired branch joins as `EXPIRED` via `sweep` rather than holding the barrier open forever.

```python
@workflow(id="release/after-reviews")
async def after_reviews(ctx, r: Resumption):
    jr: JoinResult = r.payload["join"]                  # branch name → Outcome
    blocking = [f for o in jr.outcomes() for f in o.findings if f.blocking]
    if blocking or not jr["tim"].succeeded:
        return await ctx.do(Fix, FixSpec.from_findings(blocking, jr))
    return jr.as_outcome()
```

Aggregation — quorums, weighted verdicts, "security overrides all" — is ordinary code or a bound `ReviewAggregator` policy. The framework's contract is only the barrier.


## 5. The AI subsystem

The non-deterministic verbs are built on six framework types the user asked for by name — `Model`, `Provider`, `Auth`, `Prompt`, `Context`, `Mcp` — composed by one internal engine, `AiInvoker`.

### 5.1 Model and Provider

```python
@dataclass(frozen=True)
class Model:
    id: str                      # "anthropic:claude-sonnet-4-6", "local:qwen3-8b"
    caps: ModelCaps              # context window, tool use, structured output, vision
    cost: CostTable | None       # None for local

class Provider(Protocol):
    async def complete(self, req: ModelRequest, creds: Credentials) -> ModelResponse: ...
    data_policy: DataPolicy      # residency/retention classification, used by policy middleware
```

Shipped providers: `AnthropicProvider`, `OpenAIProvider`, `AzureOpenAIProvider`, `BedrockProvider`, `VertexProvider`, and two that satisfy the "custom/local/gateway" requirement:

- `OpenAICompatibleProvider(base_url=...)` — any OpenAI-compatible server: LM Studio (`http://localhost:1234/v1`), Ollama, vLLM, llama.cpp, and most enterprise gateways (LiteLLM proxy, OpenRouter, internal gateways). One adapter covers the entire local/gateway ecosystem because that ecosystem standardized on this wire format.
- `AnthropicCompatibleProvider(base_url=...)` — for gateways and local servers exposing the `/v1/messages` shape.

`ModelRouter` is the strategy object mapping work to models: per-verb defaults (`lockstep.models.route(Verb.TRIAGE, "local:qwen3-8b")`), overridable per call, and constrainable by policy (e.g., "repos labeled `restricted` may only route to providers with `data_policy.residency == INTERNAL`" — R1-SEC-4).

### 5.2 Auth

```python
class Auth(Protocol):
    async def credentials_for(self, target: AuthTarget) -> Credentials: ...
```

`AuthTarget` covers model providers, SCM hosts, and ticket sources uniformly. Shipped resolvers, tried in order by `Auth.chain(...)`: explicit env vars → CI-native OIDC federation (GitHub Actions OIDC → cloud/gateway STS; GitLab CI JWT) → OS keychain (local dev) → vault adapters. Credentials never enter prompts or logs: `Redact` middleware runs on every model request and every log record, seeded with the resolver's known secret values (R1-SEC-2). Local providers resolve to `Credentials.none()`.

### 5.3 Prompt — prompts are code

```python
class Prompt(Generic[P, S]):                # P: params dataclass, S: output schema
    version: ClassVar[str]                  # eval-tracking key, bumped on any change
    output: ClassVar[type[S]]               # pydantic model → structured output
    def render(self, params: P, ctx_pkg: ContextPackage) -> Messages: ...
```

Prompts are classes in your repo, so they diff, blame, and roll back like everything else, and `version` keys the evaluation store (§8). The framework ships a default prompt per AI verb (`prompts.ImplementPrompt`, `prompts.ReviewPrompt`, …) designed for subclass extension — override `emphasis`, add few-shot `exemplars`, or replace `render` wholesale. A prompt change is a PR; §8 makes prompt PRs pass offline evals in CI before merge.

### 5.4 Context — deterministic assembly of what the model sees

```python
class ContextSource(Protocol):
    async def gather(self, ctx: RunContext, need: ContextNeed) -> list[ContextItem]

@dataclass(frozen=True)
class ContextItem:
    kind: str                      # "file", "diff", "ticket", "test-failure", "log"
    provenance: Provenance         # TRUSTED_REPO | UNTRUSTED_EXTERNAL | GENERATED
    tokens: int
    content: str
```

Shipped sources: changed-files, dependency-slice (imports of touched files), failing-test output, ticket body + comments, recent-commit summaries, CI log tail. A `ContextCurator` assembles a `ContextPackage` under an explicit token budget with a stable priority order — same inputs, same package, which is what makes cassette replay (§9.2) and eval comparison (§8) meaningful. Every item carries `provenance`; untrusted items are delimited and labeled in the rendered prompt, and policy can restrict the ToolSet whenever untrusted content is present (R1-SEC-1).

### 5.5 Mcp — tools

```python
mcp = McpServer.stdio("uvx", "mcp-server-git", pin="1.4.2@sha256:…")   # pinned (R1-SEC-5)
tools = ToolSet.from_mcp(mcp).allow("git_log", "git_diff") \
      + ToolSet.builtin("read_file", "search_code")
```

Tool exposure is allowlist-only and declared per AI verb binding. Defaults are read-only; anything with `WRITES_FILES` or `EXECUTES_CODE` capability is deny-by-default and typically paired with `ApprovalGate`. MCP servers support stdio and HTTP transports and must be version-pinned; `in-lockstep doctor` warns on unpinned servers.

### 5.6 AiInvoker

```python
class AiInvoker:
    async def run(self, *, prompt: Prompt[P, S], params: P, need: ContextNeed,
                  tools: ToolSet, skills: SkillSet, policy: InvokePolicy) -> S: ...
```

`InvokePolicy` bounds the agentic loop: `max_turns`, token/USD budget, wall-clock limit, allowed providers. The invoker emits OTel GenAI spans per model call, validates structured output against `S` (bounded repair-reprompt on schema failure), and appends an eval event to the ledger for every invocation. AI-backed verb adapters (`AiImplement`, `AiFix`, `AiReview`, `AiTriage`, `AiDebug`) are thinner still in v0.4: they resolve the selected `Strategy` (§5.7) and delegate. The strategy maps verb input → prompts, context, tools, and skills (§5.8), drives one or more invoker calls — possibly interleaved with deterministic verbs — and maps structured output back to the verb's type.

### 5.7 Strategies — pluggable behavior for AI verbs *(added in v0.4)*

Binding chooses *which adapter* serves a verb; a strategy chooses *how the AI adapter approaches the work*. A `Strategy` names a complete approach — prompt(s), context recipe, tools, skills, invoke policy, and possibly a multi-phase internal plan:

```python
class Strategy(Protocol[I, O]):
    id: ClassVar[str]                          # "implement/tdd"
    verb: ClassVar[Verb]
    skills: ClassVar[SkillSet] = SkillSet.none()
    async def execute(self, ctx: RunContext, ai: AiInvoker, inp: I) -> Outcome[O]: ...
```

A strategy may drive several invoker calls and interleave deterministic verbs. The shipped `implement/tdd` strategy literally runs red→green: generate failing tests, `ctx.do(Test, …)` to confirm red, implement, re-test, iterate within policy bounds. `implement/wayfinder` explores first — read-only tools over the dependency slice, a plan artifact — then implements against the plan. `implement/direct` is single-shot. Review ships `review/standard`, `review/security`, `review/code-smells`; a team persona strategy (`review/tims-review`) is a two-line prompt subclass plus a registration — and immediately usable as a fan-out branch (§4.7).

Selection, most-specific wins: per call (`ctx.do(Implement, spec, strategy="wayfinder")`) → a bound `StrategySelector` (rules over ticket labels/change size, or an AI triage) → the registered default (`lockstep.strategies.default(Verb.IMPLEMENT, "tdd")`). Every selection is ledgered, and `strategy_id` is part of the eval-subject key (§8.2), so strategies are measured *against each other* on the same ground-truth signals — merge rate, churn, reverts, defect linkage — and the Improver's proposals to re-route between strategies arrive as evidence-bearing PRs like any other learning (R4-QA-1).

### 5.8 Skills *(added in v0.4)*

Skills are versioned instruction packages — "how we do X here" — included in AI invocations:

```python
skills = SkillSet.from_repo(".in-lockstep/skills")                    # reviewed like code
       | SkillSet.package("acme-eng-skills", pin="1.2.0@sha256:…")    # pinned like MCP (§10.7)
```

`AiInvoker.run(..., skills=...)` accepts the set; strategies declare defaults via their `skills` classvar and calls add more — composition is additive. Rendering is budget-aware progressive disclosure: every included skill contributes its name and description to the system context; bodies load within a reserved `ContextCurator` slice by relevance; oversized skills expose an `open_skill` builtin tool so the model pulls detail on demand. Repo-local skills carry `TRUSTED_REPO` provenance and are reviewed like any code; external packs are hash-pinned supply chain (R4-SEC-2). The `skillset_hash` joins the eval key (§8.2), so skill edits are measurable — and harvested exemplars (§8.3) frequently graduate into skills, the learning loop's most durable output.


## 6. SCM and CI awareness

### 6.1 The Scm interface

```python
class Scm(Protocol):
    def repo(self) -> RepoInfo
    def diff(self, base: Ref, head: Ref) -> Diff
    def blame(self, path: str, line_range: range) -> Blame
    async def open_change(self, cs: ChangeSet, *, title: str, body: str = "",
                          ticket: TicketRef | None = None) -> ChangeRequest   # PR / MR
    async def comment(self, target: ChangeRequest | Commit, body: str) -> None
    async def checks(self, ref: Ref) -> list[CheckRun]
    async def find_changes(self, query: ChangeQuery) -> list[ChangeRequest]  # feeds §8 signals
```

Implementations: `GitLocal` (pure git — always available, no host API), `GitHubScm` (REST/GraphQL; token or GitHub App), `GitLabScm`. `GitHubScm`/`GitLabScm` *contain* a `GitLocal` — host features layer over plain git rather than replacing it.

### 6.2 Write discipline *(changed in v0.2)*

Default policy: **the framework never pushes to a shared branch.** All writes go through `open_change` on a run-scoped branch (`in-lockstep/<workflow>/<run-id>`), which also serializes concurrent runs without a lock service (R1-DEVOPS-4, R1-SEC-3). Direct-push is possible only by explicitly binding a `DirectPushScm` — a deliberate, greppable, reviewable act.

### 6.3 Environment detection

`Lockstep.detect()` resolves, in order: `GITHUB_ACTIONS` env (repo, ref, PR number, actor, OIDC available) → `GITLAB_CI` env (`CI_*` vars) → local git (remotes sniffed to pick GitHub/GitLab API client if a token is present, else `GitLocal`). The result is a `CiEnvironment | None` on the context plus a correctly-configured `Scm`. Detection is overridable — it is a default, not magic: `Lockstep(scm=GitLabScm(...), ci=None)` is always available and is what tests use.

## 7. Work item management

### 7.1 Abstract types

```python
class Ticket(Protocol):
    key: str; title: str; description: str
    state: TicketState; type: TicketType; url: str
    links: list[TicketLink]

class TicketSource(Protocol):
    async def get(self, key: str) -> Ticket
    async def search(self, q: TicketQuery) -> list[Ticket]
    async def create(self, draft: TicketDraft) -> Ticket
    async def transition(self, t: Ticket, to: TicketState) -> Ticket
    async def comment(self, t: Ticket, body: str) -> None
    async def link(self, t: Ticket, target: Linkable, kind: LinkKind) -> None   # §7.4
```

`TicketState` and `TicketType` are framework-level enums with an `raw` escape hatch, because every tracker's state machine differs; adapters map both ways.

### 7.2 Jira implementation

```python
class JiraSource(TicketSource): ...          # cloud + data-center auth via Auth (§5.2)

class JiraIssue(Ticket): issue_type: ClassVar[JiraTypeRef]
class JiraEpic(JiraIssue):    issue_type = JiraTypeRef(name="Epic")
class JiraStory(JiraIssue):   issue_type = JiraTypeRef(name="Story")
class JiraFeature(JiraIssue): issue_type = JiraTypeRef(name="Feature")   # premium hierarchies
class JiraSpike(JiraIssue):   issue_type = JiraTypeRef(name="Spike")     # commonly custom
```

`JiraTypeRef` resolves by name or explicit id at runtime because Jira type schemes vary per site; declaring a new custom type is a two-line subclass — the "declare" semantic doing real work. `GitHubIssuesSource` and `GitLabIssuesSource` ship as well, reusing the SCM auth and clients, so small projects get work-item integration with zero extra systems.

### 7.3 Ticket-driven workflows

`ImplementSpec.from_ticket(ticket)` is the canonical entry: ticket body and comments become `UNTRUSTED_EXTERNAL` context items (§5.4), acceptance-criteria extraction feeds `Validate`, and `triage` can route signals into ticket creation (`TicketDraft.from_signal(...)`).

### 7.4 Artifact ↔ ticket traceability *(mechanism changed in v0.2)*

Every `Outcome` artifact gets an `ArtifactRef` (kind, content hash, storage ref). Association is written in four redundant layers, most-portable first:

1. **Commit trailers** on every framework-authored commit: `Ticket: PROJ-123`, `In-Lockstep-Run: 8f31…`, `In-Lockstep-Prompt: ReviewPrompt@team-3`. Greppable forever, survives any migration.
2. **ChangeRequest body** metadata block (rendered + machine-readable fenced JSON).
3. **Ticket remote-links/comments** via `TicketSource.link` (Jira remote issue links; GH/GL cross-references).
4. **The Ledger**: append-only JSON records, one file per run at `.in-lockstep/ledger/<run-id>.json`, committed with the change. One-file-per-run means no merge conflicts; in-repo means versioned, diffable, and owned by you. v0.1 used git notes; review R1-DEVOPS-2 killed that (notes don't survive default clone/push). A `LedgerStore` adapter can mirror to external storage at scale, but the repo copy is canonical. The backend is a pluggable `LedgerStore`: the in-repo store is the zero-config default, and a workspace-scoped store takes over only when one is configured (§15.3).

`in-lockstep trace PROJ-123` walks all four layers and prints every run, commit, PR, artifact, and eval score associated with a ticket.

## 8. Evaluation and the learning loop

The requirement: measure AI-generated artifacts over time, and improve the *local implementation* using that measurement, through commits/PRs. The design closes an elegant loop: because prompts, exemplars, and routing policy are code (§5.3), "learning" means "open a PR against your own repo," which the same lifecycle then reviews, tests, and merges. The framework improves through the process it automates, with humans at the merge gate.

### 8.1 Signals — ground truth already lives in your SCM

```python
class SignalCollector(Protocol):
    async def collect(self, since: datetime) -> SignalSet
```

The shipped collector mines what git/GitHub/GitLab already record about every framework-authored change (found via §7.4 trailers): merged vs. closed-unmerged; reverted (revert-commit detection); human review churn (change-requested rounds, comment count); CI first-pass rate; time-to-merge; post-merge defect linkage (a later `fix:` commit whose trailer references the same ticket). No new tracking infrastructure — the SCM *is* the label store.

### 8.2 Evaluators and the store

```python
class Evaluator(Protocol):
    async def evaluate(self, subject: EvalSubject, signals: SignalSet) -> list[Score]
```

`EvalSubject` keys a subject by `(verb, strategy_id, prompt_id@version, model_id, context_recipe_hash, skillset_hash)` — the exact knobs a team can turn. Scores append to the `EvalStore` (ledger-backed, §7.4). Shipped evaluators: outcome-signal evaluator (from §8.1), rubric evaluator (AI-graded against a code-reviewed rubric prompt — graded by a *different* model than the producer), and assertion evaluators for structured outputs. Composite scoring is explicit and code-reviewed, because single metrics are gameable (R1-QA-2): merge-rate alone would reward timid diffs, so it is always reported alongside churn, revert rate, and defect linkage.

### 8.3 The offline eval harness *(added in v0.2)*

`in-lockstep eval` runs a prompt/model/recipe against a fixture corpus with cassette-recorded context, entirely offline where possible, producing a scorecard diff between versions. Fixtures come from history: `in-lockstep eval bootstrap` harvests them retroactively — merged framework-authored diffs become positive exemplars, reverted/rejected ones become negatives and regression cases (R2-QA-1 — solves the cold-start problem for new adopters). The corpus lives in `.in-lockstep/evals/`.

### 8.4 The Improver — learning as pull requests

```python
class Improver(Protocol):
    async def propose(self, evidence: EvalEvidence) -> ChangeSet | None
```

A shipped meta-workflow, `improve`, runs on a schedule: collect signals → aggregate per `EvalSubject` → where evidence clears a configured threshold, generate a proposal (revise a prompt's guidance, add/remove few-shot exemplars from harvested cases, adjust `ModelRouter` policy, tune a context recipe) → run the offline harness on the proposal → **open a PR** with the scorecard diff embedded in the body. Governance is structural: learning PRs carry a `lockstep-learning` label, require human review like any change, and CI on that PR re-runs `in-lockstep eval` — a prompt change that regresses the corpus cannot merge (R1-QA-3). Reject the PR and the evidence threshold rises for that proposal class. Nothing self-modifies at runtime; the only learning channel is the reviewed commit.

## 9. Observability and reproducibility

### 9.1 OTel-native

The `otel()` middleware is default-on and needs only standard `OTEL_EXPORTER_OTLP_*` env to ship. Span hierarchy: `workflow → step → action → model-call`, with model-call spans following the OTel GenAI semantic conventions (`gen_ai.system`, `gen_ai.request.model`, token usage attributes). Metrics: `in_lockstep.action.duration`, `in_lockstep.action.outcome` (dimensions: verb, adapter, status — bounded cardinality; run_id lives on spans, not metrics — R1-SRE-2), `gen_ai.client.token.usage`, `in_lockstep.cost.usd`. Incoming `TRACEPARENT` is honored, so runs join the CI pipeline's trace. Logs flow through the OTel logging bridge with `Redact` applied.

### 9.2 Record/replay *(added in v0.2)*

`RecordReplay` middleware writes cassettes of all provider IO and tool IO per run. `--replay <run-id>` re-executes a workflow with model calls served from cassettes: deterministic, offline, free. This is simultaneously the debugging story (step through the exact failed run in `pdb`), the testing story (framework and user workflow tests run without keys or spend), and the eval story (§8.3 replays context deterministically). `DryRunProvider` additionally lets any AI verb answer with canned/templated outputs for pipeline smoke tests (R1-DX-1).

## 10. Security model *(section added in v0.2 — R1-SEC)*

Threat model in one line: the framework feeds attacker-influenceable text (repo files, ticket bodies, CI logs) to models that hold tools and credentials, then acts on the output.

1. **Provenance-tagged context** (§5.4). Untrusted items are labeled and delimited in prompts; policy middleware can shrink the ToolSet to read-only when any `UNTRUSTED_EXTERNAL` item is present.
2. **Deny-by-default tools** (§5.5). Allowlists per verb; write/exec tools require explicit grant, typically behind `ApprovalGate`.
3. **Sandboxed `run`.** The default `Run` adapter executes in a container (or firejail/no-net subprocess fallback) with no ambient credentials and an explicit mount set. Executing generated code outside a sandbox requires binding an adapter whose name says so (`UnsandboxedRun`).
4. **Write-via-PR only** (§6.2). Protected branches are structurally unreachable; branch protection + CODEOWNERS remain the org's enforcement backstop.
5. **Credential hygiene** (§5.2). Scoped, short-lived, OIDC-preferred; `Redact` on prompts, logs, cassettes; secrets resolve at the edge and never enter `ContextPackage`.
6. **Data egress policy.** `Provider.data_policy` + repo classification lets policy middleware block external providers for restricted repos — the local/gateway provider path (§5.1) is the sanctioned alternative.
7. **Supply chain.** MCP servers, adapters, and external skill packs (§5.8) pinned by version+hash; `in-lockstep doctor` audits pins; plugins load only from declared entry-point groups.
8. **Audit.** Ledger (§7.4) + OTel logs give an append-only, in-repo record of every invocation, prompt version, model, and outcome.

## 11. CLI and CI/CD integration

```
in-lockstep init                      # scaffold lockstep.py from detected stack; paved-road templates
in-lockstep run <workflow|verb> [--trigger …] [--offline] [--replay ID] [--recover ID] [--no-middleware]
in-lockstep resume --event <kind:ref> | --run ID --as <verdict> --by <user>   # human-boundary continuation (§13)
in-lockstep sweep                 # escalation/expiry scan over parked runs (§13.6)
in-lockstep eval [bootstrap|report]   # §8.3
in-lockstep trace <ticket|run|commit> # §7.4
in-lockstep ls [--parked]             # resolved config; parked runs awaiting humans, with ages
in-lockstep doctor                    # auth, SCM reach, provider reach, pins, import-purity lint
```

Discovery: `lockstep.py` (or `lockstep/` package) at repo root, plus entry-point-registered workflow packages. `in-lockstep ls` prints the *resolved* container — since config is code, this is the "what will actually run" answer that YAML users get from their file (R1-DX-3).

**The trampoline principle.** CI hosts require their own YAML; that YAML invokes the CLI and contains no lifecycle logic. It is host-owned invocation, not framework output — nothing is generated, nothing is transpiled:

```yaml
# .github/workflows/fix-ci.yml — the entire file
on: {workflow_run: {workflows: ["CI"], types: [completed]}}
permissions: {contents: write, pull-requests: write, id-token: write}
jobs:
  fix:
    if: ${{ github.event.workflow_run.conclusion == 'failure' }}
    runs-on: ubuntu-latest
    steps:
      - uses: in-lockstep/setup@v1          # installs pinned CLI, wires OIDC + TRACEPARENT
      - run: in-lockstep run fix_ci --trigger ci:${{ github.event.workflow_run.id }}
```

A GitLab CI component with the same shape ships alongside, as does a container image (`ghcr.io/in-lockstep/runner`) for cold-start-sensitive pipelines (R1-DEVOPS-3). Kill switch: `IN_LOCKSTEP_DISABLE=1` at org/repo level halts all runs before any middleware executes.

## 12. Packaging, extension, and stability

Import-time purity: `lockstep.py` may construct objects and bind, but must not perform IO at import; `in-lockstep doctor` lints this by importing the module under a recording shim (R1-STAFF-4). Extension is via ordinary subclassing plus entry points (`in_lockstep.adapters`, `in_lockstep.workflows`, `in_lockstep.evaluators`). API stability: `in_lockstep.*` root namespace is semver-stable; incubating pieces live under `in_lockstep.x.*` until promoted. Sync facade: every async API has a `.sync` mirror (`lockstep.sync.do(...)`) for scripts and REPL use; the core is async because model calls, SCM APIs, and subprocesses are all IO-bound and workflows fan out (R1-STAFF-1).

## 13. Human boundaries *(added in v0.3)*

Principle 7 in full: a run is a single machine-driven episode. The moment a person must weigh in, the run does not wait — it **parks**: state externalizes to the ledger and the system of record, notifications go out (§14), the process exits with status `PARKED`, and a **continuation workflow** starts as a fresh run when the human's event arrives. Long lifecycles are chains of short runs stitched by human events, not one long process. This buys the durable-execution outcome (waits of days or weeks) with none of its costs: no determinism rules on user code, no replay sandbox, no stateful server — workflows remain plain Python, and in-lockstep remains a library.

The companion stance: **the human acts in the system of record** — approving the PR, transitioning the ticket, granting the host's environment approval — never in a bespoke UI. Notifications are signposts pointing at the place to act, not control surfaces (§14). Human decisions therefore inherit the SCM's and tracker's authentication, authorization, and audit for free, and the framework exposes no inbound endpoint.

### 13.1 park

```python
return await ctx.park(
    boundary: HumanBoundary,
    resume: str,                                  # stable continuation id (§13.4)
    payload: Mapping[str, Serializable] = {},     # what the continuation needs, serialized
    notify: Sequence[NotifyRoute] | None = None,  # default: policy routes for Event.PARKED
    expires: timedelta | None = None,
) -> Outcome                                      # status=PARKED
```

Parking does four things: writes a `parked` block into the run's ledger file (boundary descriptor, continuation id, payload, head SHA, expiry); places a machine-readable marker in the system of record (fenced JSON block in the PR/MR body plus a `lockstep:parked` label, or a ticket property/comment); fires notifications; exits. Everything needed to resume lives in the repo and the host — nothing in memory, nothing in a service.

### 13.2 Boundary types

| Boundary | Human act | Resume event |
|---|---|---|
| `HumanBoundary.pr_review(pr)` | approve / request changes / comment on the PR/MR | host review webhook |
| `HumanBoundary.ticket_transition(t, to=...)` | move the ticket (e.g. → Approved) | tracker webhook or poll |
| `HumanBoundary.host_approval(env)` | GH environment / GL protected-env approval | host deployment event |
| `HumanBoundary.choice(options)` | `/lockstep choose 2` as a PR or ticket comment | comment webhook |
| `HumanBoundary.free_text(ask)` | `/lockstep guide "…"` as a comment | comment webhook |

`choice` and `free_text` piggyback on comments deliberately: slash-commands inherit the host's identity, permissions, and audit trail, and need no new surface.

### 13.3 The resume flow

Host event → resume trampoline (a webhook-triggered CI job; it filters on the `lockstep:parked` label so unrelated events cost nothing, and declares a host `concurrency` group per target so duplicate deliveries serialize — R3-DEVOPS-1, R3-SRE-2) → `in-lockstep resume --event pr_review:41` → the router reads the marker, loads the park record from the ledger, then:

1. **Verifies the condition** — is this event the boundary's completion (an approval, the right transition, a well-formed slash-command)?
2. **Verifies the actor** — host-native authorization: an approval only counts from a user the host accepted as a reviewer; slash-commands are honored only from write-or-above roles (R3-SEC-2). Actor identity is recorded in the ledger.
3. **Claims idempotently** — park lifecycle is a small state machine, `PARKED → RESUMING(event-id) → RESUMED(child-run-id) | EXPIRED`, with dedupe on `(run_id, event_id)`; webhook redelivery and double-submitted reviews collapse into one continuation (R3-SRE-2).
4. **Invokes the continuation** as a fresh run carrying a `Resumption` (event, actor, verdict, comment text, rehydrated payload, staleness — §13.5), then clears the marker/label.

Local development and tests never need webhooks: `in-lockstep resume --run <id> --as approved --by tim` simulates any boundary event, and `RecordReplay` cassettes capture resumptions like everything else (R3-DX-1).

### 13.4 Continuations

A continuation is an ordinary `@workflow` registered with a **stable id** — `@workflow(id="fix-ci/after-review")` — and park records reference that id, never the function name, so refactoring can't strand parked runs; `doctor` flags park records whose id no longer resolves (R3-STAFF-1). The child run carries `parent_run_id`; OTel span links join the traces; the ledger chains the runs, so `in-lockstep trace` shows the whole lifecycle across every park. A continuation may itself park — that is the normal shape of a long lifecycle.

### 13.5 Staleness

The park record pins the head SHA at park time. On resume, `Resumption.staleness` reports whether the branch or base moved while humans deliberated. Policy `on_stale=` chooses `reassess` (default: re-run `validate` and `test` against the current head before acting), `rebase`, or `escalate` (R3-QA-1). Approval of a diff is not treated as approval of a different diff.

### 13.6 Expiry and escalation

`sweep` is a shipped, scheduled meta-workflow (cron trampoline) that scans parked runs: past a soft threshold it walks the route's escalation ladder (e.g. Slack re-ping → PagerDuty after 24h), digest-batched so fifty stale parks make one message, not fifty (R3-SRE-1); past `expires` it closes the run `BLOCKED(expired)`, clears the marker, and comments on the ticket. Parked inventory is first-class: `in-lockstep ls --parked` lists boundary, owner, and age; a bounded-dimension age metric feeds dashboards so handoffs are visible work, not silent rot (R3-EM-1).

## 14. Notifications *(added in v0.3)*

Alerting follows the same pluggable pattern as every other subsystem: one protocol, shipped adapters, routing as code, everything interceptable.

```python
class Notifier(Protocol):
    channel: ClassVar[str]                        # "slack", "email", "sms", "pagerduty", "jira"
    async def send(self, n: Notification) -> DeliveryReceipt: ...

@dataclass(frozen=True)
class Notification:
    severity: Severity
    title: str
    body: str                                     # channel adapters render appropriately
    links: tuple[Link, ...]                       # run, PR/MR, ticket, trace — the places to act
    audience: Audience
    correlation_key: str                          # run id + boundary/event — threading & dedup
```

**Routing is code.** `lockstep.notify.route(event_or_predicate, to=[...], throttle=..., digest=...)`. Notifications fire from three places into one pipeline: explicitly in workflows via `ctx.notify(...)` — the "use it in your workflow" case; automatically on lifecycle events (`PARKED`, `PARK_EXPIRING`, `RESUMED`, `RUN_ERRORED`, `RUN_BLOCKED`, ...); and from `NotifyOn(...)` middleware mapping any outcome pattern to an event. Routes, like everything else, resolve through the container, so an org paved-road package can ship default routes a repo overrides in one line.

**Default notifiers.**

- `SlackNotifier` — bot token or webhook via `Auth`; one thread per `correlation_key`, so a run's whole lifecycle (parked → expiring → resumed) is a single thread, not channel spam; block-kit rendering with link-out buttons.
- `EmailNotifier` — SMTP in core; SES/SendGrid adapters via entry points; subject threading on the correlation key.
- `SmsNotifier` — Twilio adapter behind a generic protocol; content is structurally restricted to title + short link (§ security note below).
- `PagerDutyNotifier` — Events API v2; the correlation key maps directly onto PagerDuty's `dedup_key`, so lifecycle updates collapse into one incident, and a `RESUMED` event resolves it.
- `JiraTaskNotifier` — notification-as-work-item: composes the existing `TicketSource` (§7) to create a task assigned to the audience, linked (§7.4) to the run and its PR; sweep escalations update the same task rather than filing new ones. For teams whose real inbox is the board.

**Audience resolution — a pluggable loading pattern** *(reworked in v0.4)*. Who gets notified is resolved lazily at delivery time by `AudienceResolver`s, so the answer reflects the world when the event fires, not when the route was declared:

```python
class AudienceResolver(Protocol):
    async def resolve(self, ctx: RunContext, subject: NotifySubject) -> set[Recipient]: ...

audience = JiraAssignees() | Codeowners(of="change")                    # | = union
audience = JiraAssignees() >> Codeowners(of="change") >> Static("#eng-ai")   # >> = first non-empty
```

Shipped resolvers: `JiraAssignees` — users assigned on the ticket(s) *associated with the change*, found by walking the §7.4 trace links (change → ticket → assignees); `TicketAssignee` (source-agnostic); `Codeowners`; `Oncall(service)` (PagerDuty schedules); `Static`. Resolvers register through the container and entry points, so orgs add LDAP groups, GitHub teams, or custom directories without touching core. A resolver failure at send time degrades, never blocks: the `>>` chain falls through, then the route's `fallback_audience`, and an unresolved delivery is receipted as such (R4-DX-2). `Recipient` stays channel-agnostic; `UserDirectory` maps identities to per-channel handles as before.

**Delivery semantics.** Best-effort by design: a notifier failure never fails the run — it logs, records a failed `DeliveryReceipt`, and tries the route's `fallback=` chain (e.g. Slack → email). Every attempt writes a receipt to the ledger: an auditable record of who was told what, when, on which channel. `Retry` applies to `ERRORED` deliveries as it does everywhere.

**Security posture** (R3-SEC-1). Notifications cross the trust boundary out of repo/SCM into chat, mail, and phones. Default templates are therefore *link-first*: title, context line, deep links — the content lives where access control does. Including diffs or code excerpts is per-channel opt-in, never available for SMS, and every body passes `Redact`.

**Storm control** (R3-SRE-1). Dedup on `correlation_key`, per-key `Throttle`, and `digest=` batching for sweep-class events. A flapping workflow produces one escalating thread, not a paged-out on-call.

## 15. Multi-repo workspaces *(added in v0.4)*

### 15.1 Topology

A `Workspace` is declared in code in a **control repo** (for small cases, any member repo serves):

```python
ws = Workspace(
    repos=[Repo("github:acme/proto"), Repo("github:acme/api"), Repo("github:acme/web")],
    ledger=GitLedger("github:acme/lockstep-ledger"),   # §15.3 — omit to keep local per-repo ledgers
    merge_order=["proto", "api", "web"],
)
lockstep = Lockstep.detect(workspace=ws)
```

Workflows live in the control repo. Member repos carry only event trampolines that dispatch into the control repo's CI (GitHub `repository_dispatch`, GitLab multi-project pipelines) with the event payload — the same trampoline discipline as §11, one hop longer. A single repo is the degenerate workspace of one; nothing in the existing design changes (R4-STAFF-2). Mixed-host membership (GitHub + GitLab in one workspace) is expressible, since a member is just an `Scm`; it is flagged as an untested surface (open question 5).

### 15.2 Cross-repo change groups

There is no atomic cross-repo merge, and the design says so rather than pretending. `Implement` against a workspace yields per-repo `ChangeSet`s; `ws.open_changes(...)` opens linked PRs, each carrying an `In-Lockstep-Group:` trailer and marker. The group's merge gate **is** a fan-in (§4.7): a join over each PR's `pr_review` boundary — cross-repo coordination reuses the barrier, no second mechanism. The join continuation then merges in `merge_order`, honoring each repo's own branch protection and CODEOWNERS: a group merges only when every PR passes its own repo's policy (R4-EM-1). A failed mid-sequence merge triggers the group rollback policy (revert merged members, comment the group, escalate). Merge-queue integration (GitHub merge queue, GitLab merge trains) is an adapter seam, deferred (open question 4).

### 15.3 The workspace ledger

`LedgerStore` protocol: append a run file, CAS a state transition, scan. The resolution rule preserves the local default verbatim: **a run's ledger is the workspace's when one is configured, otherwise the local in-repo store — unchanged.** The recommended workspace store is `GitLedger` — a plain git repo as the ledger:

- run files stay one-per-run → conflict-free appends;
- state transitions (park claims, barrier ticks) are commits, and **git's atomic ref update is the CAS**: concurrent writers race the push; the loser rebases, re-reads, and a claim that lost sees the winner and aborts. Retries are jittered (R4-DEVOPS-1);
- barrier completion is decided by commit order on the ref — exactly one tick's commit completes the barrier, so exactly one continuation fires (§4.7, R4-SRE-2);
- auth rides the existing `Scm`/`Auth` machinery, and the record is diffable, auditable, and permissioned like any repo — the framework's values, zero new infrastructure.

Object-store (S3/GCS conditional-put CAS) and database adapters exist via entry points for hot orgs; `ledger compact` applies to every backend.

### 15.4 Credentials and blast radius

Workspace auth is per-repo, never org-wide: each member resolves its own scoped `AuthTarget` (GitHub App installation per repo, GitLab project tokens), and the ledger repo's write scope is its own target. A compromised member trampoline can dispatch events; it cannot write a sibling repo or forge the ledger (R4-SEC-1).


---

## 16. Design Review Record

Method: the v0.1 draft (everything above minus the *(changed in v0.2)* items) was reviewed by seven personas covering a standard SDLC. Each produced findings independently; findings were resolved into v0.2; a second round reviewed the resolutions; a third round reviewed the v0.3 delta — human boundaries (§13), notifications (§14), and the rename; a fourth round reviewed the v0.4 delta — fan-out/fan-in (§4.7), strategies and skills (§5.7–§5.8), audience resolution (§14), and workspaces (§15). IDs are `R<round>-<persona>-<n>` and are cross-referenced from the sections above.

### Round 1 — findings against v0.1, with resolutions

**Staff Engineer (API design, extensibility)**

| ID | Finding | Resolution in v0.2 |
|---|---|---|
| R1-STAFF-1 | Sync/async unspecified; mixing will infect every user API later. Decide now. | Async core (all boundaries are IO), `.sync` facade for scripts/REPL (§12). |
| R1-STAFF-2 | Exceptions as control flow would make "tests failed" unwind workflows. | `Outcome` with failure-as-data; exceptions = programmer error only (§4.3). |
| R1-STAFF-3 | Generic `Action[I, O]` risks capability soup — how does a workflow know an adapter can, e.g., write files? | `capabilities` classvar on adapters; policy/middleware key off it, workflows don't (§4.2). |
| R1-STAFF-4 | "Config is code" invites import-time side effects and un-introspectable setups. | Import-purity convention + `doctor` lint; `in-lockstep ls` prints resolved bindings (§12, §11). |
| R1-STAFF-5 | Protocol stability story missing; adapters will break downstream. | Semver-stable root namespace, `in_lockstep.x.*` incubator (§12). |

**Application Developer (day-to-day DX)**

| ID | Finding | Resolution |
|---|---|---|
| R1-DX-1 | Can't develop a workflow without burning tokens or having keys. | `RecordReplay` cassettes + `--offline` + `DryRunProvider` (§9.2). |
| R1-DX-2 | Adopting the whole framework to get one verb is too big an ask. | Every verb runs standalone (`in-lockstep run review --target pr:41`) with detected defaults; `lockstep.py` optional until you customize (§11). |
| R1-DX-3 | With YAML I can at least *read* the config; code-as-config can hide the effective setup. | `in-lockstep ls` = resolved-container dump; deterministic resolution order (§4.5, §11). |
| R1-DX-4 | Debugging through framework machinery is usually miserable. | Plain-call middleware (no decorator/metaclass stacks), `--no-middleware`, replay-then-pdb story (§4.4, §9.2). |

**DevOps / Platform Engineer**

| ID | Finding | Resolution |
|---|---|---|
| R1-DEVOPS-1 | "No YAML" collides with reality: GitHub Actions *is* YAML. Where's the line? | Trampoline principle made explicit: host-owned ~5-line invoker, zero logic, never generated (§11, principle 1). |
| R1-DEVOPS-2 | Git-notes ledger (v0.1) silently doesn't survive default clone/fetch/push. | Ledger moved to in-repo one-file-per-run JSON; notes dropped (§7.4). |
| R1-DEVOPS-3 | Cold-start (pip install + model warmup) will dominate short CI jobs. | Published runner image + `setup` action with pinned cache (§11). |
| R1-DEVOPS-4 | Two concurrent runs mutating one branch = corruption; where's locking? | Run-scoped branches + PR-only writes make runs naturally serialized; no lock service (§6.2). |
| R1-DEVOPS-5 | Secrets management strategy vague; "env vars" is not a story in CI. | `Auth.chain` with CI-native OIDC first-class; keychain/vault adapters (§5.2). |

**Security Engineer**

| ID | Finding | Resolution |
|---|---|---|
| R1-SEC-1 | Prompt injection: ticket text and repo content are attacker-controlled inputs to tool-holding models. Nothing in v0.1 addresses it. | Provenance-tagged context, labeled/delimited untrusted items, ToolSet shrink-on-untrusted policy (§5.4, §10.1). |
| R1-SEC-2 | Credentials can leak into prompts, logs, cassettes. | Secrets resolve at the edge only; `Redact` middleware seeded with known secret values applied to prompts, logs, and cassettes (§5.2, §10.5). |
| R1-SEC-3 | An AI with push rights to main is an incident waiting to happen. | Write-via-PR-only default; direct push requires binding an explicitly-named unsafe adapter (§6.2, §10.4). |
| R1-SEC-4 | Source code exfiltration to external model providers; no residency control. | `Provider.data_policy` + repo classification enforced by policy middleware; local/gateway path is the sanctioned alternative (§5.1, §10.6). |
| R1-SEC-5 | MCP servers are arbitrary code from the internet. | Mandatory pin-by-version+hash, `doctor` audit, entry-point-only plugin loading (§5.5, §10.7). |
| R1-SEC-6 | Generated code execution (`run`, and tests of generated code) on the host. | Sandboxed `Run` default (container/no-net), `UnsandboxedRun` as the explicit opt-out (§10.3). |

**QA / Test Engineer**

| ID | Finding | Resolution |
|---|---|---|
| R1-QA-1 | Flaky tests will send `fix_ci` into loops, or worse, teach `fix` to delete tests. | `TestReport.flaky[]` first-class (retry-based detection adapter); shipped `ReviewPrompt` and a deterministic review gate reject test deletion/skip without ticket linkage (§4.2). |
| R1-QA-2 | Merge-rate as the eval metric is gameable (timid diffs win). | Composite, code-reviewed scoring; churn/revert/defect-linkage always reported alongside (§8.2). |
| R1-QA-3 | The learning loop can degrade prompts with no regression net. | Offline eval harness; learning PRs must pass `in-lockstep eval` in CI to merge (§8.3–8.4). |
| R1-QA-4 | Nondeterminism makes the framework itself untestable. | Cassette replay is the framework's own test substrate too; seeded corpora ship in-repo (§9.2). |

**SRE**

| ID | Finding | Resolution |
|---|---|---|
| R1-SRE-1 | Long agentic runs vs. CI timeouts/spot death: total loss of progress. | Step checkpointing + `--recover` on a `StateStore` (§4.6). |
| R1-SRE-2 | run_id as a metric dimension will explode cardinality. | run_id on spans only; metrics dimensions bounded to verb/adapter/status (§9.1). |
| R1-SRE-3 | One "failure" bucket makes alerting impossible — a red test and a provider 500 page differently. | Outcome taxonomy: FAILED / ERRORED / BLOCKED / SKIPPED; `Retry` targets ERRORED only (§4.3). |
| R1-SRE-4 | Cost/rate runaway: an agent loop at 2 a.m. with no ceiling. | `CostBudget` (tokens/USD/wall/turns) per call & per run; `KillSwitch` env checked before any middleware (§4.4, §11). |

**Engineering Manager**

| ID | Finding | Resolution |
|---|---|---|
| R1-EM-1 | Governance: who approved what? Auditors will ask. | ApprovalGate + ledger + labels on framework PRs; CODEOWNERS interplay documented (§10.8, §8.4). |
| R1-EM-2 | I can't defend spend without cost-per-outcome visibility. | `in_lockstep.cost.usd` metric + per-Outcome `Cost`; `trace`/eval reports aggregate cost per merged change (§9.1, §8.2). |
| R1-EM-3 | Team-by-team divergence: fifty bespoke `lockstep.py` files. | Paved-road org packages via container resolution order; `init` templates (§4.5, §11). |
| R1-EM-4 | Vendor lock-in risk on any one model provider. | Provider adapters + router constraints; all learning data (ledger, evals, exemplars) lives in *your* repo, portable by construction (§5.1, §8). |

### Round 2 — review of v0.2

| ID | Persona | Finding | Disposition |
|---|---|---|---|
| R2-S1 | Staff | Ambient contextvar vs explicit `ctx`: pick a primary or users will mix them. | Resolved: explicit-first in all docs/examples; contextvar documented as library-author API (§4.1). **Sign-off.** |
| R2-DX-1 | Developer | Verify middleware keeps tracebacks flat in practice. | Resolved: plain-call composition constraint written into §4.4; `--no-middleware` retained. **Sign-off.** |
| R2-DEVOPS-1 | DevOps | Ledger growth in hot repos. | Accepted with note: `ledger compact` (roll runs older than N into a summary file) added to CLI backlog; not blocking. **Sign-off.** |
| R2-SEC-1 | Security | ApprovalGate UX in CI — where does the human click? | Resolved: gate materializes as a PR review request / environment approval on the host; BLOCKED outcome until granted. **Sign-off.** |
| R2-QA-1 | QA | Eval cold-start for a repo with no framework history. | Resolved: `eval bootstrap` retro-harvests from pre-adoption git history (§8.3). **Sign-off.** |
| R2-SRE-1 | SRE | Precedence: KillSwitch vs in-flight resumable runs. | Resolved: switch blocks new steps; in-flight step finishes; run checkpoints and exits BLOCKED (§4.4). **Sign-off.** |
| R2-EM-1 | EM | Adoption metric for the paved road itself. | Accepted with note: `doctor --report` emits adoption/config-drift summary; backlog. **Sign-off.** |

### Round 3 — findings against the v0.3 delta (human boundaries, notifications, rename)

| ID | Persona | Finding | Disposition |
|---|---|---|---|
| R3-STAFF-1 | Staff | Continuations referenced by function name = refactor breaks parked runs in the field. | Resolved: stable `@workflow(id=...)` ids in park records; `doctor` flags unresolvable ids (§13.4). **Sign-off.** |
| R3-DX-1 | Developer | Can't develop or test park/resume without standing up webhooks. | Resolved: `resume --run ID --as <verdict> --by <user>` simulates events; cassettes record resumptions (§13.3). **Sign-off.** |
| R3-DEVOPS-1 | DevOps | Every PR event triggering a resume CI job is cost and noise at org scale. | Resolved: `lockstep:parked` label as trampoline filter — non-parked targets never start a job; label is routing hint, ledger is truth (§13.3). **Sign-off.** |
| R3-SEC-1 | Security | Notification content exits the repo/SCM trust boundary into chat/email/SMS. | Resolved: link-first templates; code content per-channel opt-in, never SMS; `Redact` on every body (§14). **Sign-off.** |
| R3-SEC-2 | Security | Who may resume? A drive-by commenter must not drive a continuation. | Resolved: actor verification via host-native permissions (reviewer legitimacy for approvals; write+ roles for slash-commands); actor ledgered (§13.3). **Sign-off.** |
| R3-SEC-3 | Security | Interactive notification actions (buttons that act) would require an inbound callback endpoint in scope. | Accepted as designed: out of core — humans act in the system of record; an org-run relay is a documented extension point, not shipped surface. **Sign-off with note.** |
| R3-QA-1 | QA | Humans approve a diff, branch moves, continuation acts on different code. | Resolved: head-SHA pinning + `Resumption.staleness` + `on_stale=reassess|rebase|escalate`, default reassess (§13.5). **Sign-off.** |
| R3-SRE-1 | SRE | Sweep over fifty stale parks = notification storm; flapping runs page repeatedly. | Resolved: correlation-key dedup (maps to PD `dedup_key`), per-key throttle, digest batching (§13.6, §14). **Sign-off.** |
| R3-SRE-2 | SRE | Webhook redelivery / double review submission = duplicate continuations. | Resolved: park state machine with `(run_id, event_id)` claim + host `concurrency` group in the resume trampoline (§13.3). **Sign-off.** |
| R3-EM-1 | EM | Parked runs are invisible work that rots; SLAs need teeth. | Resolved: escalation ladders, `ls --parked` inventory, age metrics, Jira-task notifier turning handoffs into board items (§13.6, §14). **Sign-off.** |
| R3-ALL-1 | All | Rename review: "in-lockstep" vs. the SDLC concept; import legality. | Resolved: no collision — SDLC remains the domain term, in-lockstep the product; import package `in_lockstep`; PyPI availability still to verify (open question 3). **Sign-off.** |

### Round 4 — findings against the v0.4 delta (fan-out/fan-in, strategies, skills, audiences, workspaces)

| ID | Persona | Finding | Disposition |
|---|---|---|---|
| R4-STAFF-1 | Staff | Join semantics conflate completion with success; heterogeneous branch outputs need a type. | Resolved: barrier = all-terminal; `JoinResult` maps branch → `Outcome`; aggregation is user policy (§4.7). **Sign-off.** |
| R4-STAFF-2 | Staff | Where does a workspace live? Duplicated decls across repos would drift. | Resolved: control-repo pattern; single repo = workspace of one, existing design unchanged (§15.1). **Sign-off.** |
| R4-DX-1 | Developer | Fan-out debugging: which branch did what, and how do I replay one? | Resolved: per-branch steps, cassettes, spans; `--replay <run> --branch <name>` (§4.7, §9.2). **Sign-off.** |
| R4-DX-2 | Developer | Audience resolver fails at send time (Jira down) — notification lost? | Resolved: `>>` fallback chains, route `fallback_audience`, unresolved deliveries receipted (§14). **Sign-off.** |
| R4-DEVOPS-1 | DevOps | Git-ledger push contention at org scale; repo growth. | Resolved: jittered rebase-retry, one-file-per-run appends; object/DB stores for hot orgs; `ledger compact`. Accepted with monitoring note (§15.3). **Sign-off with note.** |
| R4-SEC-1 | Security | Workspace credentials risk becoming an org-wide god token. | Resolved: per-repo scoped App installations; ledger write scope separate; trampolines can dispatch, not write (§15.4). **Sign-off.** |
| R4-SEC-2 | Security | Skills are a prompt-injection and supply-chain surface. | Resolved: repo-local skills are reviewed code with `TRUSTED_REPO` provenance; external packs hash-pinned like MCP (§5.8, §10.7). **Sign-off.** |
| R4-QA-1 | QA | Strategy sprawl: ten unmeasured strategies is worse than one measured. | Resolved: `strategy_id` + `skillset_hash` in the eval key; selection ledgered; routing changes only via evidence-bearing Improver PRs; new strategies ship eval fixtures by convention, `doctor` warns otherwise (§5.7, §8.2). **Sign-off.** |
| R4-SRE-1 | SRE | Fan-out multiplies cost and parallelism; mixed ERRORED/PARKED branches undefined. | Resolved: joint `CostBudget` across branches, `max_parallel`; barrier waits for terminal states incl. per-branch expiry (§4.7). **Sign-off.** |
| R4-SRE-2 | SRE | Exactly-once continuation firing when barrier ticks race across repos. | Resolved: ledger-write ordering decides the completing tick; git ref CAS makes it atomic (§4.7, §15.3). **Sign-off.** |
| R4-EM-1 | EM | Cross-team governance of a change group spanning repos. | Resolved: each PR gates on its own repo's protection/CODEOWNERS; group merges only when all gates pass; `trace` shows the group (§15.2). **Sign-off.** |

**Termination:** all seven personas signed off in Round 2 with zero blocking findings, and again in Rounds 3 and 4 against their deltas, with accepted notes R3-SEC-3 and R4-DEVOPS-1. Per the process definition — repeat until satisfied — the loop terminates at v0.4.

### Open questions deliberately deferred (tracked, not blocking)

1. **Windows support** for the sandbox path (container fallback exists; native sandboxing TBD).
2. **PyPI name availability** for `in-lockstep` to be verified before publishing.
3. **Interactive notification actions** via an org-run relay endpoint — documented extension point, out of core (R3-SEC-3).
4. **Merge-queue adapters** (GitHub merge queue, GitLab merge trains) for change-group ordered merges (§15.2).
5. **Mixed-host workspaces** (GitHub + GitLab members in one workspace) — expressible via the `Scm` abstraction, untested surface (§15.1).

---

## 17. v0.5 — the implementation delta *(added in v0.5)*

v0.4 was reviewed as a design. v0.5 is what survived contact with an implementation plan: seven SDLC
personas reviewed the pivot plan (Round 5 — all seven returned CHANGES REQUIRED), fifteen contested
questions went to three cross-persona arbitration panels, and two further rounds closed it out. The
amendments below are the design changes those rounds forced. The reasoning lives in
`design/adr/0001-pivot-to-runnable-framework.md`; the executable form lives in `design/gates.md`.

### 17.1 The AI subsystem is built on concrete transport types

§5.1's `Provider.complete(ModelRequest, Credentials) -> ModelResponse` is replaced by the concrete
`LLMProvider.generate(LLMInput) -> LLMOutput`, vendored one-way from `pipeline-framework/src/llm/`.
The signature is preserved byte-identically. `ModelRequest`/`ModelResponse` become
`LLMInput`/`LLMOutput`; `Messages` becomes `list[Message]`; tool declarations and calls become
`ToolDefinition`/`ToolCall`; token counts become `TokenUsage`.

**`resolver.get_provider(config)` is not adopted.** It binds one provider per process from an
ambient `config.llm_provider`, and `LLMInput.model` carries no provider prefix — which makes §2's
own headline example (`route(Verb.TRIAGE, "local:qwen3-8b")` alongside
`route(Verb.IMPLEMENT, "anthropic:…")`) inexpressible. `Model`, `ModelCaps`, `CostTable`,
`ModelRouter`, `Credentials`, `DataPolicy` and a `ProviderRegistry` stay framework-owned above the
transport, and routing is `ModelRouter → Model → registry.provider_for(model)`.

### 17.2 Credentials are a constructor seam *(amends §5.2)*

`generate()` takes no credentials. Providers are constructed only by the `ProviderRegistry` from an
explicit `(ProviderSettings, Credentials)` pair, and **may not read `os.environ`**. `Auth` is the
sole issuer: it mints `Credentials`, registers `creds.secret_values()` with the redaction registry,
and only then returns. Credentials are a property of the connection, not the request, so this
satisfies R1-SEC-2 without changing the adopted call signature.

`data_policy` and `endpoint` live on the **registration**, not the provider class — so §10.6
residency keys on the resolved destination. One OpenAI-compatible provider pointed at
`localhost:1234` and one pointed at a hosted endpoint are two registrations with two policies; a
class-keyed policy would be defeated by an environment variable.

### 17.3 A privileged tier below middleware *(amends §4.4)*

`--no-middleware` is a supported debugging flag and §7.4 commits ledger records to git. `Redact` as
middleware would therefore write unredacted provider errors into a permanently public record.
`KillSwitch` was already specified as running before any middleware; **`Redact`, `EgressPolicy` and
residency enforcement join it**. `Redact` becomes a **default-deny sink filter** wrapping a single
egress writer type — not an enumerated list of sinks, because an enumeration missed stdout/stderr,
OTel span attributes, checkpoints, the `ChangeSet` artifact, and notification bodies.

§4.4's "keeps secrets out of prompts/logs" narrows to **output-side only**; input-side redaction is
a separate mechanism. `Retry` narrows to refuse any action declaring `Capability.SPENDS_BUDGET`:
retrying at `ctx.do` re-runs the whole tool loop and re-pays every prior turn. Transport retry is
owned by exactly one layer, with SDK `max_retries=0`.

### 17.4 Cost is enforced inside the invocation, not around it *(amends §4.4)*

`CostBudget` at the `ctx.do` boundary cannot see the runaway, because a whole agentic loop is one
`ActionCall`. The loop re-sends its accumulated message list every turn, so cost is **quadratic in
turns** and a middleware check only fires once the turns are already paid for. A run-scoped `Spend`
accumulator on `RunContext` is therefore checked **predictively before every turn**, with
`CostBudget` retained as a reconciling post-turn check. `KillSwitch` and an `InvokePolicy` deadline
are re-checked at the same point, for the same structural reason.

An unpriced model is `BLOCKED` before any call, never priced by a default table.

**The daily cross-run ceiling is a recorded loss.** `per_agent_daily_ai_credits` was enforced
out-of-process, per agent workflow per day, before the agent started. Provider-side org spend limits
are stronger on enforcement location but replace a per-repo partition with an org-wide pool — one
runaway repo can consume every other consumer's budget. This is booked as a loss with its
blast-radius consequence named, not presented as a replacement.

### 17.5 Prompts are classes with file-backed bodies *(amends §5.3, §5.8)*

`Prompt` remains a class exactly as §5.3 describes — `version`, `output`, `render`, subclass
extension. The **body text lives in `.md` with YAML frontmatter**, loaded lazily via
`Body.from_file(...)` and resolved through `importlib.resources` at `render()` time, which preserves
§12 import purity. Nothing is generated; the markdown is an input.

This keeps prose reviewable by the people who write it, and it is also a security property: an
Improver-authored prompt change (§8.4) is **data**, not executable code entering `lockstep.py`'s
import graph.

§5.8 gains a `load: always` frontmatter key exempting a skill from progressive disclosure — the
destination for repo knowledge that must reach every invocation.

### 17.6 Standards are a monotone policy stack *(amends §4.5)*

Sealing is not a binding. Bindings resolve repo-above-org by construction (§4.5, unchanged), so a
container cannot express "an org standard the repo may not weaken". Guardrails are an **append-only
registry with a monotone merge**: `lockstep.policy.contribute(...)` appends, there is no removal
API, ceilings merge by `min`, tool denies by union, egress `deny-all` is an irreversible floor, and
the strictest scan setting wins.

The capability preserved is **visibility of removal**, not impossibility — consistent with the
project's own position that sealing "is not an access control against the repository's own owners."
A middleware chain cannot bound code that never calls `ctx.do`; enforcement that must survive a
hostile repo owner lives in CI required checks and provider billing limits, and that is stated
rather than implied.

### 17.7 Outcome gains evidence, not a status *(amends §4.3, §9.1)*

`Status` stays a closed six-member enum. `Outcome` gains `decided: bool` and `reason: str | None`.
Status answers "how did it end"; `decided` answers "did it produce evidence" — orthogonal questions.
An unjudged rubric is a fully successful run that decided nothing, and mapping it to `SKIPPED` would
make it indistinguishable from a cache hit. A boolean also composes under fan-out
(`all(o.decided)`), which a seventh status member does not.

`decided` joins §9.1's `in_lockstep.action.outcome` dimensions (cardinality-safe, ×2): without it a
nightly eval suite that judged nothing emits `SUCCEEDED` and reads green forever. `Cache` refuses to
store an undecided outcome, and `SKIPPED` stays reserved to `Cache` alone.

### 17.8 Eval identity is a content hash *(amends §8.2)*

`EvalSubject` becomes
`(verb, strategy_id, composed_prompt_sha256, skillset_hash, context_recipe_hash, model_id)`, with
`prompt_id@version` demoted to a display label. A *declared* version pools runs whose guardrails,
skills or turn caps differ, and their genuine behavioural difference is then measured **as noise**,
inflating the floor until real regressions read "within noise."

`composed_prompt_sha256` is the **static layer flatten** (guardrails → body → skills → contexts),
not the rendered prompt — the rendered reading makes every subject N=1 and no baseline ever
accumulates. `skillset_hash` is retained separately because §5.8 loads skill bodies by progressive
disclosure, so they are not in the composed text.

Ledger records carry `schema` and `epoch`; comparing across epochs raises rather than averaging.

### 17.9 Security additions *(§10 gains items 9 and 10)*

**9 — Egress control.** Replaces the substrate firewall that in-process invocation removes; it is
not covered by `InvokePolicy`. `EgressMode ∈ NONE | ENFORCED_EXTERNAL | ENFORCED_CONTAINER`, and
`ENFORCED_*` is **probe-verified**, not attested. Mandatory when a ToolSet grants `WRITES_FILES`,
`EXECUTES_CODE` or `REACHES_NETWORK`; when the repo is classified `restricted`; **or when any
`UNTRUSTED_EXTERNAL` item is present in the `ContextPackage`** — the last covers unattended review
of a fork diff, which capability alone exempts. MCP tool capability is taken only from the repo's own
declaration, never a server-supplied hint, failing closed when undeclared.

**10 — Workflow-file provenance.** Config is code discovered at the repo root, so a pull request
would otherwise supply the `lockstep.py` defining every binding, policy, egress mode and path tier
that reviews it. `lockstep.py` and `.in-lockstep/` therefore load from a **trusted ref** — the base
or default branch — while only the content under review comes from head.

**Protected paths** are enforced at two points: the write-tool boundary inside the invoker (which
returns a tool result, so the model can recover in-turn) and `Scm.open_change` (authoritative,
catching writes by third-party MCP servers). Tier 1 leads with `lockstep.py` and `.in-lockstep/**`,
covers CI directories, `.git/**` including hooks, install-time-executing packaging, `CODEOWNERS`,
and anything resolving outside the repo root evaluated on the post-change tree. `prompts/**` is
granted only to the Improver, keyed on its stable `@workflow(id=…)` — never on a strategy id, which
§5.7 permits untrusted ticket labels to select.

`Test` and `Validate` execute **out-of-process** under the §10.3 sandbox: `pytest` collects
`conftest.py` from the repo, so an in-process test run shares an address space with live
credentials.

### 17.10 The trampoline is two jobs *(amends §11)*

§11's single job held `contents: write` and the provider credential together. It becomes two: an
unprivileged `run` job (provider credential, `contents: read`) emitting a `ChangeSet` artifact, and
a privileged `apply` job (`contents: write`, **no** provider credential) running
`in-lockstep apply --from-artifact`, which re-runs the path guard treating the artifact as untrusted.
This reproduces the substrate's privilege split in roughly a dozen lines, and costs nothing extra
because §4.2 already requires every inter-verb type to serialize losslessly. `timeout-minutes`
returns to the template. Notifications dispatch from `apply`.

`init` writes the trampoline once and never reads it back: there is no `--force`, no drift check,
and no `doctor` rule about it. Its output is independent of `lockstep.py` — a property a compiler
cannot satisfy and a scaffolder satisfies trivially. That condition binds the trampoline YAML only;
`init`'s `lockstep.py` scaffold may detect the stack freely.

CLI additions: `apply --from-artifact`, `show-prompt` (renders the composed prompt offline with
per-fragment provenance — the successor to a committed flattened prompt tree), and `ls` promoted to
a Phase-1 deliverable, since it is the entire answer to "config is code, so how do I read it?"

### 17.11 Stability *(amends §12)*

1.0 ships §§1–12 with §13 human boundaries, §14 notifications, §8.1/§8.4 signal harvesting and the
Improver, and §4.7/§15 fan-out and workspaces **unimplemented**. Enum members and shared-state
layouts are not additive changes, while methods are — so the shapes are committed at 1.0 and the
behaviour is deferred: `Status` carries `PARKED` (nothing produces it), `LedgerStore` declares
`compare_and_set` (the protocol default raises `Unsupported`; the shipped in-repo store implements
it and declares `LOCAL` scope), `TestReport` carries `flaky`, step ids are scoped, and `ctx.call` /
`ctx.run_call` exist so branches can be declared rather than started. `ctx.park` and `ctx.fan_out`
are not on `RunContext` at 1.0.

### 17.12 Rounds 5–7 review record

| ID | Finding | Disposition |
|---|---|---|
| R5-STAFF-1 | `get_provider` binds one provider per process; the substitution table silently deleted `Model`, `ModelCaps`, `CostTable`, `Credentials`, `DataPolicy`. | Resolved: transport-only vendoring under a `ProviderRegistry` (§17.1). **Sign-off.** |
| R5-SEC-1 | The substrate egress firewall has no replacement, and `InvokePolicy` was wrongly credited with covering it. | Resolved: §10 item 9 (§17.9). **Sign-off.** |
| R5-SEC-2 | The trust boundary moved before any bounding control existed. | Resolved: controls-parity phase gate; first value is attended, read-only, tokenless. **Sign-off.** |
| R5-SEC-3 | `Redact` unseedable — providers consume credentials inside SDK clients; provider error text reaches a git-committed ledger. | Resolved: constructor seam + privileged sink filter (§17.2, §17.3). **Sign-off.** |
| R5-DX-2 | Prose forced into Python; repo-knowledge contexts had no destination. | Resolved: file-backed bodies, `load: always` (§17.5). **Sign-off.** |
| R5-DX-3 | Sealed guardrails are a capability the container cannot reproduce. | Resolved: monotone `PolicyStack`; loss of *impossibility* recorded (§17.6). **Sign-off.** |
| R5-SRE-1 | `CostBudget` at `ctx.do` cannot see a quadratic in-loop runaway. | Resolved: predictive per-turn `Spend` (§17.4). **Sign-off.** |
| R5-SRE-3 | Blocking SDK calls make `KillSwitch`, wall-clock, cancellation and `max_parallel` unenforceable. | Resolved: async transports; `GATE-ASYNC-1..4`. **Sign-off.** |
| R5-QA-4 | Eval baseline would produce a false cross-architecture comparison. | Resolved: `epoch` + content-hash identity (§17.8). **Sign-off.** |
| R6-SEC-1 | Config loaded from the ref under review — a PR supplies the file defining every control. | Resolved: trusted config ref, §10 item 10 (§17.9). **Sign-off.** |
| R6-SEC-2 | Capability-gated egress exempted the case that matters. | Resolved: `UNTRUSTED_EXTERNAL` trigger + `REACHES_NETWORK` (§17.9). **Sign-off.** |
| R6-QA-4 | `TestReport.flaky[]` and the "fix must not delete tests" guard both absent. | Resolved: commitment 8 + a diff-shape rule in `open_change` (§17.11). **Sign-off.** |
| R6-SRE-2 | A daily ceiling was claimed replaced when it was lost. | Resolved: claim withdrawn, loss recorded (§17.4). **Sign-off with note.** |
| R7 | Close-out across all seven personas. | **Sign-off with notes**; notes landed in phase 0. |

**Termination:** all seven personas signed off in Round 7, with accepted notes on the daily-ceiling
loss (§17.4) and the direct-provider-import bypass, both recorded in the ADR rather than mitigated.
