# Status — Phase 1: the compiler skeleton

Phase 1 delivers a compiler that turns a pipeline spec into a complete, structurally valid workflow
tree, with the drift gate that keeps the two in lockstep. It is a working end-to-end path, not a set
of stubs — but several capabilities named in the design land in Phase 2, and they are listed here
rather than silently missing.

## Implemented

| Area | What works |
|---|---|
| **Spec** | Commands, agents, guardrails, skills (nested paths), contexts, profiles, `mcp/servers.json`, `pipeline.yaml`. Step grammar is a port of the runtime's, extended with `id:`, `targets:`, `min-success-rate:`, `job-boundary:`. Cross-reference validation (unknown agent/command/script/guardrail/skill/MCP/context) fails at compile time. |
| **Orchestration** | Steps → jobs with `needs:` chaining; fusion of consecutive deterministic steps; conditions → job-level `if:`; `pre:`/`post:`/`on-failure:` hooks; nested `command:` → local reusable workflow; per-target step gating so a local-only step never reaches CI. |
| **Parallelism** | `foreach` → `strategy.matrix` fed by a `pipeline-exec fanout` step injected into the producing job; `parallel: N` → `max-parallel`; `fail-fast: false`. |
| **AI steps** | Agent → gh-aw `workflow_call` workflow; guardrails inlined at the top of the body, skills and contexts as ordered `imports:`; provider → engine mapping; `max_tool_turns` → `max-turns`; required credit budgets; MCP servers with allow-lists narrowed by guardrail `enforce:`. |
| **Security** | `permissions: read-all` on every agent; computed named `secrets:` blocks (never `inherit`, never a secret the callee did not declare); computed egress allow-lists; the enforcement floor re-asserted *after* overlays, so a patch cannot reopen what a guardrail closed. |
| **Profiles** | Values exported under both `{PREFIX}_{KEY}` and `PROFILE_{KEY}`; `${NAME}` refs lowered to `secrets.`/`vars.` and refused when undeclared; `environment:` applied only to jobs that consume secrets. |
| **Overlays** | Strategic merge, `insert-step` with `after:`/`before:`, `delete`; anchors keyed on step `id:` so display renames do not break them; `OVL404` with nearest-match suggestion; overlays targeting ungenerated files refused. |
| **Drift gate** | `compile --check` byte-compares committed output against a fresh compile; detects hand edits, missing files, un-recompiled spec changes, and orphans. Ejected files are skipped. |
| **Semantic diff** | Extracts permissions, triggers, egress, MCP allow-lists, safe-output caps, secrets, pins and budgets; classifies deltas as blocking or informational. |
| **Structural validation** | Reusable-workflow jobs carrying disallowed keys, dangling `needs:`, matrices reading undeclared outputs, jobs over the 6-hour limit, and dispatch-input overflow are all compile errors. |
| **Output** | Provenance headers with source and overlay hashes, `compile-manifest.json`, `SECRETS.md`, `.gitattributes`, `--show-surface`. |

## Deferred to Phase 2

Each of these currently emits a compiler note or a clear refusal rather than wrong output.

- **Step caching** — the two-layer content-addressed probe (`step-cache` composite, run-scoped artifact layer, target fingerprints). `force`/`force_steps` inputs are already emitted but unused.
- **Convergence loops** — `max-iterations` unrolling for repair-style loops. Currently a note.
- **State DB** — `state: true` is reported as not lowered. Steps needing shared state should be fused into one job for now.
- **Shard mode** — script-foreach sharding above a threshold. Agent foreach is always matrix legs by design.
- **`fanout-verify` / `min-success-rate`** — parsed, not yet emitted as a gate.
- **Per-command agent variants** — an agent resolving to different prompt layers in different commands is refused rather than silently merged.
- **Deploy modes** — profile `deploy.mode` (services / external / steps), readiness gates, CLI provisioning.
- **`.lock.yml` production** — `gh aw compile` is a separate stage; Lockstep emits the `aw-*.md` sources and the `uses:` references that point at the lock files.
- **`lockstep pin`, `eject`/`uneject`, `upgrade`** — pins.lock is read, not yet resolved by the tool; ejection is honoured by the writer but has no command yet.

## Testing

136 tests, 95% line coverage. The golden tree in `tests/golden/basic/` pins the complete output of the
fixture pipeline; `make golden` rewrites it after an intentional change.
