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
| `GATE-ASYNC-4` | P2 | held | Three concurrent `generate()` calls complete in under 2x the time one takes, against the **real** provider code rather than a stand-in: `ClaudeTransport.generate` with its SDK client replaced (the shared path for Anthropic, Bedrock and Vertex, with a companion test failing if any of the three grows its own `generate`), and `OllamaProvider` end to end through its own `httpx` path. Paired with a negative control — the same transport with a synchronous wait inside `async def`, which must fail the assertion, or the gate proves only that asyncio exists. |

## Cost

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-COST-1` | P1 | held | No module-level mutable accumulator exists under `in_lockstep/`; two concurrent `RunContext`s accumulate `Spend` independently. |
| `GATE-COST-2` | P2 | held | Stub charges proportional to **cumulative** input tokens, never a flat per-turn fee (flat is the one curve under which quadratic growth is invisible): with `budget=$0.10` the run is `BLOCKED` before the turn whose *estimate* would cross, and a `len/4` estimator fails this gate. |
| `GATE-COST-3` | P2 | held | A model absent from `CostTable` yields `Outcome(BLOCKED)` with finding id `cost.unpriced_model` and **zero** `generate()` calls. |
| `GATE-COST-4` | P0 | held | The identifier `DEFAULT_COST_PER_M` appears nowhere under `in_lockstep/`. |
| `GATE-COST-5` | P3 | held | `in-lockstep doctor` exits non-zero when no provider org spend limit is attested in config. |
| `GATE-COST-6` | P10 | deferred | A 4-branch `fan_out` under a joint `$1.00` `Spend` charges ≤ `$1.00` in aggregate, not per branch. |
| `GATE-BUDGET-1` | P1 | held | A run with no declared budget is refused at startup — `Lockstep.context()` raises `UndeclaredBudget`, surfaced by the CLI as a message rather than a traceback. Scoped to lifecycles that can spend: the trigger is a bound adapter declaring `Capability.SPENDS_BUDGET`, so a repository binding only `Test` and `Validate` needs no ceiling. Any of the four `Budget` dimensions satisfies it, as does a `CostBudget` in the middleware chain, since the scaffold declares it that way. Two defaults were removed to make it satisfiable at all: `--budget` no longer has one, and `_default_lockstep` no longer injects `CostBudget(usd=2.00)` — a ceiling the CLI invents is a budget nobody chose, and every run would have arrived bounded by it. |
| `GATE-DEADLINE-1` | P2 | held | An `InvokePolicy` deadline expiring mid-loop yields `BLOCKED(reason="deadline")` with no further `generate()` calls; `KillSwitch` set mid-loop does the same. |

## Retry

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-RETRY-1` | P2 | held | A transport stub returning 500 forever is called exactly 3 times for one logical model call. |
| `GATE-RETRY-2` | P0 | held | `with_retry` is imported nowhere under `in_lockstep/`; every SDK client is constructed with `max_retries=0`. |
| `GATE-RETRY-3` | P0 | held | A 400 error whose message contains `"generated"` maps to a non-retryable class and is attempted exactly once. (Today `"rate" in msg.lower()` matches "gene**rate**d".) |
| `GATE-RETRY-4` | P2 | held | `Retry-After: 30` is slept; `Retry-After: 3600` with 60s remaining wall clock returns `ERRORED` without sleeping — asserted **through `AiInvoker`**, which supplies the remaining budget, not against a `RetryPolicy` handed one by the test. The original form passed for the whole pivot while nothing on any live path set the field, so the gate proved the policy honours a budget it was never given. Successive sleeps are bounded in aggregate, not individually. |
| `GATE-RETRY-5` | P1 | held | `Retry` middleware invokes an action declaring `Capability.SPENDS_BUDGET` exactly once and emits a finding explaining the refusal. |
| `GATE-RETRY-6` | P2 | held | A provider error carrying an API-key-shaped string produces an `Outcome` and ledger record matching no `Redact` secret pattern. Asserted for a seeded key and for an unseeded one caught structurally, in the `Outcome`, in the `Finding` it carries, in a rendered traceback (the chained cause is suppressed, or the original prints unredacted on a crash), and in the ledger file. Implementing it also closed a gap it depended on: a provider error escaped `AiInvoker.run` raw, so every adapter — all of which catch only `InvocationBlocked` — turned a 401 into a traceback rather than an `Outcome`. It is now `InvocationFailed`, mapping to `ERRORED` rather than `BLOCKED`, because §4.3 reserves `BLOCKED` for a policy refusal. |

