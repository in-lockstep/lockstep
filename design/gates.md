# in-lockstep exit gates

Every gate referenced by id in the pivot plan, defined here. A gate named in a phase table and
defined nowhere is indistinguishable from a gate that does not exist — that was the weak point of
two review rounds (R6-QA-5), so this file is a Phase-0 deliverable and the phase tables reference
it rather than restating it.

`(Pn)` is the phase whose exit the gate blocks. `(Pn-m)` means it holds continuously across a range.

## Status, and why this column exists

The preamble above says a gate defined nowhere is indistinguishable from one that does not exist.
The same argument runs one step further, and this file did not make it: **a gate that is defined,
and whose mechanism is unit-tested, and which nothing invokes during a run, is also
indistinguishable from one that does not exist.** Nine rows below were in that state while
`docs/controls-crosswalk.md` cited them as evidence that a substrate control had been replaced.

| Status | Meaning |
|---|---|
| `held` | The mechanism runs on a live path and an assertion covers it. |
| `unit only` | The mechanism exists and is tested in isolation — but nothing calls it during a run, so the assertion, which is about a run, is not established. **Not a pass.** |
| `partial` | Some clause of the assertion is covered and some is not; the row says which. |
| `unmet` | No mechanism, or a mechanism with nothing asserting over it. |
| `deferred` | Past the 1.0 cut line, by the deferral recorded in the plan and ADR 0001 §17.11. |
| `retired` | The subject was deleted; the row records what it held and why it stopped. |

`unit only` is the status worth understanding, and `GATE-EGRESS-1` was the example. It had a
thorough test that a `ContextPackage` carrying untrusted content under `EgressMode.NONE` raises
`EgressRefused` — a real test of a real mechanism. But `EgressPolicy.check()` had no caller
outside that test, so no run had ever been refused and the gate's actual assertion ("BLOCKED
before the first model call") had never been exercised. It is `held` now, because `AiInvoker.run`
calls it. The distinction this project already draws between `decided` and `succeeded` is the
same one: a mechanism nobody invoked is outstanding, not passed.

`GATE-APPROVAL-1` and `GATE-POLICY-1` are still in that state, and their rows say what is missing.

