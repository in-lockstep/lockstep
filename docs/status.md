# Status

All seven phases of the design are implemented. What remains open is listed under each phase and
gathered at the end; nothing is silently missing.

## Phase 1 — the compiler skeleton

| Area | What works |
|---|---|
| **Spec** | Commands, agents, guardrails, skills (nested paths), contexts, profiles, `mcp/servers.json`, `pipeline.yaml`. Step grammar is a port of the runtime's, extended with `id:`, `targets:`, `min-success-rate:`, `job-boundary:`, `max-iterations:`, `fingerprint:`. Cross-reference validation fails at compile time. |
| **Orchestration** | Steps → jobs with `needs:` chaining; fusion of consecutive deterministic steps; conditions → job-level `if:`; `pre:`/`post:`/`on-failure:` hooks; nested `command:` → local reusable workflow; per-target step gating. |
| **Parallelism** | `foreach` → `strategy.matrix` fed by a `fanout` step injected into the producing job; `parallel: N` → `max-parallel`; `fail-fast: false`. |
| **AI steps** | Agent → gh-aw `workflow_call` workflow; guardrails inlined, skills and contexts as ordered `imports:`; provider → engine mapping; required credit budgets; MCP allow-lists narrowed by guardrail `enforce:`. |
| **Security** | `permissions: read-all` on agents; computed named `secrets:` blocks (never `inherit`); computed egress allow-lists; the enforcement floor re-asserted after overlays. |
| **Profiles** | Values exported under both prefixes; `${NAME}` refs lowered to `secrets.`/`vars.` and refused when undeclared; `environment:` only on jobs that consume secrets. |
| **Overlays** | Strategic merge, `insert-step`, `delete`; anchors keyed on step `id:`; `OVL404` with nearest-match; overlays targeting ungenerated files refused. |
| **Drift gate** | `compile --check` byte-compares committed output against a fresh compile. Ejected files are skipped. |
| **Semantic diff** | Permissions, triggers, egress, MCP allow-lists, safe-output caps, secrets, pins, budgets and turn caps; blocking vs informational. |
| **Structural validation** | Reusable-workflow jobs with disallowed keys, dangling `needs:`, matrices reading undeclared outputs, over-limit timeouts, dispatch-input overflow. |

## Phase 2 — step-type coverage

### Done

**Skip-on-cached-output.** Steps declaring an output are wrapped in a content-addressed probe/save
pair. The key covers the normalized step definition (written to `.pipeline/step-defs/` and hashed
like any other file), the script's own content, upstream outputs the step reads — so invalidation
cascades down the DAG — the profile fingerprint, and runtime input expressions. `--output-dir=`
deliberately never participates: a directory always exists, so treating it as a skip signal would
skip work that never ran. `force` and `force_steps` are threaded through to the probe; a cache hit
skips the step's hooks along with the step, because the hooks are part of the step.

**Live-target fingerprints.** A step may declare `fingerprint: <shell command>`; its output joins the
cache key. Repo files cannot describe the state of a deployed application, so without this a staging
redeploy that renames endpoints would serve stale discovery output indefinitely. A fingerprint that
fails or returns nothing fails the step rather than falling back to a cached result.

**State database.** `{state_db}` expands to a workspace path and the owning job gets load/save steps
around it. The compiler is total about scope: state used from two different jobs is a compile error
naming the offending steps, and state used inside a `foreach` is refused outright — the file travels
as a last-writer-wins artifact, which is fine within one job and silently lossy across parallel ones.
`state: keep` becomes retention on the save step. Declaring state without using it is reported.

**Convergence loops.** A nested `command:` step with `max-iterations: N` unrolls into N chained jobs,
each skipped once the previous reported convergence. The callee declares `converged-from: <step-id>`,
which publishes a `converged` workflow-call output. Actions has no `while`; the bound is therefore a
compile-time decision, which is a better habit than an unbounded local loop.

**Partial-failure policy.** A `foreach` step declaring `min-success-rate: R` gets an explicit coverage
gate: a verify job gated on `!cancelled()` running `fanout-verify` against the expected item list,
which downstream jobs depend on instead of the matrix. Without a declared rate the default stays
strict, matching plain `needs:` semantics. This restores the local runtime's "save each item and keep
going" behaviour as an inspectable policy rather than an accident of dependency handling.