## Test net

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-TEST-1` | P0 | held | `tests/characterization/` holds the composed prompts + sha256; re-running the current compiler reproduces them. |
| `GATE-TEST-2` | P5 | held | Each `in_lockstep`-composed prompt's ordered section-id list equals the corpus's; any byte delta has a committed `.diff` of identical hash. |
| `GATE-TEST-3` | P0 | held | `make cov-new` runs in CI; it fails below the committed floor, and above `floor+2` it fails **as a required floor-file update** ("coverage rose to X; bump `.coverage-floor`"), not as a bare failure. `cov-new` must not inherit `[tool.coverage.report] omit`. |
| `GATE-TEST-4` | P0-7 | retired | ~~The golden tree's git hash equals the Phase-0 tag value.~~ **Retired at phase 7 with its subject.** It replaced the drift gate and protected generated output for the duration of the pivot; the compiler that generated that output is gone, so the gate has nothing to hold. `tests/characterization/` is what survives, and it protects the thing that outlived the emitter: the composition order. |
| `GATE-TEST-5` | P1-7 | retired | ~~`pytest --collect-only -q packages/pipeline-exec/tests` count ≥ the Phase-0 baseline.~~ **Retired with its subject.** `packages/pipeline-exec` was deleted after 1.0; the 478 tests this gate counted went with the code they covered. It existed so the pivot could not quietly erode the one test net that predated it, and it held for seven phases — the package was then removed deliberately, by decision, which is the outcome the gate was protecting the right to make. |
| `GATE-TEST-6` | P1-7 | retired | ~~`pyproject` `testpaths` still contains both `tests` and `packages/pipeline-exec/tests`.~~ **Retired with its subject**, for the same reason — it was `GATE-TEST-5`'s companion, guarding against the collection count being held up by a path that no longer ran. |
| `GATE-TESTGUARD-1` | P6 | held | A `ChangeSet` deleting a test, or adding `skip`/`xfail` to one, without a ticket is refused. (R1-QA-1, second half.) A rule about the *shape* of a change rather than its path, because tests must stay writable and no tier lists them — composed into `ChangeGuard.check()` rather than added as a second call each enforcement point would have to remember. Test files are recognised across Python, Go, TypeScript, Java, Ruby and Rust, and silencers likewise. `apply` passes a reader for the working tree so an *added* skip is told from one already there; with no reader the rule fails closed, because a false positive costs a ticket trailer and a false negative lets `fix` make CI green by silencing what failed. |

## Ledger, evals, metrics

| Gate | Phase | Status | Assertion |
|---|---|---|---|
| `GATE-LEDGER-1` | P4 | held | Every record written by `in_lockstep` carries `schema` and `epoch`; a record lacking them reads back as epoch `"ghaw"`. |
| `GATE-LEDGER-2` | P4 | held | `compare()` over records spanning two epochs raises `LedgerError`. |
| `GATE-LEDGER-3` | P4 | held | A window whose records omit `credits` yields `mean_credits is None` and no credits delta. (Regression test for the fabricated −100%: `history.py` does `float(record.get("credits") or 0)`, coercing absent to `0.0`.) |
| `GATE-LEDGER-4` | P6 | held | Changing one guardrail file changes `EvalSubject` identity while `prompt_id@version` is unchanged. |
| `GATE-LEDGER-5` | P4 | held | Every emitted metric name starts with `in_lockstep.` or `gen_ai.`, and none starts with the compiler's retired `lockstep.`. Checked from the **AST of every metric call site** as well as from a run, because a runtime check only sees metrics some test happened to trigger, and a metric added behind an unexercised branch is exactly the one that would carry a stale prefix. Note the wording is about a *prefix*: `in_lockstep.` contains `lockstep.`, so a substring check would reject every legitimate metric in the package — there is a test asserting that trap so nobody later "tightens" the check into it. |
| `GATE-LEDGER-6` | P4 | held | The keys `credits`, `busy_seconds`, `pickup_seconds` appear in no emitted metric — name or **dimension** — and no current-epoch record. Both halves are asserted against real output rather than source text, and both were checked to fail when a key is reintroduced. Implementing it also collapsed two ledger writers into one: `cli._write_ledger` hand-rolled the record and stamped `schema` and `epoch` as literals beside the store that owns those constants, and a gate about what a record contains is worth only as much as the number of things that write one. |
| `GATE-LEDGER-7` | P4 | held | `priced_fraction` is `None` when no token was billable, and the metric and ledger field are **omitted** rather than defaulted — `1.0` for a run that spent nothing is coverage computed from an empty denominator, the same shape as a suite reporting 100% having judged nothing. The gate had no subject until the metric existed, and giving it one exposed what it measures: `price()` substituted the input rate for cache tokens whenever a rate declared none, silently, which is the fabrication this module's own docstring condemns one level up. The substitution stays — it over-estimates, and a ceiling that under-estimates is not a ceiling — but the total is now labelled an upper bound by a number that says how much of it came from a declared rate. |
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
| `GATE-APPROVAL-1` | P3 | held | An adapter that lets a **model** write or execute, with no approval path in the middleware chain, is refused at startup rather than at call time. **Narrowed from the literal wording, deliberately:** read as "any adapter granting `WRITES_FILES`/`EXECUTES_CODE`", it refuses every repository that runs its own test suite, since `PytestTest` declares `EXECUTES_CODE` and means it. `Sandbox` is the control for a deterministic adapter that executes code; approval is the control for one that *also* spends money, because there a model is choosing. Approval is a **declared** property (`provides_approval`), not a class identity, so a house gate routing approvals through an organisation's own system of record satisfies it. Fires for nothing shipped — `AiReview` is read-only — which is the right answer for a framework with no write verb. |
| `GATE-POLICY-1` | P1 | held | `PolicyStack` reproduces `PromptLayers.enforce()`: `deny-all` is an irreversible floor, ceilings take the lowest not the last, scan is strictest-wins, deny-tools union — **and the resolved result now reaches the loop.** `InvokePolicy.under()` composes an adapter's own needs with the stack: `max_turns` tightens and never raises, `deny_tools` is removed from the `ToolSet` so a denied tool cannot be dispatched rather than being refused when called, and `scan_input="block"` refuses before the first model call where `warn` records and proceeds. Until this, `resolve()` had exactly one caller — `ls`, printing a summary — so a contributed ceiling was a comment. |
| `GATE-CI-1` | P7 | held | No workflow under `.github/workflows/` invokes `lockstep`; no `aw-*.lock.yml` remains. |
