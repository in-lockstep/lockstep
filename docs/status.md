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

- **Shard mode.** Deferred deliberately, and not merely unfinished: the threshold is a decision about
  *how many items there are*, which is a runtime fact. The compiler cannot know it, so the branch
  belongs inside `pipeline-exec fanout` — emit item legs below the threshold and shard descriptors
  above it — with the compiler emitting the same matrix either way. That makes it Phase 3 work, not
  Phase 2. Agent `foreach` stays one leg per item regardless, since each item needs its own gh-aw run.
- **Deploy modes** — profile `deploy.mode` (services / external / steps), readiness gates, CLI
  provisioning for CLI-session tests.
- **Per-command agent variants** — an agent resolving to different prompt layers in different commands
  is still refused rather than emitted as variants.
- **`.lock.yml` production** — `gh aw compile` remains a separate stage.
- **`lockstep pin`, `eject`/`uneject`, `upgrade`** — Phase 5 lifecycle tooling. `pins.lock` is read but
  not resolved by the tool; ejection is honoured by the writer but has no command yet.

## Testing

179 tests, 96% line coverage. The golden tree in `tests/golden/basic/` pins the complete output of the
fixture pipeline, which exercises fusion, fan-out with a coverage gate, caching with a live-target
fingerprint, a state database, nested commands, and a three-iteration convergence loop.
`make golden` rewrites it after an intentional change.