**Addressable steps.** Every emitted run step carries an `id:` derived from its spec step id, so
convergence outputs and overlay `insert-step` anchors both have something stable to point at.

### Still open

- **Deploy modes** — profile `deploy.mode` (services / external / steps), readiness gates, CLI
  provisioning for CLI-session tests.
- **Per-command agent variants** — an agent resolving to different prompt layers in different commands
  is still refused rather than emitted as variants.
- **`.lock.yml` production** — `gh aw compile` remains a separate stage.
- **`upgrade` with migration maps** — the rest of Phase 5 shipped; this did not. See the end of this
  document.

## Phase 3 — the executor package

`pipeline-exec` is a second workspace package in this repo. One repository, two distributions: the
compiler emits `pipeline-exec …` invocations as literal text, so the two are developed and tested
together — but a generated pipeline repo installs only the runtime, never the compiler.

### Done

**Fan-out, including the sharding decision.** `fanout` turns a JSON array into a matrix, dropping
items whose output already exists (`--only-missing`), refusing counts above the matrix cap, and —
above a threshold — emitting shard descriptors instead of items. This is where Phase 2's deferred
shard mode landed, and the reason it belongs here rather than in the compiler: the threshold is a
question about *how many items there are*, which is a runtime fact. The compiler emits the same
matrix expression either way. Agent fan-out passes `--no-shard`, because an agent leg is a whole
gh-aw run and cannot host more than one item.

**`shard-run`** accepts either shape the matrix can carry — one item, or a shard covering many — and
substitutes `{item}` / `{item.field}` per item. Every item runs even after one fails, so a bad item
costs its own output rather than the rest of the slice; the leg still exits non-zero.

**`fanout-verify`** enforces the `min-success-rate` policy Phase 2 emits, writes coverage to the step
summary, and names what is missing.

**`validate-schema`** validates and sanitizes agent output at the trust boundary: JSON well-formedness,
required keys, control-character and markup stripping, and field-length caps. An absent output
directory is reported, not failed — the producing step may have been skipped, and failing here would
mask the real cause.

**`wait-for`** blocks until an application answers, so tests do not race a still-booting target and
send the repair loop chasing a startup race.

**Contract tests.** `tests/test_contract.py` extracts every `pipeline-exec` invocation the compiler
emits and parses it against the real CLI, and asserts that the compiler's declared builtin list and
matrix cap match the runtime's. A `builtin:` step naming a command `pipeline-exec` does not provide
is now a compile error rather than a `command not found` at 2am.

**Fixed while wiring this up:** a `foreach` step that declared an output was being step-cached, and
every matrix leg computed the *same* key — so one leg publishing would have made all legs skip on the
next run. Per-item skipping belongs to `fanout --only-missing`, which drops covered items before the
matrix exists and never starts a runner for them; `foreach` steps are no longer step-cached.

**The extraction from pipeline-framework.** The API, browser and CLI session executors, the direct
executor, the test runner, API discovery and the report renderer were copied across, along with
`collect-failures` and `check-convergence`. They arrived as `test-runner`, `discover`, `report`,
`collect-failures` and `check-convergence` on the CLI.

The executors carry resilience behaviour earned against a real application — 409 conflict recovery,
422 auto-recovery, PATCH/PUT fallback, rate-limit and transient retry ladders, browser auto-login and
crash recovery, runtime variable tracking, teardown 404 tolerance. That behaviour is the asset, so
the modules were copied verbatim and only their imports and configuration plumbing were adapted.
Equivalence was checked by comparing normalized ASTs against the originals, which surfaced two
changes an autofix had made: `asyncio.TimeoutError` → `TimeoutError` (the same object on 3.11+) and
the removal of an unused `as primary` binding. Both are safe; the extracted tree is now excluded from
formatting and from cosmetic lint rules so a routine `make fmt` can never silently rewrite it again.

Two deliberate adaptations:

- **Configuration.** The framework's `Config` spans LLM providers, Jira and the orchestrator. None of
  that belongs here, so `ExecConfig` reads only the profile — from the `PROFILE_*` block the compiler
  already exports to every job. A contract test asserts the two conventions match.
