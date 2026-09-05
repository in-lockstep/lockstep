# in-lockstep objectives

## The mission

> Enable teams of software engineers to work together using a framework to keep AI usage
> disciplined and structured, enabling collaborative development work to proceed on the hosted SCM
> of their choice using the provider(s) and model(s) of their choice constrained by the
> process(es) and policy of their choice.

The ten objectives in `CLAUDE.md` are how that sentence is made measurable. This file is the
ledger over them, and it exists for one reason: **the objectives had no ratchet.**

`design/gates.md` opens by arguing that a gate defined nowhere is indistinguishable from one that
does not exist, and its status column is a two-sided ratchet so the claim cannot decay. The
objectives had neither. `CLAUDE.md` says every change is measured against them and that a pull
request should say which one it serves — and that is genuinely happening, 17 citations across the
30 commits before this file was written. What was missing is the other direction. Nothing said
that O5 had been unmet since the beginning, or that O4's default contradicted O4's own sentence.
A survey found both, which is exactly the failure mode: a property somebody has to go looking for
is a property nobody is holding.

So this file joins the two ledgers. An objective's status is not an opinion recorded here; it is
constrained by the gates that carry it, and `tests/in_lockstep/test_objectives.py` fails when the
two disagree.

## Status

| Status | Meaning |
|---|---|
| `held` | Every gate carrying this objective is `held`, and nothing in the objective's text is outside them. |
| `partial` | Some of it is carried and some is not. The row names the gap. |
| `unmet` | No mechanism, or a mechanism nothing asserts over. |

There is no `unit only` here. That status is about a mechanism with no call site, which is a
property of a gate rather than of a direction; an objective served only by mechanisms nobody calls
is `unmet`, and the gates it cites are where the distinction is recorded.

**An objective no gate carries can never be `held`.** That is the rule doing the most work in this
file. It is the same argument as *absent is not zero*: a direction with no gate under it has not
been checked, and a ledger that let it read as satisfied would be a reassuring figure computed
from no evidence.

## What the ratchet checks

The two gate columns are the mechanism, and the split between them is the whole design. **Carried
by** names gates that hold and serve the objective. **Blocked on** names gates that do not hold and
whose closure would change the row. Every claim in either column is about one gate, so no gap can
hide inside a bundle: an objective blocked on four things does not go quiet when three of them
close.

Seven properties, all in `test_objectives.py`, which discharges `GATE-TEST-8`:

1. **Primary key.** The ids here are exactly `O1`-`O10`, each once. `GATE-TEST-7` had to be added
   to `gates.md` for the same reason, after a duplicate id let two rows discharge each other.
2. **The titles match `CLAUDE.md` verbatim.** Rewording an objective there without re-reading its
   row here fails the build. The objectives are the subject; this file is a claim about them, and
   a claim whose subject moved is not a claim.
3. **Every cited gate exists in `gates.md`.** A citation to a gate nobody wrote is the defect the
   gate ledger's own preamble is about, one level up.
4. **Every gate under *carried by* is `held`.** That is what carrying means, and it is the
   direction that catches over-claiming.
5. **Every gate under *blocked on* is `unmet`, `partial` or `unit only`.** This is the direction
   that matters, and it fires on the *first* gate to close rather than the last: implement one,
   and the row still listing it as a blocker turns red the moment its status flips. An earlier
   draft of this file asked only that *some* cited gate be open, and the four `GATE-IMPROVE` rows
   under O5 are why that was not enough — three of them could have been built without anything
   here noticing.
6. **`held` means blocked on nothing, and carried by something.** An objective no gate carries can
   never read as satisfied. Same argument as *absent is not zero*: a direction with no gate under
   it has not been checked, and letting it read as met would be a reassuring figure computed from
   no evidence.
7. **Every objective short of `held` states a gap**, and the *claimed by no objective* section
   lists exactly the unsettled gates that appear under no row's *blocked on*. `CLAUDE.md` is blunt
   about what that section means: surface serving no objective is surface to remove.

