# Contributing

## The mechanics

```bash
make check     # ruff format + lint, mypy --strict, pytest — what CI runs
make cov       # the two-sided coverage ratchet
```

`make cov` fails below the committed floor **and** more than two points above it — a rise is a
required one-line update to `.coverage-floor`, so the number in the repository is always the
number that is true. `design/gates.md` works the same way: a gate marked `held` must be
discharged by a test that names it, and a gate marked `unmet` must not be, so implementing one
without updating the ledger fails, in both directions. The README's what-ships-today matrix is
checked the same way.

## The three disciplines a change is judged by

Most review feedback here is one of these, so it is cheaper to read them first:

1. **Honest numbers.** Absent is not zero: `None` means unmeasured, `0` means measured as none,
   and collapsing them is how a fabricated improvement gets reported as fact. No default rates,
   no coerced missing fields, no "100%" computed over an empty denominator.
2. **Tighten-only composition.** Budgets merge to the lowest, policy layers append with no
   removal API, scan strength is strictest-wins. A contribution that could loosen another is a
   bug even when nothing exploits it yet.
3. **No unwired claims.** A control that is defined, unit-tested, and called by nothing is
   indistinguishable from one that does not exist — that is `design/gates.md`'s `unit only`
   status, and `docs/controls-crosswalk.md` exists because four rows once said "Replaced" about
   exactly that. New controls land with their caller, or with a row that says they have none.

Workflow-created commits use Conventional Commit syntax; human commits here conventionally use a
sentence that says what changed and why.

## Wanted contributions

Sized and genuinely wanted — each is a roadmap item or a recorded gap:

- **Backport workflow** (roadmap 25): deterministic-first — cherry-pick via plain git, escalate
  to a model only on conflict. The verb exists; nothing serves it.
- **RFE workflow** (roadmap 25): rides the triage vertical rather than growing its own.
- **Flaky-test adapter** (roadmap 26): detect, quarantine with a ticket trailer
  (`GATE-TESTGUARD-1` refuses silencing without one), report.
- **A SHARED-scope ledger store**: `LedgerStore.compare_and_set` is declared and deliberately
  refused at `LOCAL` scope; park/fan-out barriers need a store more than one machine can see.
- **A hosted OpenAI-compatible provider recipe**: the seam (an explicit `invoker_factory(registry=...)` passed to the adapter)
  exists and is documented; a worked gateway example is not.
- **GitLab live hardening**: the protocols and host-aware `init` ship; nobody has dogfooded a
  real MR pipeline end to end. First person to run one will find the honest gaps.
- **Redaction shapes**: `privileged/redact.py` masks structurally; a credential format you know
  and it does not is a two-line pattern plus a test.
- **Cookbook recipes**: ≤20 lines, executed by the test suite, solving something you actually
  did. One recipe that is true beats three that are aspirational.

Before a large change, read `design/adr/0001-pivot-to-runnable-framework.md` — the strengths to
preserve (the cassette seam, the credential-split trampoline, honest numbers, tighten-only
policy) are commitments, not habits, and a PR that trades one away needs to argue with the ADR,
not just the diff.
