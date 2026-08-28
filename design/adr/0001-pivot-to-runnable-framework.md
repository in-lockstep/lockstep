# ADR 0001 — Pivot from YAML compiler to runnable framework

**Status:** Accepted · **Date:** 2026-08-28 · **Supersedes:** the compiler line (`in-lockstep` 0.1.x)

## Context

`design/in-lockstep-design.md` describes a framework whose first principle is *"Runnable, never
rendered."* This repository is the opposite artifact: a compiler that lowers a markdown/YAML spec
into GitHub Agentic Workflows (~4.8k LOC `emit/`, ~1.8k LOC `spec/`), plus `pipeline-exec`, the
runtime those generated workflows call.

Three facts decided this, and all three are unusual:

1. **Nothing has ever executed.** `docs/needs.md` N2: *"Nothing has executed yet… there are no
   adopters yet."* The published artifacts were never exercised.
2. **The whole product was built in three days** (95 commits, 2026-08-22 → 2026-08-24). Sunk cost is
   days of AI-assisted work.
3. **The ledger holds no evidence.** `origin/pipeline-history`: 11 records, 0 eval records, every one
   `tokens: 0`, `cost_usd: 0.0`, `models: {}`.

So compatibility is worth nothing and unproven mechanisms are worth less than nothing. What is
*not* cheap is the irreversible surface: PyPI versions cannot be reused, published digests can be
retention-pruned, and the composition invariant dies with `emit/fragments.py`.

## Decision

**Full pivot.** `src/lockstep/` is replaced by `src/in_lockstep/`, deleted last so every phase
leaves `main` working. Seven phases to 1.0; §13 human boundaries, §14 notifications, §8.1/§8.4
learning loop, and §4.7/§15 fan-out and workspaces are post-1.0.

### Distribution

Reuse the **`in-lockstep`** name; 1.0 is the framework. One distribution with provider extras
(`pip install 'in-lockstep[anthropic]'`); the compiler archived behind tag `compiler-v0.1.x` with a
README notice and **no formal deprecation window** — there are no adopters. Accepted consequence: a
loose `>=0.1` constraint silently swaps products. Mitigated only by the fact that no such
constraint exists in the wild.

`actions-v0.1.4` tags are permanent and are never retagged. The GHCR image stops receiving
`:latest`; a final `exec-v0.1.x` is cut. Neither is deleted.

#### Amendment (post-1.0): `in-lockstep-exec` is deleted, not folded in

This decision originally said `in-lockstep-exec` was "folded in." It was not, and the word was
doing work the code never did. The second distribution survived all seven phases untouched and
arrived at 1.0 with **nothing in the framework importing it** — 7,928 LOC and 478 tests reachable
only by running its own CLI.

It is now deleted outright. The reasoning is the same one this ADR opens with: most of its command
surface (`fanout`, `shard-run`, `cache-key`, `meter`, `eval-*`, `parse-command`, `scan-input`) was
glue a compiler emitted as literal text, and it describes a system that no longer exists. Keeping
it "in case the `run` verb wants it" was keeping a fixed answer to a question the `run` verb has
not been asked yet, and it was doing so at the cost of a lint, type-check and coverage surface
nobody was maintaining. The reasons it was kept in the first cleanup pass — that it was working,
tested code — are reasons not to delete it *accidentally*, which is why this is a recorded decision
rather than a tidy-up.

What goes with it, stated rather than discovered later:

| Deleted | Consequence |
|---|---|
| `executors/` — browser, API and CLI session drivers with 409/422 recovery, method fallback, rate-limit ladders, browser auto-login and crash recovery | The `run` verb, when it lands, starts from the design rather than from behaviour earned against a real application. This is the one genuine loss. |
| `builtins/test_runner.py`, `builtins/discovery.py`, `reports/` | Live-application test running and its report surface. `PytestTest` was always new code, never a port of this. |
| `improvement.py` — the noise-aware comparator | Post-1.0 §8.4's eval loop loses a starting point. The *method* is recorded in the plan and in `design/in-lockstep-design.md` §8; only the implementation goes. |
| 478 tests, and `GATE-TEST-5`/`GATE-TEST-6` with them | Both gates are marked retired-with-subject in `design/gates.md` rather than removed, so the count they defended does not read as having silently eroded. |

Recoverable from git history and from `in-lockstep-exec` on PyPI, both of which are permanent. The
GHCR image and the `exec-v0.1.x` tag are still not deleted.

### The AI layer

Adopt `pipeline-framework/src/llm/` as **transport only**: `LLMProvider.generate(LLMInput) ->
LLMOutput` is kept byte-identical. `resolver.get_provider()` is **dropped** — it binds one provider
per process from ambient config, which makes the design's own per-verb routing example
inexpressible. `Model`, `ModelCaps`, `CostTable`, `ModelRouter`, `Credentials`, `DataPolicy` and a
`ProviderRegistry` stay lockstep-owned above it.