Deliberately not checked: whether a gap's prose is *true*. No test reads prose. What a test can do
is make it impossible for the gap to be silently empty, and impossible for the gates underneath it
to move without somebody standing in front of this table.

## The ledger

| Objective | Title | Status | Carried by | Blocked on | The gap |
|---|---|---|---|---|---|
| `O1` | Drop in, and reuse what the repository already has | held | `GATE-TOOLING-1`, `GATE-PROVISION-1`, `GATE-TOOLING-2` | — | — |
| `O2` | Onboarding is light | partial | `GATE-PROVISION-1`, `GATE-PROVISION-2`, `GATE-PLUGIN-2`, `GATE-RECORD-1`, `GATE-TOOLING-2` | `GATE-TOOLING-3` | The same gap as before, one ecosystem-family narrower. #237 taught detection to read `Cargo.toml`, `go.mod`, `pom.xml` and `build.gradle`, so a Rust, Go or JVM adopter no longer hand-writes what was in their tree. A Ruby, PHP, Elixir, .NET, C++ or Swift adopter still does. This row did not move with O1's, and the reason is the difference between the two sentences: O1 sanctions declining in its own text -- *detection that guesses is worse than detection that declines* -- so a stack that is read or else named is O1 satisfied. O2's standard has no such clause. It is that what a person writes by hand is the thing nobody could have discovered for them, and a `Rakefile` sitting in the tree is discoverable. `GATE-TOOLING-3` is where the remainder is recorded. |
| `O3` | The same process at a terminal and in CI | partial | `GATE-CI-1`, `GATE-RECORD-1` | `GATE-CI-2` | Every verb runs at a terminal, and on GitHub five trampolines carry the triggers and none of the logic. GitLab gets one active `review` job; the gate/work/propose split for the write verbs ships commented out, and there is no OIDC federation path, so keyless CI is GitHub-only. The scaffold says both plainly, which is the right way to ship a partial. It is still a partial. |
| `O4` | Every model call is recorded | held | `GATE-RECORD-1`, `GATE-RECORD-2`, `GATE-RECORD-3`, `GATE-RECORD-4`, `GATE-RECORD-5` | — | — |
| `O5` | The record is what teaches it | partial | `GATE-IMPROVE-1`, `GATE-IMPROVE-5`, `GATE-IMPROVE-6`, `GATE-IMPROVE-7`, `GATE-EVAL-2`, `GATE-EVAL-4` | `GATE-IMPROVE-2`, `GATE-IMPROVE-3`, `GATE-IMPROVE-4`, `GATE-IMPROVE-8`, `GATE-EVIDENCE-1`, `GATE-LEDGER-2`, `GATE-OUT-2` | The reading half is real and the writing half does not exist. `improve --explain` finds what recurs, attributes it to a declared body or to a dash, and prints the guard's verdict on that path; harvest turns a real session into cases and `eval run` settles them. Nothing drafts a prompt change, nothing measures a draft against the corpus, and nothing opens a pull request with the evidence attached — which is the whole second sentence of the objective. `improve` without `--explain` exits 3 saying so. This is the objective with the most complete substrate and the least surface. |
| `O6` | The model never holds a secret | held | `GATE-AUTH-1`, `GATE-AUTH-2`, `GATE-SANDBOX-1`, `GATE-EGRESS-1`, `GATE-EGRESS-2`, `GATE-EGRESS-3`, `GATE-REDACT-1`, `GATE-REDACT-2`, `GATE-GUARD-4`, `GATE-CFG-1`, `GATE-POLICY-2` | — | — |
| `O7` | Determinism first | held | `GATE-REVIEW-2`, `GATE-REVIEW-3`, `GATE-REVIEW-4`, `GATE-EVAL-4`, `GATE-COST-3` | — | — |
| `O8` | Extended without forking | held | `GATE-PACK-1`, `GATE-PACK-2`, `GATE-PACK-3`, `GATE-PACK-4`, `GATE-PACK-5`, `GATE-PLUGIN-1`, `GATE-PLUGIN-2`, `GATE-PLUGIN-3` | — | — |
| `O9` | New aspects on a verb that already exists | held | `GATE-REVIEW-3`, `GATE-PACK-5`, `GATE-REVIEW-5` | — | — |
| `O10` | It runs on itself | held | `GATE-CI-1`, `GATE-RECORD-1`, `GATE-TEST-3`, `GATE-REVIEW-5`, `GATE-CFG-3`, `GATE-CI-3` | — | — |