## Async and concurrency

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-ASYNC-1` | P0 | held | AST scan of `in_lockstep/llm/providers/*.py`: every client constructor names a class matching `^Async`, or is `httpx.AsyncClient`. |
| `GATE-ASYNC-2` | P0 | held | `asyncio.wait_for(provider.generate(...), 0.1)` against a 5s stub raises `TimeoutError` AND the stub records connection-closed-before-completion. |
| `GATE-ASYNC-3` | P1 | held | With `IN_LOCKSTEP_DISABLE` set mid-run, a workflow with steps remaining reaches a terminal `Outcome` without executing another adapter. (Arbitration wrote this against `fan_out`, which is post-1.0 by the §17.11 cut line — the branch variant is `GATE-ASYNC-3b`.) |
| `GATE-ASYNC-3b` | P10 | deferred | With `IN_LOCKSTEP_DISABLE` set mid-run, an in-flight 3-branch `fan_out` reaches a terminal `Outcome` within 2s. |
| `GATE-ASYNC-4` | P2 | unmet | Three concurrent `generate()` calls against a 1s stub complete in < 2s wall clock (the event loop is not blocked). **Missing:** no concurrency test exists. |

## Cost

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-COST-1` | P1 | held | No module-level mutable accumulator exists under `in_lockstep/`; two concurrent `RunContext`s accumulate `Spend` independently. |
| `GATE-COST-2` | P2 | held | Stub charges proportional to **cumulative** input tokens, never a flat per-turn fee (flat is the one curve under which quadratic growth is invisible): with `budget=$0.10` the run is `BLOCKED` before the turn whose *estimate* would cross, and a `len/4` estimator fails this gate. |
| `GATE-COST-3` | P2 | held | A model absent from `CostTable` yields `Outcome(BLOCKED)` with finding id `cost.unpriced_model` and **zero** `generate()` calls. |
| `GATE-COST-4` | P0 | held | The identifier `DEFAULT_COST_PER_M` appears nowhere under `in_lockstep/`. |
| `GATE-COST-5` | P3 | held | `in-lockstep doctor` exits non-zero when no provider org spend limit is attested in config. |
| `GATE-COST-6` | P10 | deferred | A 4-branch `fan_out` under a joint `$1.00` `Spend` charges ≤ `$1.00` in aggregate, not per branch. |
| `GATE-BUDGET-1` | P1 | unmet | A run with no declared budget is refused at startup. (`checks.py` `DOC006` is `Severity.ERROR` today; porting it to an advisory `doctor` check would downgrade a refusal to a suggestion.) **Missing:** the refusal itself. `Lockstep.__init__` defaults to an all-`None` `Budget` and nothing refuses at startup. |
| `GATE-DEADLINE-1` | P2 | held | An `InvokePolicy` deadline expiring mid-loop yields `BLOCKED(reason="deadline")` with no further `generate()` calls; `KillSwitch` set mid-loop does the same. |

## Retry

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-RETRY-1` | P2 | held | A transport stub returning 500 forever is called exactly 3 times for one logical model call. |
| `GATE-RETRY-2` | P0 | held | `with_retry` is imported nowhere under `in_lockstep/`; every SDK client is constructed with `max_retries=0`. |
| `GATE-RETRY-3` | P0 | held | A 400 error whose message contains `"generated"` maps to a non-retryable class and is attempted exactly once. (Today `"rate" in msg.lower()` matches "gene**rate**d".) |
| `GATE-RETRY-4` | P2 | held | `Retry-After: 30` is slept; `Retry-After: 3600` with 60s remaining wall clock returns `ERRORED` without sleeping — asserted **through `AiInvoker`**, which supplies the remaining budget, not against a `RetryPolicy` handed one by the test. The original form passed for the whole pivot while nothing on any live path set the field, so the gate proved the policy honours a budget it was never given. Successive sleeps are bounded in aggregate, not individually. |
| `GATE-RETRY-5` | P1 | held | `Retry` middleware invokes an action declaring `Capability.SPENDS_BUDGET` exactly once and emits a finding explaining the refusal. |
| `GATE-RETRY-6` | P2 | unmet | A provider error carrying an API-key-shaped string produces an `Outcome` and ledger record matching no `Redact` secret pattern. **Missing:** `GATE-REDACT-2` covers `Redact` directly, but nothing asserts a provider error reaches the ledger redacted. |

## Test net

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-TEST-1` | P0 | held | `tests/characterization/` holds the composed prompts + sha256; re-running the current compiler reproduces them. |
| `GATE-TEST-2` | P5 | held | Each `in_lockstep`-composed prompt's ordered section-id list equals the corpus's; any byte delta has a committed `.diff` of identical hash. |
| `GATE-TEST-3` | P0 | held | `make cov-new` runs in CI; it fails below the committed floor, and above `floor+2` it fails **as a required floor-file update** ("coverage rose to X; bump `.coverage-floor`"), not as a bare failure. `cov-new` must not inherit `[tool.coverage.report] omit`. |
| `GATE-TEST-4` | P0-7 | retired | ~~The golden tree's git hash equals the Phase-0 tag value.~~ **Retired at phase 7 with its subject.** It replaced the drift gate and protected generated output for the duration of the pivot; the compiler that generated that output is gone, so the gate has nothing to hold. `tests/characterization/` is what survives, and it protects the thing that outlived the emitter: the composition order. |
| `GATE-TEST-5` | P1-7 | retired | ~~`pytest --collect-only -q packages/pipeline-exec/tests` count ≥ the Phase-0 baseline.~~ **Retired with its subject.** `packages/pipeline-exec` was deleted after 1.0; the 478 tests this gate counted went with the code they covered. It existed so the pivot could not quietly erode the one test net that predated it, and it held for seven phases — the package was then removed deliberately, by decision, which is the outcome the gate was protecting the right to make. |
| `GATE-TEST-6` | P1-7 | retired | ~~`pyproject` `testpaths` still contains both `tests` and `packages/pipeline-exec/tests`.~~ **Retired with its subject**, for the same reason — it was `GATE-TEST-5`'s companion, guarding against the collection count being held up by a path that no longer ran. |
| `GATE-TESTGUARD-1` | P6 | unmet | A `ChangeSet` deleting a test, or adding `skip`/`xfail` to one, without a `Ticket:` trailer is `BLOCKED`. (R1-QA-1, second half.) **Missing:** the diff-shape rule in `Scm.open_change`. R1-QA-1's second half was never implemented. |

## Ledger, evals, metrics

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-LEDGER-1` | P4 | held | Every record written by `in_lockstep` carries `schema` and `epoch`; a record lacking them reads back as epoch `"ghaw"`. |
| `GATE-LEDGER-2` | P4 | held | `compare()` over records spanning two epochs raises `LedgerError`. |
| `GATE-LEDGER-3` | P4 | held | A window whose records omit `credits` yields `mean_credits is None` and no credits delta. (Regression test for the fabricated −100%: `history.py` does `float(record.get("credits") or 0)`, coercing absent to `0.0`.) |
| `GATE-LEDGER-4` | P6 | held | Changing one guardrail file changes `EvalSubject` identity while `prompt_id@version` is unchanged. |
| `GATE-LEDGER-5` | P4 | partial | Every emitted metric name starts with `in_lockstep.` or `gen_ai.`; the prefix `lockstep.` appears in no emitted metric. The positive half is asserted. **Missing:** the negative clause — that no emitted metric starts with `lockstep.`. |
| `GATE-LEDGER-6` | P4 | unmet | The keys `credits`, `busy_seconds`, `pickup_seconds` appear in no emitted metric and no current-epoch record. **Missing:** no assertion; the compiler-era keys are absent by construction rather than by check. |
| `GATE-LEDGER-7` | P4 | unmet | `priced_fraction` is never `1.0` when the token denominator is zero; it is omitted. **Missing:** `priced_fraction` is emitted nowhere, so the gate has no subject yet. |
| `GATE-EVAL-1` | P6 | held | Editing a **skill body** changes `EvalSubject` identity. (Guards the progressive-disclosure hole: §5.8 loads skill bodies on demand, so they are not in `composed_prompt_sha256`.) |

## Outcome and ledger store

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-OUT-1` | P1 | held | `Status` has exactly 6 members and `UNDECIDED` is not among them. |
| `GATE-OUT-2` | P6 | held | An unjudged rubric yields `Outcome(SUCCEEDED, decided=False)` with `pass_rate None`; a cache hit yields `Outcome(SKIPPED, decided=True)`; **both the ledger and the `in_lockstep.action.outcome` metric** distinguish them; and `Cache` refuses to store `decided=False`. |
| `GATE-OUT-3` | P10 | deferred | `JoinResult.decided == all(branch.decided)`. |
| `GATE-OUT-4` | P4 | held | `LedgerStore` declares `compare_and_set`; the shipped in-repo store implements it and reports `scope == LOCAL`. |
| `GATE-OUT-5` | P10 | deferred | `ctx.fan_out` with a `ctx.human()` branch on a `LOCAL`-scope store raises at the call site, before any branch starts. |
| `GATE-OUT-6` | P10 | deferred | `ctx.park` on a `LOCAL` store returns `BLOCKED` naming the store; on `GitLedger`, 8 concurrent barrier ticks launch the continuation exactly once. |
| `GATE-OUT-7` | P10 | deferred | An all-machine `fan_out` on the default store performs zero ledger writes. |

## Security

The first revision of the plan had **none** of these. The plan's own thesis — "a layering rule with
no enforcement will not survive seven phases" — applies with equal force to security rules.

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-CFG-1` | P2 | held | A `lockstep.py` modified in the head tree has **zero** effect on the resolved container; config resolves from the trusted ref. Run as a fork-simulation fixture. |
| `GATE-CFG-2` | P3 | held | `doctor` fails when config would resolve from the ref under review. |
| `GATE-EGRESS-1` | P3 | held | A `ContextPackage` containing any `UNTRUSTED_EXTERNAL` item with `EgressMode.NONE` yields `BLOCKED` before the first model call. |
| `GATE-EGRESS-2` | P3 | held | `ENFORCED_*` performs a live probe to a known-blocked host and refuses to start if the probe succeeds (verified, not attested). |
| `GATE-EGRESS-3` | P3 | held | An MCP tool with undeclared capability is treated as `REACHES_NETWORK`, not read-only. |
| `GATE-GUARD-1` | P2 | partial | A synthetic MCP write to each Tier-1 path is refused on all three paths: the in-loop tool boundary, `--apply-inline`, and `apply --from-artifact`. **One of three holds.** `apply --from-artifact` is covered end to end, over ten Tier-1 paths plus an escape above the repository root — and it is the path that matters most, because the artifact crossed a trust boundary and a previous job having produced it is not a reason to trust it. **Missing:** the in-loop tool boundary (no tool runner ships, so there is no boundary to sit at) and `--apply-inline` (no such flag exists). |
| `GATE-GUARD-2` | P3 | held | A `ChangeSet` whose `FileChange` resolves outside the repo root, or introduces a symlink that would, is refused — evaluated on the **post-change tree**. |
| `GATE-GUARD-3` | P6 | held | A strategy selected from ticket-label input cannot hold the `prompts/**` grant. |
| `GATE-REDACT-1` | P2 | held | Every module is AST-scanned for a raw write primitive (`write`, `write_text`, `write_bytes`, `writelines`, `open`/`fdopen` in a write mode); each use must be inside `privileged/sink.py` or carry a written reason it is not a redaction sink. Stdout and stderr are covered by wrapping the *stream* at CLI entry rather than the sixty-odd `echo` calls, so a call added later is covered without anyone remembering. A companion test fails on an exemption whose call site is gone, so a licence cannot outlive its subject. |
| `GATE-REDACT-2` | P2 | held | A secret framed as base64 and as `Authorization: Bearer <v>` is redacted in stdout, span attributes, cassette, artifact, and ledger — not just the ledger. |
| `GATE-AUTH-1` | P2 | held | Every provider **and every MCP server** constructs with `os.environ` monkeypatched empty. |
| `GATE-AUTH-2` | P2 | held | A constructed client's base URL equals its registered `endpoint`, else refuse. |
| `GATE-SANDBOX-1` | P3 | held | `Test`/`Validate` run out-of-process; a `conftest.py` attempting to read a `Credentials` object from the parent process cannot reach one. |
| `GATE-APPROVAL-1` | P3 | unit only | A `ToolSet` granting `WRITES_FILES`/`EXECUTES_CODE` without `ApprovalGate` bound is refused at container resolution, not at call time. **Missing:** `assert_gated` is never called at container resolution. |
| `GATE-POLICY-1` | P1 | unit only | `PolicyStack` reproduces `PromptLayers.enforce()`: `deny-all` is an irreversible floor, ceilings take the lowest not the last, scan is strictest-wins, deny-tools union. **Testable against the current compiler today** — the corpus in `tests/characterization/` records the expected merge per agent. The merge semantics are held and tested. **Missing:** `PolicyStack.resolve()` is consumed only by `ls`, so no resolved ceiling reaches `InvokePolicy` or `ToolSet`. |
| `GATE-CI-1` | P7 | held | No workflow under `.github/workflows/` invokes `lockstep`; no `aw-*.lock.yml` remains. |
