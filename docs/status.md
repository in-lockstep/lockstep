# Status

## Phase 1 — the compiler skeleton (complete)

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

## Phase 2 — step-type coverage (in progress)

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
- **`lockstep pin`, `eject`/`uneject`, `upgrade`** — Phase 5 lifecycle tooling. `pins.lock` is read but
  not resolved by the tool; ejection is honoured by the writer but has no command yet.

## Phase 3 — the executor package (in progress)

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

## Testing

361 tests, 96% line coverage. The golden tree in `tests/golden/basic/` pins the complete output of the
fixture pipeline, which exercises fusion, fan-out with a coverage gate, caching with a live-target
fingerprint, a state database, nested commands, and a three-iteration convergence loop.
`make golden` rewrites it after an intentional change. `pipeline-exec` adds unit and end-to-end tests
covering a full fan-out cycle in both item and shard modes.