7 of 10 are `held`. That is the number this file exists to make visible, and it should be read
the way the gate ledger's own census is read: `partial` against a stated gap is a better position
than `held` against nothing, and the previous state of this repository was not `held` — it was
unmeasured.

Both sentences stating that count are checked against the table rather than trusted, because the
first draft of this file said *two* when one was true. O1 was `held` while the sentence was written
and `partial` by the time it was committed, and the prose one screen below the table went on saying
the old number — a figure nobody recomputed, in the document about figures nobody recomputes. It is
written `N of 10` in both files so that one pattern finds both.

It moved to two the first time a gate closed. #232 shipped `py.typed`, `GATE-PLUGIN-2` flipped, and
the ledger turned red on O2 and O8 — both of which were blocked on it and neither of which had been
re-read. That is the whole mechanism working on its first real use: O8 became `held`, and O2 did
not, because closing one blocker exposed a second that the old gap text had been obscuring.

It moved to three the same way, and O2 was again the row that did not move. #237 taught detection
to read `Cargo.toml`, `go.mod`, `pom.xml` and `build.gradle`, closing `GATE-TOOLING-2` — the only
gate cited under two rows' *blocked on*, so both turned red at once. O1 became `held` and O2 stayed
`partial`, and the reason is worth keeping because it is not obvious from the two titles. O1's own
sentence sanctions declining, so a stack that is either read or named by the decline satisfies it.
O2's does not: its standard is that what a person writes by hand is the thing nobody could have
discovered for them, and a Ruby team still hand-writes bindings out of a `Rakefile` that was in the
tree the whole time. Twice now, the first closure has exposed the row underneath rather than
finishing it — which is the argument for splitting *carried by* from *blocked on* in the first
place, made twice by the mechanism rather than by anyone's judgement.

It moved to four when #234 closed `GATE-REVIEW-4`, and that row is worth reading for the opposite
reason: nothing was exposed underneath, because the gap text had named the whole of it. It said a
finding's `path` **and `line`** came from the model and were checked against nothing, and the
issue filed against it proposed doing the path half and leaving the line. Doing only that would
have left O7 `partial` against a gap the row already knew about — which is the ledger asking for a
new gate to describe something it had described perfectly well the first time. So both halves
landed, and O7 is the first objective here to close because somebody read the gap and finished it
rather than because a gate happened to flip.

Five, when #249 closed `GATE-CI-3`. Both halves of that row were about this repository not doing
what it tells adopters to do, and the second half turned out not to be a policy question at all.
`doctor` was `continue-on-error: true` at all four of its call sites, which reads as a decision
nobody made — and was in fact a decision the tool forced. It reported every unreadable answer
about branch protection as *the default branch has no protection rule*, at ERROR, and a CI job's
token cannot read that API. So a fully protected `main` was reported unprotected on every run,
`doctor` exited 1 every time, and the only way to keep the job usable was to throw its verdict
away. *Absent is not zero*, inside the tool whose job is finding absent controls, costing this
repository the whole of the second clause. The lesson generalises past this gate: a diagnostic
that cannot distinguish **did not hold** from **could not look** will have its verdict discarded,
and then it gates nothing at all.

What O10 covers, plainly, so the `held` is readable: reviews, fixes, implementations and
measurement, which are the four things the objective names. `backport`, `triage`, `rfe`, `pack`
and `market` are not exercised here, and that is not a gap this row is hiding — this repository
has no maintenance line to backport to and is not an adopter of packs, and a gate demanding
otherwise would be demanding a fiction rather than evidence.

