# Strategy selection

**Status:** Deferred, designed · **Amends:** `in-lockstep-design.md` §18.1 · **Not implemented**

§18.1 first recorded the `StrategySelector` as withdrawn: a strategy chosen from ticket text is a
strategy an untrusted author can steer, and a ticket is `UNTRUSTED_EXTERNAL` by construction. The
objection is right and it was stated imprecisely, and the imprecision cost more than it had to.

**It is not selection that is unsafe. It is selection across a capability line.** Steering only
matters if the destinations differ in something worth stealing. `Oneshot` and `TDD` hold the same
`AGENCY` and the same policy floor, so choosing between them changes the approach and the cost and
not the authority — and untrusted input is allowed to influence a quality decision, because the
work being requested is the thing the work should be shaped by. Choosing between either of those and
a strategy declaring `REACHES_NETWORK`, or one carrying a path grant, is privilege escalation with
extra steps.

This document is the design that follows from that distinction: one startup refusal, one rule about
signals, and an honest account of what neither removes.

---

## 1. The invariant

A selector is constructed from a **closed set of already-bound candidates — objects, never names —
whose declared `capabilities` are identical.** Startup refuses otherwise, with `DivergentAgency`,
beside `UngatedAgency` and `UndeclaredAgency`. Those are already refusals about the shape of a
lifecycle rather than about a run, made where they cost nothing, and this is the same kind.

What the invariant buys is that every gate sees one picture regardless of what gets chosen.
`ApprovalGate`, the budget refusal, `Retry`'s re-invocation check and the mandatory-egress trigger
all key on `capabilities` read off the **bound object** — and the bound object is the selector,
whose set is the shared set. So an outsider who influences the choice gains no authority they did
not already have, which is the whole claim.

**The selector is itself an adapter.** It binds under the request type like any strategy, declares
the shared capability set, and delegates `invoke` to whichever candidate it picked. That is what
keeps the rest of the framework unchanged, and it keeps `ls` honest:

```
bindings
  Implement            -> Selecting       (singleton, explicit)
                          Oneshot | TDD
                          reads_repo, spends_budget, writes_files, executes_code
```

A fan-out hidden behind a name would be the failure `ls` exists to prevent; a selector that prints
its candidates and their shared set is the container still answering *what will actually run*, with
*and what decides* added.

---

## 2. Signals, and the line they may not cross

A repository may genuinely want a research strategy that reaches the network for some work and not
for other work. The invariant forbids deciding that from a ticket; it does not forbid deciding it.

The second rule is about the **provenance of the deciding signal**, which this framework already
classifies for exactly this purpose. A selector receives classified `Signals` rather than raw
strings, so the question "did an untrusted signal cross a capability line" is one the framework
answers rather than one a reviewer has to notice.

| Signal | Provenance | Within an equal set | Across a capability line |
|---|---|---|---|
| `ticket.labels`, `ticket.type`, `ticket.title` | `UNTRUSTED_EXTERNAL` | yes | **no** — anyone who can file an issue writes these |
| `change.files`, `change.size` | `UNTRUSTED_EXTERNAL` | yes | **no** — a fork's diff is authored by whoever opened it |
| `approval.by`, `approval.attended` | trusted | yes | yes — `in-lockstep gate` already decided this person may spend |
| `repo.branch`, `repo.protected_paths` | trusted | yes | yes — read from the trusted ref, not from the change |
| an AI triage of the ticket | `UNTRUSTED_EXTERNAL` | yes, with §4's caveat | **no** — its input is the ticket |

Ticket content already carries `Provenance.UNTRUSTED_EXTERNAL` at its source, in
`Ticket.as_context`, and the review adapter classifies a diff the same way. Nothing new has to be
decided about what is trusted; the classification exists and this reads it.

---

## 3. What it looks like

```python
# .lockstep/lockstep.py
from in_lockstep.select import Signals, Trusted

oneshot = lockstep.use(Oneshot)
tdd     = lockstep.use(TDD)

def approach(s: Signals):
    # Untrusted signals, and that is fine: both candidates hold the same set.
    if "needs-tests" in s.ticket.labels or s.change.files > 8:
        return tdd
    return oneshot

lockstep.select(Implement, among=[oneshot, tdd], by=approach)
```

