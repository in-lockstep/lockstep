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
| `O1` | Drop in, and reuse what the repository already has | partial | `GATE-TOOLING-1`, `GATE-PROVISION-1` | `GATE-TOOLING-2` | Detection reads `pyproject.toml`, `package.json` and the `Makefile`, and `RepoFacts.stack` is `python`, `node` or empty. A repository whose build is declared in `Cargo.toml`, `go.mod`, `pom.xml` or `build.gradle` is discovered only through a `Makefile` it may not have. Declining is correct and O1 says so; not discovering is the half that is missing, and `GATE-TOOLING-2` is where it is recorded. |
| `O2` | Onboarding is light | partial | `GATE-PROVISION-1`, `GATE-PROVISION-2`, `GATE-PLUGIN-2`, `GATE-RECORD-1` | `GATE-TOOLING-2` | The mechanism is held. `init` writes a twelve-line `lockstep.py` and a trampoline, everything it writes survives `mypy --strict` now that a `py.typed` ships, and a scaffolded repository reaches a green run with no hand-editing. What is left is the same gap O1 carries, arriving here as its consequence: detection reads three files, so an adopter whose build is declared in `Cargo.toml`, `go.mod` or `pom.xml` hand-writes bindings that were discoverable from a file in their tree. That is precisely the thing O2 says a person should never have to write. The old gap named N3 -- nobody has timed `init` to a first useful review -- which is a measurement of adoption rather than a property of the framework, and it was doing the work of hiding this one. |
| `O3` | The same process at a terminal and in CI | partial | `GATE-CI-1`, `GATE-RECORD-1` | `GATE-CI-2` | Every verb runs at a terminal, and on GitHub five trampolines carry the triggers and none of the logic. GitLab gets one active `review` job; the gate/work/propose split for the write verbs ships commented out, and there is no OIDC federation path, so keyless CI is GitHub-only. The scaffold says both plainly, which is the right way to ship a partial. It is still a partial. |
| `O4` | Every model call is recorded | partial | `GATE-RECORD-1`, `GATE-RECORD-2`, `GATE-RECORD-3` | `GATE-RECORD-4` | The mechanism is complete: every model-calling command takes `--record`, the seam in `ai/bootstrap.py` wraps whatever provider an adapter builds, and a run that spends tokens the recorder never saw says so rather than reporting a reassuring zero. What is not held is the default. O4's own sentence is that recording *is not an option a run turns on*, and it is a flag that is off, so a laptop `in-lockstep review` keeps nothing. Every path the framework itself drives passes the flag; the path a person meets first does not. |
| `O5` | The record is what teaches it | partial | `GATE-IMPROVE-1`, `GATE-IMPROVE-5`, `GATE-IMPROVE-6`, `GATE-IMPROVE-7`, `GATE-EVAL-2`, `GATE-EVAL-4` | `GATE-IMPROVE-2`, `GATE-IMPROVE-3`, `GATE-IMPROVE-4`, `GATE-IMPROVE-8`, `GATE-EVIDENCE-1`, `GATE-LEDGER-2`, `GATE-OUT-2` | The reading half is real and the writing half does not exist. `improve --explain` finds what recurs, attributes it to a declared body or to a dash, and prints the guard's verdict on that path; harvest turns a real session into cases and `eval run` settles them. Nothing drafts a prompt change, nothing measures a draft against the corpus, and nothing opens a pull request with the evidence attached — which is the whole second sentence of the objective. `improve` without `--explain` exits 3 saying so. This is the objective with the most complete substrate and the least surface. |
| `O6` | The model never holds a secret | partial | `GATE-AUTH-1`, `GATE-AUTH-2`, `GATE-SANDBOX-1`, `GATE-EGRESS-1`, `GATE-EGRESS-2`, `GATE-EGRESS-3`, `GATE-REDACT-1`, `GATE-REDACT-2`, `GATE-GUARD-4`, `GATE-CFG-1` | `GATE-POLICY-2` | The best-executed objective here, and not `held`. Credentials are dropped from every child environment, a container is preferred and refused rather than silently downgraded when the caller asked for one, `run_script` refuses outright until a runner is bound, and the opt-out is a differently-named class somebody can grep for. What is unmet is a second surface: `Policy.network` and `Policy.permissions` are merged, printed by `ls` and reported in the receipt, and read by nothing. The ground is covered by the sandbox and the egress policy. A security field that reads as in force while enforcing nothing is still an O6 defect, because the receipt is what a reviewer believes — and the mission names *the policy of their choice* directly, so those five fields are not a duplicate surface but a choice an adopter is invited to make and nothing honours. |
| `O7` | Determinism first | partial | `GATE-REVIEW-2`, `GATE-REVIEW-3`, `GATE-EVAL-4`, `GATE-COST-3` | `GATE-REVIEW-4` | Well served where it was argued for: a ticket number and a lens name are resolved in Python before any credential is read, backport cherry-picks deterministically and reaches a model only for a conflict, and harvest and eval settle without one. One leak, in the place the objective names. A review finding's `path` and `line` come from the model and are checked against nothing, though `git diff --name-only base...head` knows which files the change touched for free. That is arithmetic wearing a prompt, and it decides where an inline comment lands. |
| `O8` | Extended without forking | held | `GATE-PACK-1`, `GATE-PACK-2`, `GATE-PACK-3`, `GATE-PACK-4`, `GATE-PACK-5`, `GATE-PLUGIN-1`, `GATE-PLUGIN-2` | — | — |
| `O9` | New aspects on a verb that already exists | held | `GATE-REVIEW-3`, `GATE-PACK-5` | — | — |
| `O10` | It runs on itself | partial | `GATE-CI-1`, `GATE-RECORD-1`, `GATE-TEST-3` | `GATE-CFG-3` | Five workflows are triggered on this repository and all of them record. What is not dogfooded is this repository's own lifecycle module. `make check` runs `ruff check src tests` and `mypy src`, so the 771 lines of `.lockstep/lockstep.py` — every adapter binding, the path tiers, the egress policy — are checked by neither. Pointing mypy at it finds a function defined twice, identically, the second shadowing the first. It is the file every adopter copies the shape of. |

2 of 10 are `held`. That is the number this file exists to make visible, and it should be read
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