Six, when #243 and #264 closed `GATE-RECORD-5` together — and *together* is the whole of it. Each
was a hole the other hid. The seam wraps the provider the framework builds and fires on the tape
the run carries; the five direct verbs never gave the run a tape, so a repository binding its own
adapter recorded nothing **and** heard nothing, because the bypass detection is reached from the
same reporting path that was missing. Closing either alone would have moved this row on a claim
the other half falsified, which is why the two issues were done as one change.

What `every` means here is worth stating, because O4's word is absolute and the row is now
`held`. Every provider the framework builds is wrapped, and every provider handed to it by an
adopter's own `invoker_factory=` is wrapped after the fact — the framework cannot reach inside the
lambda, but it holds what the lambda returned. Past that is a bespoke adapter that takes no
factory and constructs the invoker inside `invoke`: nothing can wrap it, and the framework should
not pretend otherwise. What it does instead is refuse to report a reassuring zero. That run is
compared against its own spend and told a model was called the recorder never saw, and there is a
test standing on that boundary rather than a sentence promising it.

## Claimed by no objective

Every gate that is `unmet`, `partial` or `unit only` and cited by no row above. `deferred` and
`retired` are exempt: one is past the cut line by a recorded decision, the other has no subject
left. The list is recomputed by the test, so a gate cannot quietly join or leave it.

| Gate | Status | Why it is here |
|---|---|---|
| `GATE-RETRY-5` | unit only | `Retry` middleware is constructed by nothing. Its own row already states the honest resolution — bind it or retire it in favour of the transport-level retry that is bound and live — and that is a decision rather than a patch. Until it is taken, no objective is served by the row, which is the fact this section exists to keep in view. |

## What the mission says that the ten do not

Recorded here rather than smoothed over, because the mission arrived after the objectives and is
wider than they are. Two clauses of it are carried by no objective at all, and a third is
carried by an objective whose gate for it is unmet:

**"teams of software engineers to work together" / "collaborative development work."** Nothing in
O1-O10 is about more than one engineer. The review conversation reaching the next `/fix` as
untrusted context is collaborative in effect, and the ledger is shared in effect, but no objective
names collaboration as a direction — so nothing measures whether a second engineer arriving at a
repository is served or obstructed.

**"the provider(s) and model(s) of their choice."** No objective mentions provider or model
choice. `llm/providers/` carries six of them behind a registry that refuses an endpoint mismatch,
which is real and load-bearing surface — and by `CLAUDE.md`'s own rule it is currently surface
that cites no objective. The mission is what justifies it; the ten have not caught up.

**"the policy of their choice"** is the third clause worth writing down, and it is the one that
is not a gap in the objectives. It is carried: O8 is why an adopter binds their own `PathPolicy`,
`InvokePolicy` and `EgressPolicy` in a `lockstep.py` the framework never edits, and O6 is why the
egress and residency halves of that are enforced rather than declared. What the clause changes is
the weight of `GATE-POLICY-2`, which is listed above as O6's blocker. That gate says every field
`Policy` carries reaches something that enforces it, and that it does not: `network`,
`permissions` and the three credit fields are merged by `resolve()`, printed by `ls` and reported
in the receipt, and read by nothing else.

Before the mission was written down that read as a duplicate surface, because the sandbox and the
egress policy cover the same ground and do enforce. It reads differently now. An adopter choosing
a policy is doing the thing the mission exists for, and five of the fields they can choose are
inert — which makes the receipt, the artefact a security reviewer reads to find out what is in
force, the place the gap surfaces.

The remaining clauses map cleanly. "The hosted SCM of their choice" is O3. "Constrained by the
process(es) of their choice" is O1 and O8. "Disciplined and structured" is O4, O6 and O7.

Whether the answer to the first two is more objectives or a wider reading of the existing ones is
a decision, not a patch — and it is recorded here unresolved rather than settled by whoever
noticed it.
