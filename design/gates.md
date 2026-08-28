# in-lockstep exit gates

Every gate referenced by id in the pivot plan, defined here. A gate named in a phase table and
defined nowhere is indistinguishable from a gate that does not exist — that was the weak point of
two review rounds (R6-QA-5), so this file is a Phase-0 deliverable and the phase tables reference
it rather than restating it.

`(Pn)` is the phase whose exit the gate blocks. `(Pn-m)` means it holds continuously across a range.

## Async and concurrency

| Gate | Phase | Assertion |
|---|---|---|
| `GATE-ASYNC-1` | P0 | AST scan of `in_lockstep/ai/llm/providers/*.py`: every client constructor names a class matching `^Async`, or is `httpx.AsyncClient`. |
| `GATE-ASYNC-2` | P0 | `asyncio.wait_for(provider.generate(...), 0.1)` against a 5s stub raises `TimeoutError` AND the stub records connection-closed-before-completion. |
| `GATE-ASYNC-3` | P1 | With `IN_LOCKSTEP_DISABLE` set mid-run, a workflow with steps remaining reaches a terminal `Outcome` without executing another adapter. (Arbitration wrote this against `fan_out`, which is post-1.0 by the §17.11 cut line — the branch variant is `GATE-ASYNC-3b`.) |
| `GATE-ASYNC-3b` | P10 | With `IN_LOCKSTEP_DISABLE` set mid-run, an in-flight 3-branch `fan_out` reaches a terminal `Outcome` within 2s. |
| `GATE-ASYNC-4` | P2 | Three concurrent `generate()` calls against a 1s stub complete in < 2s wall clock (the event loop is not blocked). |

## Cost

| Gate | Phase | Assertion |
|---|---|---|
| `GATE-COST-1` | P1 | No module-level mutable accumulator exists under `in_lockstep/`; two concurrent `RunContext`s accumulate `Spend` independently. |
| `GATE-COST-2` | P2 | Stub charges proportional to **cumulative** input tokens, never a flat per-turn fee (flat is the one curve under which quadratic growth is invisible): with `budget=$0.10` the run is `BLOCKED` before the turn whose *estimate* would cross, and a `len/4` estimator fails this gate. |
| `GATE-COST-3` | P2 | A model absent from `CostTable` yields `Outcome(BLOCKED)` with finding id `cost.unpriced_model` and **zero** `generate()` calls. |
| `GATE-COST-4` | P0 | The identifier `DEFAULT_COST_PER_M` appears nowhere under `in_lockstep/`. |
| `GATE-COST-5` | P3 | `in-lockstep doctor` exits non-zero when no provider org spend limit is attested in config. |
| `GATE-COST-6` | P10 | A 4-branch `fan_out` under a joint `$1.00` `Spend` charges ≤ `$1.00` in aggregate, not per branch. |
| `GATE-BUDGET-1` | P1 | A run with no declared budget is refused at startup. (`checks.py` `DOC006` is `Severity.ERROR` today; porting it to an advisory `doctor` check would downgrade a refusal to a suggestion.) |
| `GATE-DEADLINE-1` | P2 | An `InvokePolicy` deadline expiring mid-loop yields `BLOCKED(reason="deadline")` with no further `generate()` calls; `KillSwitch` set mid-loop does the same. |

## Retry

| Gate | Phase | Assertion |
|---|---|---|
| `GATE-RETRY-1` | P2 | A transport stub returning 500 forever is called exactly 3 times for one logical model call. |
| `GATE-RETRY-2` | P0 | `with_retry` is imported nowhere under `in_lockstep/`; every SDK client is constructed with `max_retries=0`. |
| `GATE-RETRY-3` | P0 | A 400 error whose message contains `"generated"` maps to a non-retryable class and is attempted exactly once. (Today `"rate" in msg.lower()` matches "gene**rate**d".) |
| `GATE-RETRY-4` | P2 | `Retry-After: 30` is slept; `Retry-After: 3600` with 60s remaining wall clock returns `ERRORED` without sleeping. |
| `GATE-RETRY-5` | P1 | `Retry` middleware invokes an action declaring `Capability.SPENDS_BUDGET` exactly once and emits a finding explaining the refusal. |
| `GATE-RETRY-6` | P2 | A provider error carrying an API-key-shaped string produces an `Outcome` and ledger record matching no `Redact` secret pattern. |

## Test net

| Gate | Phase | Assertion |
|---|---|---|
| `GATE-TEST-1` | P0 | `tests/characterization/` holds the composed prompts + sha256; re-running the current compiler reproduces them. |
| `GATE-TEST-2` | P5 | Each `in_lockstep`-composed prompt's ordered section-id list equals the corpus's; any byte delta has a committed `.diff` of identical hash. |
| `GATE-TEST-3` | P0 | `make cov-new` runs in CI; it fails below the committed floor, and above `floor+2` it fails **as a required floor-file update** ("coverage rose to X; bump `.coverage-floor`"), not as a bare failure. `cov-new` must not inherit `[tool.coverage.report] omit`. |
| `GATE-TEST-4` | P0-7 | The golden tree's git hash equals the Phase-0 tag value, and `LOCKSTEP_REGEN` is unset in CI. (This is the stated replacement for the retired drift gate.) |
| `GATE-TEST-5` | P1-7 | `pytest --collect-only -q packages/pipeline-exec/tests` count ≥ the Phase-0 baseline. |
| `GATE-TEST-6` | P1-7 | `pyproject` `testpaths` still contains both `tests` and `packages/pipeline-exec/tests`. |
| `GATE-TESTGUARD-1` | P6 | A `ChangeSet` deleting a test, or adding `skip`/`xfail` to one, without a `Ticket:` trailer is `BLOCKED`. (R1-QA-1, second half.) |