- **Tag filtering** dropped a hardcoded rule that auto-skipped `ocp`-tagged tests when `OCP_API_URL`
  was unset — one application's integration leaking into a general package. `.env-tests` now takes
  `TAG_<name>=skip-unless-env:VAR`, which expresses the same intent as a declaration. A pipeline
  relying on the old behaviour needs that one line.

**The extracted tree no longer matches the origin, deliberately.** The tag-filter adaptation above
turned out to be the first of a class rather than a one-off: an audit found 322 lines of one
application's knowledge across the extracted modules — a sign-in page's selectors, a domain model's
field defaults, that organisation's environment-variable prefixes, its endpoint map. `docs/layers.md`
states the rule that identifies them and the tiers they belong to; the audit table there lists every
one and where it went. The pattern is the same in each case: the algorithm was never the
application-specific part, so the algorithm stayed and the strings moved out to something the
pipeline declares. Behaviour that is now off by default when nothing is declared — automatic browser
sign-in, 422 field recovery — is off because the alternative was a guess that failed at the target
rather than at the compiler. `login.py` and `recovery.py` are new modules, written here and held to
this repository's rules; the code they replaced could not be tested without that one application in
front of it, which is how it survived.

**The exec image** (`packages/pipeline-exec/docker/`) builds on the Playwright base and adds the
runners the compiler dispatches on by extension. Workflows reference it by digest, never by tag.

### Still open

- **Deleting the framework's copy.** `pipeline-framework` still holds the originals. Removing them and
  depending on `pipeline-exec` is a change to that repo, which currently has substantial uncommitted
  work in it.
- **`cost-rollup` and `collect-patterns`** — deferred until the phases that emit them (token
  accounting and the learning loop), rather than built speculatively against no caller.
- **Coverage of the session executors.** They drive a real browser, API and shell against a running
  application and cannot be covered without one; they arrived with no tests of their own. The gate
  omits them and `make cov-all` reports the true figure (68%). Closing this needs a fixture
  application, which is Phase 4 territory.

## Phase 4 — the capability actions

Every compiled workflow references composite actions by SHA. Until now they were fictional, which
made generated workflows un-runnable. They live in [`actions/`](../actions): `restore`, `save`,
`state/load`, `state/save`, `step-cache` and `step-cache/save`.

The interesting logic lives in `pipeline-exec` where it can be unit-tested — `cache-key` computes the
content-addressed key, hashing file *contents* so a fresh checkout does not invalidate every step,
and hashing a missing input distinctly from an empty one so an unproduced upstream output cannot
share a key with a produced one. The YAML stays thin.

`step-cache` consults two layers in order: a named artifact from an earlier run, which outlives the
cache's eviction window, then `actions/cache`. It also verifies that a reported hit actually left the
declared outputs on disk, because a partial restore must re-run rather than silently skip.

**Building the actions surfaced a compiler bug:** the durable layer looks up artifacts from previous
runs through the API, which needs `actions: read` — and the compiler was emitting `contents: read`
only. Jobs that probe the cache now request it, and a contract test asserts they always will. Two
further contract tests assert that every input the compiler passes is declared by the action, every
output it reads exists, and every required input is supplied.

## Phase 5 — lifecycle tooling

**`lockstep lint`** asks whether the spec is well built: every agent evaluated, every script tested,
no agent spending tokens on sorting and deduplication, no fan-out left serial. **`lockstep doctor`**
asks a different question — whether the target will accept it: pins resolved, engines mapped, budgets
set, credentials declared, MCP servers carrying tool lists, timeouts within the platform limit. A
spec can be excellent and un-deployable, so conflating the two would make both easier to ignore.

**`lockstep pin`** resolves capability tags to the commits that will actually run, and reports when a
tag has been moved since it was pinned — the supply-chain event pinning exists to catch.

**`lockstep eject` / `uneject`** are the escape hatch for the file the spec cannot express. Ejecting
snapshots the generation it forked from, so a later merge has a real base; `compile --check` then
reports when that source moves on, keeping fork debt visible instead of silent.