Credentials are injected via the **constructor**, not the call — credentials are a property of the
connection, not the request, so `Auth` can seed `Redact` at mint time without changing the adopted
signature.

Vendored one-way at origin commit `6ac3cde`, with the async rewrite applied on the way in.

### Capability losses, recorded rather than argued away

Moving model invocation in-process deletes gh-aw's out-of-process envelope. Each loss is booked:

| Lost | Replacement | Honest status |
|---|---|---|
| Squid egress firewall (~55-domain allowlist) | `EgressPolicy`, probe-verified, mandatory on write/exec tools **or any `UNTRUSTED_EXTERNAL` context** | Replaced, narrower allowlist |
| API proxy enforcing `maxAiCredits` out-of-process | `Spend`, checked predictively inside `AiInvoker` | Replaced, but now in-process — it cannot bound a process holding the raw key |
| `max-daily-ai-credits` (per agent, per day, pre-flight) | Provider-side org spend limits + CI `concurrency` | **Genuine loss.** Enforcement moves out-of-process (stronger) but from a per-repo-per-day partition to an org-wide pool: one runaway repo can consume every other consumer's budget. The earlier claim that this was "strictly stronger" is withdrawn. |
| Safe-outputs privilege split | Two-job trampoline (`run` unprivileged, `apply` privileged) | Replaced |
| Workflow-file provenance (*"a fork cannot modify the workflow that reviews it"*, the then-`docs/adopting.md:220`, deleted in phase 7) | Trusted config ref — `lockstep.py` loads from base/default ref | Replaced |
| Sealed guardrails enforced at compile time | `PolicyStack`, append-only, monotone merge, `doctor --strict` | Weaker: visibility of removal, not impossibility. Consistent with the then-`docs/sharing.md:505` (deleted in phase 7), which already stated sealing "is not an access control against the repository's own owners." |
| Compile-time refusal of an undeclared budget (`DOC006`, ERROR) | Startup refusal, `GATE-BUDGET-1` | Replaced |

A repo can also import a provider directly and bypass the entire `PolicyStack`. That is
unrecoverable in a library architecture and is recorded, not mitigated.

### Migration

**No shipped importer. 0.x specs are frozen; `in-lockstep==0.1.0` remains installable forever.** A
throwaway `tools/` converter runs once over this repo's own examples, `.lockstep/`, and fixtures,
then is deleted with `src/lockstep/`. Defensible at zero adopters, and the option survives deletion
regardless: `src/lockstep/` persists in git history and on PyPI. Closes `docs/needs.md` N7.

### The self-hosting gate

The generated drift gate is **retired**, not narrowed. `pipeline-ci.yml` is a generated file whose
`watch: src/**` and `capabilities.compiler: "."` are deliberate, commented decisions; hand-editing
it is illegal and recompiling it regenerates the problem. `GATE-TEST-4` (golden-tree hash pinned to
the Phase-0 tag) covers the same ground from pytest and is the stated replacement.

`make fetch` keeps running the working-tree compiler. Pinning it to the released 0.1.0 was tried
and reverted in phase 2: a `lockstep:` upstream ships *inside* the compiler, so pinning the
compiler also pins the inherited pipeline content — this tree's library had moved past the
release, and the committed output immediately stopped matching what the spec compiles to. That is
exactly the property `capabilities.compiler: "."` exists to protect. The decoupling that mattered
is at the CI target instead: `ci-framework` never invokes `fetch`, so the new package is never
gated on the old one being installable. (Note also that the compiler cannot be pinned as an
ordinary dependency at all: `uv.lock` resolves `in-lockstep` to `source = { editable = "." }`.)

## Consequences

- The framework's own AI-assisted development loop runs on frozen `aw-*.lock.yml` until Phase 7.
- First value lands at Phase 2 of 7: `/review` in-process against a real PR of this repository,
  with `tokens > 0` and `cost_usd > 0` in the ledger — which is `docs/needs.md` N2, the project's
  own stated gate and its cheapest possible proof.
- Abandon criteria are written into the plan (time, architecture, cost, value) with a named cut
  line: Phases 0–4 plus docs ship as `0.9`/`1.0.0rc`, not `1.0`, because `GATE-CI-1` is what makes
  reusing the name safe.

## A note on the citations above

Two rows quote documents this pivot deleted. They are quoted because they are the strongest
evidence for the decisions: the project had already written down, in its own words, both the
fork-provenance property that made compiled review safe and the fact that sealing was never an
access control against a repository's own owners. Those files are in git history at
`compiler-v0.1.x`; the quotes are preserved here because the argument outlives the file.

## Provenance

This decision was reviewed by seven SDLC personas (Round 5, all CHANGES REQUIRED), arbitrated
across fifteen contested questions by three cross-persona panels, re-reviewed (Round 6, all CHANGES
REQUIRED), and closed out (Round 7, sign-off with notes). The design delta is v0.5.