Three things in that shape are load-bearing.

`among=` takes the adapter **objects** the module already bound, so a rule cannot reach a strategy
the repository did not choose to have. `by=` is an ordinary function in the file loaded from a
trusted ref, so the rule is reviewed code rather than data somebody edits elsewhere. And the return
value is one of the candidates: a rule points at a strategy, it cannot construct one.

Crossing the line is the same call plus a line naming the trusted signal that justifies it:

```python
lockstep.select(Implement, among=[oneshot, deep], by=approach, crossing=Trusted.ACTOR)
```

Without `crossing=`, a capability-divergent set raises `DivergentAgency` at startup. With it, the
divergence is permitted and the selector refuses at construction if `approach` reads any untrusted
signal. `crossing=` is the greppable spelling of a decision, the way `UnsandboxedEgress` is named
after what it does rather than hidden behind a flag.

### What the rest of the framework shows

- **`ls`** prints the candidates and the shared capability set, as above.
- **The receipt** (`pack describe`) gains the candidate list and the crossing declaration, so a
  selector is as visible in an audit as a binding.
- **The ledger** records the chosen `strategy_id` *and the signals that produced the choice*. Which
  approach ran is now partly influenced by input a stranger wrote, so an investigation needs the
  inputs and not only the outcome.

---

## 4. What this does not fix

**Cost steering.** Within a capability-equal set, an outsider can still steer toward the most
expensive candidate: `TDD` runs two model phases and a test suite where `Oneshot` runs one. The
bound is the per-run budget and the rolling daily ceiling, which already bounded anyone able to
trigger a run at all — so selection adds no new spend authority, and it does make each triggered
run cost more. A repository whose candidates differ wildly in cost should know that is the shape of
its exposure.

**An AI triage decides how to spend by spending.** §5.7 offered a model-driven selector. Under the
invariant it is permissible and it remains a prompt-injection surface whose payoff is cost rather
than authority. Worth allowing, worth saying: a rule you can read beats a rule you have to evaluate,
and the cheap deterministic version should be the documented default.

**Eval subjects fragment.** `strategy_id` is part of the eval-subject key (§8.2, §17.8), so a
repository that selects is measuring two subjects, and a pass rate averaged across them is a blend
rather than a measurement. `report` and `pack try` would need to group by selected strategy — and a
selector shipped without that change would quietly turn a number this project takes seriously into
one that means less than it looks like.

**The honest summary.** This converts a privilege-escalation hole into a cost-amplification nuisance
bounded by controls that already exist. That is a real improvement and it is not nothing-to-nothing.
If the bar is *an outsider's text influences no part of a run*, the feature stays withdrawn, and
that is a coherent position rather than a failure to try.

---

## 5. Why this is deferred and not open work

Nothing in this repository selects between strategies. The framework ships one implement pair
(`Oneshot`, `TDD`) and one fix strategy, no third-party strategies exist in the wild, and the
extension-pack work that would produce them landed a week ago and has not been dogfooded
(`design/extension-packs.md` §11).

A mechanism built before it has a user has its design decided by guesses — which candidates people
actually want to route between, whether the deciding signal is usually the ticket or usually the
actor, whether anyone wants to cross a capability line at all. When a second strategy somebody
genuinely wants routed to arrives, most likely from a pack, this invariant is what it should be
built against.

Two gates would come with it, in the shape `design/gates.md` uses:

| Gate | Assertion |
|---|---|
| `GATE-SELECT-1` | A candidate set whose declared capability sets are not identical raises `DivergentAgency` at startup — before a run, not at the call — and no `crossing=` is accepted for an untrusted signal. |
| `GATE-SELECT-2` | The chosen `strategy_id` and the signals that produced it reach the ledger record, and a selection made from `UNTRUSTED_EXTERNAL` signals is marked as such where a reader will see it. |