**The policy gate.** `--fail-on-blocking` fails a build when the security surface changes — a new
write permission, a new trigger, a new egress host, a widened MCP allow-list. Building it exposed a
gap in the design: comparing against the working tree answers "did you forget to recompile", which
the drift gate already covers, so an author who recompiled correctly would sail through. The question
a reviewer needs answered is what *merging* would change, which is only visible against the base
branch. Hence `--base`, which the generated gate passes automatically.

**Generated `pipeline-ci.yml`** wires all of it into the compiled repo: drift gate with the policy
gate against the base branch, lint, doctor, and the scripts' own tests. Every job is read-only, and
each installs the pinned compiler as a tool rather than syncing the repository's environment — a
check must not execute project-defined build hooks in order to run.

## Phase 6 — `lockstep init`

The scaffold is a working pipeline, not a set of empty directories. It compiles, lints clean, and
demonstrates the one shape that matters: a deterministic step producing work, an agent fanned out
over it with a coverage policy, and a deterministic step consuming the results. Someone reading it
can see where their own work goes.

It ships with an eval case for its agent and a unit test for its script, so `lint` passes from the
first commit rather than starting in the red — a linter that is red on day one is a linter people
learn to ignore.

Scaffolding surfaced two gaps: `lockstep pin` was not resolving the third-party actions the compiler
emits, and `doctor` was not checking for them, so a freshly initialised pipeline would fail to
compile with no check pointing at why. `pin` now resolves everything it can and reports what it
cannot, rather than aborting on the first unreachable reference — a placeholder capability repository
should not stop `actions/checkout` from being pinned.

The whole day-one path is covered end to end: init, pin, compile, then a clean lint, doctor and drift
gate.

## Phase 7 — conformance

Golden tests prove the compiler emits the same text twice. They say nothing about whether that text
*behaves* like the spec it came from. `lockstep.conformance` walks the emitted graph instead:
resolving `needs`, evaluating the `if:` expressions the compiler produces, and reporting which jobs
would run in which order. It understands exactly the grammar the compiler emits and refuses anything
else — an expression it cannot read is either a bug here or an emission nobody has reasoned about.

That found two real bugs in a single afternoon, both invisible to every other test:

1. **A conditional step skipped everything after it.** Actions skips a job whose dependency was
   skipped, so `(if not --skip-discovery)` did not skip discovery — it skipped the entire pipeline.
   Jobs downstream of a conditional one now carry `!failure() && !cancelled()`, the documented idiom
   for "upstream succeeded or was skipped".
2. **Convergence loops did not terminate correctly.** Each unrolled iteration checked only its
   immediate predecessor, and a *skipped* job's output is empty — which reads as "not converged". So
   converging at iteration 1 still ran iteration 3. Each iteration now checks every prior one.

Neither is visible in a golden diff, and neither would have surfaced before a scheduled run behaved
strangely in production.

The suite also asserts the properties that make a compiled graph trustworthy: no dependency cycles,
job order follows step order, every step that targets GitHub reaches a job, local-only steps reach
none, and — by trying every combination of boolean inputs — no job is unreachable.

### Not built

**Round-trip evals across backends.** The design calls for running the same eval cases through the
local runtime and the compiled output and diffing the scores. That needs `pipeline-framework`, which
this repo deliberately does not depend on. It stays open, and it is the one remaining gap between
"the compiled graph behaves as specified" and "both backends behave alike".

**The fleet dashboard.** Reading many pipeline repositories' pins and manifests to report who is
behind. It needs real consumer repositories to be worth anything; building it against none would be
speculation.

## Testing

Over 750 tests. `make ci` fails below 90% line coverage across the compiler and the runtime
together; the figure is currently 91%, and 94% for the compiler alone. Exact counts are deliberately
not repeated here — a number nobody can maintain is the first thing in a status document to go
stale, which is a poor look on a tool that exists to catch drift. `make ci` is the source of truth.
The golden tree in `tests/golden/basic/` pins the complete output of the
fixture pipeline, which exercises fusion, fan-out with a coverage gate, caching with a live-target
fingerprint, a state database, nested commands, and a three-iteration convergence loop.
`make golden` rewrites it after an intentional change. `pipeline-exec` adds unit and end-to-end tests
covering a full fan-out cycle in both item and shard modes.