## Ledger, evals, metrics

| Gate | Phase | Assertion |
|---|---|---|
| `GATE-LEDGER-1` | P4 | Every record written by `in_lockstep` carries `schema` and `epoch`; a record lacking them reads back as epoch `"ghaw"`. |
| `GATE-LEDGER-2` | P4 | `compare()` over records spanning two epochs raises `LedgerError`. |
| `GATE-LEDGER-3` | P4 | A window whose records omit `credits` yields `mean_credits is None` and no credits delta. (Regression test for the fabricated −100%: `history.py` does `float(record.get("credits") or 0)`, coercing absent to `0.0`.) |
| `GATE-LEDGER-4` | P6 | Changing one guardrail file changes `EvalSubject` identity while `prompt_id@version` is unchanged. |
| `GATE-LEDGER-5` | P4 | Every emitted metric name starts with `in_lockstep.` or `gen_ai.`; the prefix `lockstep.` appears in no emitted metric. |
| `GATE-LEDGER-6` | P4 | The keys `credits`, `busy_seconds`, `pickup_seconds` appear in no emitted metric and no current-epoch record. |
| `GATE-LEDGER-7` | P4 | `priced_fraction` is never `1.0` when the token denominator is zero; it is omitted. |
| `GATE-EVAL-1` | P6 | Editing a **skill body** changes `EvalSubject` identity. (Guards the progressive-disclosure hole: §5.8 loads skill bodies on demand, so they are not in `composed_prompt_sha256`.) |

## Outcome and ledger store

| Gate | Phase | Assertion |
|---|---|---|
| `GATE-OUT-1` | P1 | `Status` has exactly 6 members and `UNDECIDED` is not among them. |
| `GATE-OUT-2` | P6 | An unjudged rubric yields `Outcome(SUCCEEDED, decided=False)` with `pass_rate None`; a cache hit yields `Outcome(SKIPPED, decided=True)`; **both the ledger and the `in_lockstep.action.outcome` metric** distinguish them; and `Cache` refuses to store `decided=False`. |
| `GATE-OUT-3` | P10 | `JoinResult.decided == all(branch.decided)`. |
| `GATE-OUT-4` | P4 | `LedgerStore` declares `compare_and_set`; the shipped in-repo store implements it and reports `scope == LOCAL`. |
| `GATE-OUT-5` | P10 | `ctx.fan_out` with a `ctx.human()` branch on a `LOCAL`-scope store raises at the call site, before any branch starts. |
| `GATE-OUT-6` | P10 | `ctx.park` on a `LOCAL` store returns `BLOCKED` naming the store; on `GitLedger`, 8 concurrent barrier ticks launch the continuation exactly once. |
| `GATE-OUT-7` | P10 | An all-machine `fan_out` on the default store performs zero ledger writes. |

## Security

The first revision of the plan had **none** of these. The plan's own thesis — "a layering rule with
no enforcement will not survive seven phases" — applies with equal force to security rules.

| Gate | Phase | Assertion |
|---|---|---|
| `GATE-CFG-1` | P2 | A `lockstep.py` modified in the head tree has **zero** effect on the resolved container; config resolves from the trusted ref. Run as a fork-simulation fixture. |
| `GATE-CFG-2` | P3 | `doctor` fails when config would resolve from the ref under review. |
| `GATE-EGRESS-1` | P3 | A `ContextPackage` containing any `UNTRUSTED_EXTERNAL` item with `EgressMode.NONE` yields `BLOCKED` before the first model call. |
| `GATE-EGRESS-2` | P3 | `ENFORCED_*` performs a live probe to a known-blocked host and refuses to start if the probe succeeds (verified, not attested). |
| `GATE-EGRESS-3` | P3 | An MCP tool with undeclared capability is treated as `REACHES_NETWORK`, not read-only. |
| `GATE-GUARD-1` | P2 | A synthetic MCP write to each Tier-1 path is refused on all three paths: the in-loop tool boundary, `--apply-inline`, and `apply --from-artifact`. |
| `GATE-GUARD-2` | P3 | A `ChangeSet` whose `FileChange` resolves outside the repo root, or introduces a symlink that would, is refused — evaluated on the **post-change tree**. |
| `GATE-GUARD-3` | P6 | A strategy selected from ticket-label input cannot hold the `prompts/**` grant. |
| `GATE-REDACT-1` | P2 | Enumerate every module writing outside the process; fail on any unwrapped writer. (Default-deny, not an enumerated sink list.) |
| `GATE-REDACT-2` | P2 | A secret framed as base64 and as `Authorization: Bearer <v>` is redacted in stdout, span attributes, cassette, artifact, and ledger — not just the ledger. |
| `GATE-AUTH-1` | P2 | Every provider **and every MCP server** constructs with `os.environ` monkeypatched empty. |
| `GATE-AUTH-2` | P2 | A constructed client's base URL equals its registered `endpoint`, else refuse. |
| `GATE-SANDBOX-1` | P3 | `Test`/`Validate` run out-of-process; a `conftest.py` attempting to read a `Credentials` object from the parent process cannot reach one. |
| `GATE-APPROVAL-1` | P3 | A `ToolSet` granting `WRITES_FILES`/`EXECUTES_CODE` without `ApprovalGate` bound is refused at container resolution, not at call time. |
| `GATE-POLICY-1` | P1 | `PolicyStack` reproduces `PromptLayers.enforce()`: `deny-all` is an irreversible floor, ceilings take the lowest not the last, scan is strictest-wins, deny-tools union. **Testable against the current compiler today** — the corpus in `tests/characterization/` records the expected merge per agent. |
| `GATE-CI-1` | P7 | No workflow under `.github/workflows/` invokes `lockstep`; no `aw-*.lock.yml` remains. |