## Ceilings on agents an upstream never wrote

`enforce:` on a guardrail now carries `max-turns`, `max-ai-credits` and `per-run-ai-credits`. A band
bounds a dial on an agent upstream published; a ceiling bounds every agent in a consuming repository,
including ones it wrote itself — the gap bands left open. Sealed guardrails already reach every agent
unnamed, so the mechanism is the one that was there. Lowest ceiling wins; zero and non-numbers are
refused at parse time; the run cap is checked against the consumer's own `budgets.per_run_ai_credits`
and an absent budget is refused rather than passed. Enforced in `verify_enforcement`, after overlays.

It stops drift, not a determined fork: a consumer can delete `inherits:`. What it guarantees is that
removing a ceiling is a diff on a pull request, gated by `compile --check`. `docs/sharing.md` Part 4.

## Self-hosting

This repository compiles its own drift gate. `.lockstep/` holds a manifest and a profile; they
compile to `.github/workflows/pipeline-ci.yml`, which recompiles and byte-compares on every pull
request that touches the spec **or** `src/`. `docs/self-hosting.md` covers the two things that are
different when the compiler is the repository — the gate installs the checkout rather than a
release, and the hand-written `ci.yml` stays hand-written — and the defects it surfaced: `doctor`,
`pin` and `show-surface` all treating a capability the output never names as unpinned rather than
unused, `DOC007` on a pipeline with no agents, `.gitattributes` marking workflows the compiler did
not write, and the `scripts` job not triggering on the tests it runs.

It stops at the drift gate. A pipeline with any script or builtin step compiles to a job with a
`container:`, and that image is one of the unpublished capabilities below.

## Reading a private upstream

A consumer's `GITHUB_TOKEN` is scoped to the repository it belongs to and cannot read another one,
so a private upstream needs a credential from elsewhere. That was listed here as something the
framework could not solve for an organization, which was half true: it cannot create or install an
App, but it was also not wiring in the credential once somebody had.

`inherits-auth:` now declares it — a GitHub App (`app-id` + `private-key`) or a plain token — and the
compiler emits the minting step and the fetch environment, pins `actions/create-github-app-token`
like any other action, and lists what to set in `SECRETS.md`. `docs/inheriting.md` carries the setup
recipe and the argument for an App over a PAT.

`lockstep fetch` authenticates the way `actions/checkout` does, with a per-invocation header rather
than a token in the remote URL, and redacts the credential from any error it raises — git quotes
URLs back, and a credential in a build log is a leaked credential.

Requiring the App action to be pinned everywhere would have made every existing lock file fail
DOC012, so `Spec.external_actions_used()` answers which third-party actions a pipeline's output
actually references — the same shape as `capabilities_used()`.

## Eval cases assert something now

`lockstep lint` refused an agent with no eval cases and checked that a `.json` file existed. Every
case in the repository carried an `expect.notes` string — prose addressed to a human, in a file no
program opened. An agent could be rewritten from scratch without a case noticing.

`docs/evals.md` is the contract. Two halves, the same split `enforce:` draws: `schema`, `equals`,
`contains`, `absent` and `count` are applied by `pipeline-exec eval-grade` and mean the same thing
every run; `rubric` is prose for the part no substring match settles. A case must assert at least
one — LNT008 refuses one that asserts nothing, because it passed before it was written. LNT007
refuses one that will not parse or says nothing about its input.

The grader never reports a rubric case as passed. It reports the deterministic half as decided and
the rubric as outstanding, and the roll-up counts those separately: a suite claiming 4/4 while half
of it was never judged is the reassuring number the contract exists to remove. A case with no output
file fails rather than being skipped — the agent was asked and did not answer.

`lockstep compile` now emits `evals.yml`: per agent with cases, a job to expand the cases into agent
inputs, a matrix that runs the agent once per case through **the same compiled workflow the pipeline
calls**, an optional pair of jobs that judge rubrics, and a grading job that gates. It never runs on
every push — dispatch, or a change to the prompt layers it covers, which is the only thing that can
move an agent's behaviour.

The judge is an agent the pipeline declares, not one the framework ships: a framework-provided
prompt deciding whether your agents pass could not itself be evaluated without evaluating the
evaluator. Without one the deterministic half still runs and rubrics stay undecided.

Nothing has executed. The suite compiles and is covered by the drift gate like every other workflow
here; it runs the first time an agent runs at all, which needs the capabilities published.

Migrating the 22 existing cases found two in `examples/httpbin` asserting exact field values through
keys nothing read, which is where the `equals` expectation came from.

## The gh-aw seam

`lockstep compile` emits an agent as markdown; `gh aw compile` turns that markdown into the
`.lock.yml` a runner executes. Everything the drift gate proved stopped one layer above that file: a
reviewer approved a turn limit and a tool deny-list in a document GitHub never reads, and no lock
file had ever been produced at all, so every orchestrator referenced a workflow that did not exist.

The seam is inside the gate now. `lockstep compile` produces the lock files, `compile --check`
regenerates them from the committed markdown and byte-compares, and a missing `gh aw` is an error
rather than a skip — a check that could not look at the artifact has not checked it. The generated
drift job installs the pinned extension itself.

Two properties make it work, both established by running the tool rather than assumed. `gh aw
compile` is **deterministic**: the same markdown gives byte-identical output across runs, which is
what makes byte-comparison a gate rather than a coin toss. And its safe-update approval of new
secrets and actions is recorded **inside the lock file**, so a committed lock file is its own
baseline and regeneration beside it needs no interactive approval. A version other than the one
`capabilities.gh-aw` pins is refused, because a lock file compiled by a different version is a
different file and comparing them proves nothing.

Running the real tool corrected three things the compiler had wrong:

| Found | Was | Now |
|---|---|---|
| `engine.model` is deprecated | emitted nested, warned on every compile | top-level `model:` |
| the pinned version did not exist | `gh-aw: v0.34.0`, against v0.86.2 installed | pinned to a version that exists |
| no lock files anywhere | orchestrators named files never produced | 14 committed, covered by the gate |

It also confirmed two claims that had been assertions: `max-turns` reaches the agent CLI as
`--max-turns`, and `max-ai-credits` reaches the API proxy as `GH_AW_MAX_AI_CREDITS`. Both are
substrate. `ANTHROPIC_API_KEY` is the credential gh-aw asks for, which is what `ENGINE_SECRET`
already said.

**One claim it corrected in the other direction.** `enforce.network: deny-all` does not produce zero
egress: gh-aw compiles a squid firewall with its own baseline allow-list — the model API, GitHub,
package registries, certificate authorities. What deny-all means is *no domains beyond that
baseline*, which is a real constraint and not the one the wording implies. `docs/layers.md` and the
semantic diff describe it accurately now.

## Pinning the compiler, and naming the engine credential

Two things a pre-launch review found missing from a system whose premise is that nothing floats.

**The compiler was the one floating dependency.** Actions pin to a commit, the executor image to a
digest, inherited pipelines to a commit — and then the check enforcing all of that installed its own
compiler from a version range. A release could change what a consumer's security gate ran without a
line changing in their repository. `lockstep pin` now records the compiler that produced the
committed output, which is the only version known to reproduce it, and the gate installs
`in-lockstep==<version>`. A local-path compiler — this repository compiling itself — is passed
through, because the checkout is the version. `DOC023` reports an unpinned compiler; `DOC024` reports
compiling with a version other than the pinned one, which means the committed output came from one
compiler and is about to be checked by another.

**`SECRETS.md` omitted the engine credential.** A document titled "every secret this pipeline needs"
listed the profile's secrets and not the key the model authenticates with — the most sensitive one in
the system. It went unlisted precisely because nothing this compiler emits references it: it is read
by the workflows `gh aw compile` produces. It is now derived from the engines in use, named with the
agents that need it, and shown in `lockstep show-surface`. `ENGINE_SECRET` is gh-aw's contract, not
this compiler's, and `capabilities.gh-aw` pins the version it was written against.

## TLS verification

`executors/api_session.py` verified no certificates: `check_hostname = False`,
`verify_mode = CERT_NONE`, and an unverified client on every request, in a runtime that holds a
profile's credentials and talks to whatever host a pipeline names. It came in with extracted code
and `docs/layers.md` recorded it as a decision deferred rather than an oversight.

Taken now. Verification is the default. A profile facing a genuinely self-signed certificate
declares `insecure_tls=true` — visible in the spec, scoped to one profile, warned about at runtime,
never inherited. `packages/pipeline-exec/tests/test_tls.py` fails if any client in that module is
constructed with verification off, so the unconditional version cannot come back quietly.

## Distribution names

The two distributions are `in-lockstep` (the compiler) and `in-lockstep-exec` (the runtime). The
bare names `lockstep` and `pipeline-exec` both belong to unrelated projects on PyPI, and a generated
drift gate runs `uv tool install "<capabilities.compiler>"` against a public index — so a name this
project does not own is not merely an install that fails. It is one that could succeed, resolving to
somebody else's package, inside every consumer's security gate.

Import names and console scripts are unaffected: the module is still `lockstep`, the commands are
still `lockstep` and `pipeline-exec`. Two contract tests hold the line — one binds
`capabilities.compiler` and `capabilities.exec` to the names in the two `pyproject.toml` files, the
other scans every shipped spec for a capability naming a distribution this repository does not build.

## What remains open

**The capabilities have not been published yet.** `capabilities.actions` points at
`github.com/in-lockstep/lockstep/actions` and `capabilities.exec-image` at `ghcr.io/in-lockstep/pipeline-exec`.
The release workflows exist — `release-actions.yml` and `release-exec-image.yml`, on `actions-v*` and
`exec-v*` tags — but no tag has been pushed, so every example and fixture here still pins both to
forty zeros.

The composite actions need no separate repository: `uses: in-lockstep/lockstep/actions/restore@<sha>`
resolves against a subdirectory of this one, and `resolve_ref` already slices `repo.split("/")[:2]`
to pin it. That removes a second repository and the cross-repository push credential a split would
have needed. The two capabilities keep their own tag lines rather than the compiler's, so a pin moves
when the thing behind it moved.

That was invisible for longer than it should have been — a zero has the shape of a pin, so the
examples compiled, linted and simulated exactly like ones that would run. It is now stated in three
places rather than left to be noticed: `lockstep doctor` reports `DOC015` as an error and treats a
placeholder as unpinned, `lockstep compile` prints "this output cannot run as emitted" on every run,
and the readiness tests assert the disclosure is present rather than asserting a clean bill of
health. `tests/test_pinning.py::test_every_example_is_honest_about_being_unpublished` fails if an
example ever stops saying so.

The check catches zeros, not invention. Nothing offline can tell a fabricated commit from a real one
— the `basic` fixture's actions SHA is made up and looks entirely plausible. Only `lockstep pin`
contacting the remote can settle that, which is why it reports what it could not resolve instead of
leaving something believable in place.

| Gap | Why it is not built |
|---|---|
| Transitive inheritance | An import that imports is a package manager. A consumer lists every upstream directly — fan-in is supported and documented in `docs/sharing.md`; only following an upstream's own `inherits:` is refused. |
| A `/review` on this repository's own pull requests | Needs the first `actions-v*` and `exec-v*` tags pushed. The lenses are designed — `docs/self-hosting.md` names them. |
| Round-trip evals across backends | Needs `pipeline-framework`, which this repo deliberately does not depend on. The conformance suite proves the compiled graph behaves as specified; this would prove both backends behave alike. |
| Deleting the framework's copy of the executors | A change to a repository with substantial uncommitted work in it. |
| The fleet dashboard | Needs real consumer repositories to report on. |
| Per-command agent variants | An agent resolving to different prompt layers in different commands is refused rather than emitted as variants. |
| Deploy modes | Profile `deploy.mode` (services / external / steps), readiness gates, CLI provisioning. `wait-for` exists; nothing emits it yet. |
| `cost-rollup`, `collect-patterns` | Deferred until token accounting and the learning loop have a caller. |
| Coverage of the session executors | They drive a real browser, API and shell against a running application. `make cov-all` reports the true figure; closing it needs a fixture application. |
| `upgrade` with migration maps | Pinning, ejection and the drift gate are in place; automated overlay-anchor migration across capability majors is not. |
