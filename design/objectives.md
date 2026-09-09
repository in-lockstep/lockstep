# in-lockstep objectives

## The mission

> Enable teams of software engineers to work together using a framework to keep AI usage
> disciplined and structured, enabling collaborative development work to proceed on the hosted SCM
> of their choice using the provider(s) and model(s) of their choice constrained by the
> process(es) and policy of their choice.

The thirteen objectives in `CLAUDE.md` are how that sentence is made measurable. This file is the
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

Eight properties, all in `test_objectives.py`, which discharges `GATE-TEST-8`:

1. **Primary key.** The ids here are exactly `O1`-`O13`, each once. `GATE-TEST-7` had to be added
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
8. **The count of `held` gates no row cites is written down, and is exact.** Property 7 sees only
   the unsettled half of the ledger: a gate that holds and that no objective claims was invisible
   to it, and thirty-nine of them were when #273 counted. The *held, and claimed by no objective*
   section states the number; the test recomputes it and fails in both directions, naming the
   gates, so a held gate can neither join the uncited set quietly nor leave it without the
   sentence being lowered.

Deliberately not checked: whether a gap's prose is *true*. No test reads prose. What a test can do
is make it impossible for the gap to be silently empty, and impossible for the gates underneath it
to move without somebody standing in front of this table.

## The ledger

| Objective | Title | Status | Carried by | Blocked on | The gap |
|---|---|---|---|---|---|
| `O1` | Drop in, and reuse what the repository already has | held | `GATE-TOOLING-1`, `GATE-PROVISION-1`, `GATE-TOOLING-2`, `GATE-CI-3`, `GATE-TOOLING-4`, `GATE-VALIDATE-1`, `GATE-VALIDATE-2` | — | — |
| `O2` | Onboarding is light | held | `GATE-PROVISION-1`, `GATE-PROVISION-2`, `GATE-PLUGIN-2`, `GATE-TOOLING-2`, `GATE-TOOLING-3`, `GATE-FIXTURE-1`, `GATE-SEARCH-1` | — | — |
| `O3` | The same process at a terminal and in CI | held | `GATE-CI-1`, `GATE-RECORD-1`, `GATE-CI-2`, `GATE-CI-4`, `GATE-OUT-7`, `GATE-OUT-5`, `GATE-FORK-1` | — | GitLab is scaffold-tested and never executed: the parity walk and the credential split are asserted over the file an adopter is given, and only a pipeline on an instance this repository does not have would move README's row from `partial`. The learning loop has no GitHub scaffold; its GitHub spelling is this repository's own `improve.yml`. |
| `O4` | Every model call is recorded | held | `GATE-RECORD-1`, `GATE-RECORD-2`, `GATE-RECORD-3`, `GATE-RECORD-4`, `GATE-RECORD-5`, `GATE-LEDGER-10`, `GATE-LEDGER-11`, `GATE-JUDGE-3`, `GATE-DELEGATE-1`, `GATE-COST-7`, `GATE-RECORD-6` | — | — |
| `O5` | The record is what teaches it | held | `GATE-IMPROVE-1`, `GATE-IMPROVE-2`, `GATE-IMPROVE-3`, `GATE-IMPROVE-4`, `GATE-IMPROVE-5`, `GATE-IMPROVE-6`, `GATE-IMPROVE-7`, `GATE-IMPROVE-8`, `GATE-EVAL-2`, `GATE-EVAL-4`, `GATE-EVIDENCE-1`, `GATE-OUT-2`, `GATE-LEDGER-12`, `GATE-JUDGE-1`, `GATE-JUDGE-3`, `GATE-LEDGER-2`, `GATE-LEDGER-4`, `GATE-EVAL-1`, `GATE-EVAL-3` | — | The reading half, the writing half and, since PR-18, the half that reads back: `improve` reads the ledger for a qualifying trend, drafts a change to the one declared body that trend is attributed to, measures the draft against the promoted corpus on both arms -- the bound judge settling the rubrics, a verdict kept beside its case -- opens the change with its scorecard, and `report --around` compares the runs after the merge with the runs before it on that body's subject (`GATE-LEDGER-2`). What this repository's own ledger can show of that last step was thin when the row moved -- the Phase 4 merge had two subject-carrying runs after it, because the required check's fan-out records carried its lenses as steps without a subject -- and the fix that followed stamps each lens step with its subject and its bill, so every check since is four comparable runs; the records before it stay thin and the window says so. On this repository the corpus is one promoted case with one kept verdict, so the loop refuses before spending until somebody tightens a case to what a correct answer would have said -- the labelling act it learns from. The reason the first dispatched `improve` run actually gives belongs in this row once it has run. |
| `O6` | The model never holds a secret | held | `GATE-AUTH-1`, `GATE-AUTH-2`, `GATE-SANDBOX-1`, `GATE-SANDBOX-2`, `GATE-EGRESS-1`, `GATE-EGRESS-2`, `GATE-EGRESS-3`, `GATE-REDACT-1`, `GATE-REDACT-2`, `GATE-REDACT-3`, `GATE-GUARD-4`, `GATE-CFG-1`, `GATE-POLICY-2`, `GATE-CFG-2`, `GATE-RETRY-6`, `GATE-APPROVAL-1`, `GATE-GUARD-1`, `GATE-GUARD-2`, `GATE-DELEGATE-1`, `GATE-SEARCH-1` | — | The container a model-staged test needs is one whose image carries the suite's dependencies, and `init` names that image rather than deriving it, so an adopter's first `implement` or `fix` refuses `sandbox.host_fallback` until a person writes the line (`GATE-SANDBOX-2` says why a stack image will not do). This repository's own container mounts the `.venv` its runner built, which runs the suite wherever its packages import on linux -- everywhere, while they stay pure Python -- and fails at import, naming the module, the day one does not. Egress for the run as a whole stays opted out here and in the scaffold (`GATE-EGRESS-2`, #315). |
| `O7` | Determinism first | held | `GATE-REVIEW-2`, `GATE-REVIEW-3`, `GATE-REVIEW-4`, `GATE-EVAL-4`, `GATE-COST-3`, `GATE-SHAPE-1`, `GATE-VERDICT-1`, `GATE-PROGRESS-1`, `GATE-OUT-3`, `GATE-JUDGE-2`, `GATE-WORKSPACE-1`, `GATE-VALIDATE-2` | — | — |
| `O8` | Extended without forking | held | `GATE-PACK-1`, `GATE-PACK-2`, `GATE-PACK-3`, `GATE-PACK-4`, `GATE-PACK-5`, `GATE-PLUGIN-1`, `GATE-PLUGIN-2`, `GATE-PLUGIN-3`, `GATE-DOCS-1`, `GATE-BODY-1`, `GATE-POLICY-1` | — | — |
| `O9` | New aspects on a verb that already exists | held | `GATE-REVIEW-3`, `GATE-PACK-5`, `GATE-REVIEW-5`, `GATE-LENS-1` | — | — |
| `O10` | It runs on itself | held | `GATE-CI-1`, `GATE-RECORD-1`, `GATE-TEST-3`, `GATE-REVIEW-5`, `GATE-CFG-3`, `GATE-CI-3`, `GATE-DOGFOOD-1`, `GATE-LEDGER-10`, `GATE-CI-5`, `GATE-VALIDATE-1` | — | Reviews on every pull request, a review asked for on a thread, a fix and measurement have closed here (`GATE-DOGFOOD-1` names the runs); an implementation has not, and neither has delegation: `.lockstep/lockstep.py` binds `TDD(delegation=True)`, so the next `/implement` here holds `delegate` (`GATE-DELEGATE-1`), and no run has used it yet. Deliberately not dogfooded on this repository's own runs, and each a separate decision: egress enforcement and the daily ceiling (off, documented), GitLab (no instance; O3 says so), and any provider but Anthropic (`GATE-COST-3` prices it; nothing here has routed to it). The required `review` check is enforced by choice -- `doctor` now says so (`DOC127`, `DOC128`) -- and stays that way while one engineer is the administrator. |
| `O11` | The provider and the model are the adopter's, not ours | held | `GATE-AUTH-2`, `GATE-RESIDENCY-1`, `GATE-COST-4`, `GATE-LENS-1`, `GATE-MODEL-1`, `GATE-JUDGE-2`, `GATE-COST-3` | — | — |
| `O12` | A second engineer is served, not obstructed | held | `GATE-TEAM-1`, `GATE-TEAM-2`, `GATE-LEDGER-10`, `GATE-LEDGER-11`, `GATE-LEDGER-12`, `GATE-REVIEW-6`, `GATE-OUT-6`, `GATE-LEDGER-1`, `GATE-LEDGER-3`, `GATE-LEDGER-7`, `GATE-LEDGER-8`, `GATE-LEDGER-9`, `GATE-REVIEW-1`, `GATE-VERDICT-2`, `GATE-RECORD-6`, `GATE-FORK-1` | — | — |
| `O13` | A run is bounded before it starts | held | `GATE-BUDGET-1`, `GATE-COST-1`, `GATE-COST-2`, `GATE-COST-5`, `GATE-DEADLINE-1`, `GATE-RETRY-4`, `GATE-TESTGUARD-1`, `GATE-COST-6`, `GATE-ASYNC-3b`, `GATE-DELEGATE-1`, `GATE-COST-7` | — | Written in Phase 8 (PR-19) from the mission's *disciplined* clause and not from the gates, and then found to be carried by nine that already held: the startup refusal of an undeclared ceiling (`GATE-BUDGET-1`), the projection checked before the turn it prices (`GATE-COST-2`) and accumulated per run rather than per process (`GATE-COST-1`), the org-level spend limit `doctor` demands be attested (`GATE-COST-5`), the deadline and the kill switch stopping a loop mid-flight (`GATE-DEADLINE-1`), the retry that is not made when it would outlast the clock (`GATE-RETRY-4`), the refusal of a change that loosens the tests bounding it (`GATE-TESTGUARD-1`), and the two Phase 5 added -- a joint ceiling across a fan-out (`GATE-COST-6`) and the switch reaching every in-flight branch (`GATE-ASYNC-3b`) -- which waited in the uncited count for exactly this row rather than being stretched under O7. What the row does not claim: a daily ceiling on this repository's own runs, which is deliberately off (O10's row lists it as not dogfooded), and any bound on wall clock the deadline does not already state. Found on 2026-09-08, while reading a failed `/implement`: every ceiling under this row had been tripping at half its stated value, because an outcome was charged onto the `Spend` its invoker had already charged (`GATE-COST-7`). A bound that fires early is still a bound, but a bound nobody can read is not a declaration, and the figures it produced are now marked as doubled rather than quietly kept. |

13 of 13 are `held`. That is the number this file exists to make visible, and it should be read
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

On 2026-09-07 the loop closed (`GATE-DOGFOOD-1`, #312). Until then this row's `held` rested on
reviews alone: the framework had never opened a pull request here, and the two runs whose work
half succeeded had died at `propose`. The ninth `/fix` on #319 opened #343 and it merged -- the
first change in this repository's history a model staged and a person merged -- and it took eight
framework fixes to get there, each one a defect only a real run on a real runner could have shown
(the row lists them). `improve.yml` has run twice and refused to propose twice, by name, which is
the learning loop working on no evidence rather than not working. What has not happened is said in
the row's gap: no implementation, and no `/review <lens>` on a thread.

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

Eight, when #236 closed `GATE-CI-2` by shipping GitLab's gate/work/propose jobs active rather than
commented out. This row's `held` is worth reading with its limit attached, because the limit is
unusual: **no GitLab pipeline has ever run them.** This repository is GitHub-hosted, so it cannot
execute them, and whether a federation rule can exist for a GitLab issuer is a question for the
provider rather than for any file here.

It is `held` anyway, on the terms `GATE-RECORD-1` established: assertions over the scaffold an
adopter is actually given, compared against the GitHub trampoline rather than against a list
written in the test, since a list is a third statement of the contract that can drift from both.
O3's own sentence is what makes that sufficient — *the repository's own SCM, GitHub **or**
GitLab* — so the objective is about an adopter's host being served, not about this repository
running on both. Where *we run it here* lives is O10, and that row already states what this
repository does and does not dogfood. Recording the distinction is the alternative to letting a
`held` quietly mean more than it does.

Still eight, when #268 and #269 closed `GATE-EVIDENCE-1`, and O5 is the row that did not move —
correctly, because that gate was one of seven under its *blocked on* and the other six are the
writing half its gap text has named from the start. The closure is recorded anyway for what it
was: the first promotion. The shipped cassette's request had shipped beside it in `request.json`
since #129, and `harvest` had never been taught the file, so the one recording every clean
install has was told to re-record itself. Teaching the reader turned it into a case; promoting
that case gave the gate its subject; and because the case is the demo's, the first thing
published forever into `evidence/` was something the package already published. The path the
corpus was written for — a CI run's artifact, downloaded and read by a person — has still carried
no promotion, and the gate row says so rather than letting `held` cover it.

Still eight, when #163 closed four of O5's blockers at once -- `GATE-IMPROVE-2`, `-3`, `-4` and
`-8` -- and O5 is again the row that did not move. Four is the largest single movement this ledger
has recorded, and the row stayed `partial` on two gates its gap text has named since the file was
written, which is the mechanism working rather than failing: the writing half of the objective now
exists, and what it cannot yet do is stated by the two rows it is still blocked on. One of them was
decided rather than closed. #266 asked whether the loop's before/after arm should go through
`ledger.compare()`; it does not, because it compares answers to a case and not windows of runs,
and the row records the caller that would -- a post-merge comparison -- as the thing that does not
exist yet. Recording a decision not to close a gate is the same act as closing one: the ledger
says what is true, and the number stays where the evidence leaves it.

Still eight, when #164 closed `GATE-TEAM-1`, and O12 moved from `unmet` to `partial` rather than to
`held`. It could have read `held`: one gate carried it and nothing blocked it, which is the rule's
letter. It does not, because the gate's own row says the dogfood ledger holds one human under two
spellings and forty-five local runs with no identity at all -- so the spread the command prints
on this repository is between spellings, not people. Rather than let one gate carry the mission's
"teams" half on that evidence, the row names what is missing as a gate of its own: `GATE-TEAM-2`,
an opt-in local identity, which the issue itself refused to make a default. That is the third
time this ledger has kept a row short of `held` by naming the gate underneath, and the first time
it did so on the way up from `unmet`.

Nine, when #274 closed `GATE-MODEL-1` and O11 moved to `held`. Its gap had named exactly one
thing -- the third refusal, a capability declared per model and read by nothing -- and closing it
exposed nothing underneath, so the row finished the way O7's did. Two things about the closure
are worth keeping. The field could not be wired as it was declared: `local` said
`structured_output=False` from when the flag meant a native JSON mode the framework never used,
and a refusal keyed on that value would have refused this repository's own `triage` route, the
`$0` path the docs show. So the flag was given the meaning the framework can check -- answers a
schema when asked -- and the registration was corrected before the check went live, which is the
order the ledger asks for: a control that reads as in force has to be true of the thing it reads.
And the row is `held` on a refusal no shipped registration triggers, the same standing
`GATE-AUTH-2` has had all along: the check runs before every call, and the refusal is exercised
against a registration constructed to declare the incapacity, because the declaration is the
operator's and the shipped ones make none.

Ten, when #289 closed `GATE-TEAM-2` and O12 moved to `held`. The #164 paragraph above kept the
row `partial` on purpose, and named the one thing missing as a gate: a local run carried no
identity, so the consistency question could not be asked of the runs a team mostly makes. That
gate now holds, on the terms the issue set -- one line a repository writes and nothing detects,
because the alternative was naming people who never chose to be named. So the two gates carry
the objective's sentence between them: what one engineer's run produced is legible to the next,
and whether people get consistent results can be asked of every run, not only the ones a host or
a grant happened to stamp. What has not changed is stated where it was: this repository's own
ledger holds one engineer, and opting in here gives him a third spelling, so the spread the
command prints on this repository is still between spellings and not people. That is a fact
about this repository's headcount and not a gap in the framework -- no gate can close it, and
`GATE-TEAM-1`'s row keeps saying it -- which is why the objective is `held` on the mechanism the
tests exercise over a two-person ledger rather than on the evidence this one repository can
produce. Still the objective with the least under it; two gates rather than one.

Eleven, when PR-17 (#317) closed `GATE-TOOLING-3` and O2 moved to `held`, the row that did not
move twice. Nothing was exposed underneath this time, and the reason is the one the two earlier
paragraphs give: the gap text had named the remainder exactly -- seven manifests, each a
repository saying how it tests itself, each declined by name -- and the closure read all seven by
the two rules #237 set rather than by any looser one. `mix test`, `dotnet test`, `swift test` and
`bazel test //...` bind from the file because the toolchain guarantees them; `composer test`,
`bundle exec rake test` and `ctest` bind only from the line in the file that declares them, and
a Rakefile without a `test` task is told that rather than "unsupported". What a Ruby, PHP,
Elixir, .NET, Swift, Bazel or CMake adopter writes by hand is now what nobody could have
discovered for them, which is O2's sentence. What is still declined is stated in the same
place: a CMake configure step, and a `run` line for any of the seven.

Twelve, when PR-18 (#318) closed `GATE-LEDGER-2` and O5 moved to `held`. The row had named its
own caller since #266 -- a post-merge window comparison, did the runs after a merged proposal
differ from the runs before it -- and `report --around` is that caller, with no verdict in its
output because the reading is a person's. Nothing was exposed underneath; what was exposed
beside it is worth keeping: the first real subject, the Phase 4 merge, had two subject-carrying
runs after it, because the required check's fan-out record carried its lenses as steps and a
step carried no subject. The fix landed one merge later: a lens step carries its subject and its
bill, and the window reads it as a run of its own. Every objective is `held`; the ledger's next
work is the re-read Phase 8 owes each row, not a closure.

Thirteen, when PR-19 wrote O13 and it arrived `held`. The opposite of O12's arrival: that row
came `unmet` because nothing served it, and this one came carried by nine gates that had held for
months under nobody -- the seven spend-and-time rows #273's count found sitting in the uncited
half, and the two Phase 5 added there on purpose. The objective was written from the mission's
*disciplined* clause first and matched to gates second, in that order, because a paragraph
written from the gates would have been a list wearing a sentence. The uncited count fell from
forty-one to thirty-two, the largest fall it has recorded, and the map below now carries the
clause under an objective whose own text says what discipline means here: a bound stated before
the spend, and a ceiling nobody declared refused rather than defaulted.

## Claimed by no objective

Every gate that is `unmet`, `partial` or `unit only` and cited by no row above. `deferred` and
`retired` are exempt: one is past the cut line by a recorded decision, the other has no subject
left. The list is recomputed by the test, so a gate cannot quietly join or leave it.

Empty. `GATE-RETRY-5` was its one entry from the day this file was written: a `Retry` middleware
that was tested, correct, and constructed by nothing. Its row said the honest resolution was to
bind it or retire it, and #265 retired it -- what it did was not wanted by any module, which is
why no module bound it, and `CLAUDE.md` is blunt that surface serving no objective is surface to
remove. The table returns the day a gate qualifies; the test recomputes it either way.

## Held, and claimed by no objective

**16 held gates are cited by no objective row.** The number is exact and the test recomputes it:
it may not rise without a row claiming the gate or this sentence saying why it holds for nobody,
and when it falls this sentence is lowered and the fall is the credit. Thirty-nine until Phase 5
built `fan_out` and added two spend-and-time rows to it; forty-one until O13 claimed those two and
seven older ones in Phase 8 (PR-19); thirty-two until PR-20 re-read every row against the
sixteen citations the audit named (#315) and retired `GATE-GUARD-3` with its subject.

What is left is apparatus, and every one of the fifteen is named here so that the next re-read
starts from a list rather than a count. The two ledgers checking themselves: `GATE-TEST-1`,
`GATE-TEST-2`, `GATE-TEST-7`, `GATE-TEST-8` -- consistency of the corpus and of these tables,
serving every row rather than one. `GATE-BODY-2` joins them and is the sixteenth: the
prompt package checking that it ships nothing nobody reads, which serves every prompt rather than
one and which no objective's words reach -- O5 improves the prompts that PRODUCED a recorded
inference, and an orphan produced none. The transport's own discipline, held under `AiInvoker` and
cited by no objective because no objective's sentence is about how a transport behaves:
`GATE-ASYNC-1` to `-4` and `GATE-RETRY-1` to `-3`. The ledger's own shape: `GATE-LEDGER-5` and
`GATE-LEDGER-6`, the epoch and the keys a record may not carry. And the two the plan asked the
owner about, kept by decision at PR-20 rather than retired: `GATE-OUT-4`, the refusal a LOCAL
store gives `compare_and_set`, which is what `park` reads to refuse by name; and `GATE-OUT-1`,
the outcome vocabulary every row's status is spelled in. A gate here that a later objective's own
text turns out to claim moves up; one whose row cannot be claimed by any objective's text after
this reading is what `CLAUDE.md` calls surface to remove, and none of the fifteen is that.

## How the mission maps, and what it cost to say so

The mission arrived after the objectives and was wider than they were. Two of its clauses were
carried by no objective at all, and this section recorded that unresolved rather than letting
whoever noticed it settle it. #240 is where it was settled.

**The decision was two more objectives, not a wider reading of the ten.** Both were available.
Provider and model choice could have been read into O8 — an adopter binding their own provider in
a `lockstep.py` the framework never edits is extension without forking, and the shape does fit.
It was refused because O8 does not say it. Stretching an objective to cover something it does not
state is the inflation this whole apparatus exists to refuse, and it would have made O8's `held`
mean less than it does today: a row is worth something only while its text is the thing being
measured. Collaboration had no such candidate at all.

So the ten became twelve, and in Phase 8 thirteen. The mapping is now one clause to one
objective, which is the property worth having:

| the mission says | the objective |
|---|---|
| the hosted SCM of their choice | O3 |
| the provider(s) and model(s) of their choice | **O11** |
| the process(es) of their choice | O1, O8 |
| the policy of their choice | O6, O8 |
| disciplined and structured | O4, O6, O7, **O13** |
| teams of engineers working together | **O12** |

**O12 arrives `unmet`, and that is the point.** The ratchet's rule is that an objective no gate
carries can never read as `held`, so adding a direction nothing serves does not quietly improve
the census — it makes the census worse, visibly, which is the only honest thing a new objective
with nothing under it can do. `GATE-TEAM-1` is what it is blocked on and #164 is the work. The
alternative was to keep collaboration out of the objectives because it would look bad in the
table, which is the reasoning this file exists to prevent.

**O11 arrives `partial`.** Its carriers were already here and already `held` — an endpoint that
does not match its registration is refused, a restricted repository blocks a model that is not
registered as internal, and no model is priced by a default — they were simply cited by nothing.
That is the state `CLAUDE.md`'s rule reads as *surface to remove*, over surface nobody wanted
removed. The rule was not wrong; the objectives were incomplete, and nothing could show it until
the mission was written down beside them.

**The policy clause needed no new objective and did need a change.** It is carried by O8 and O6,
and what it changed was the weight of `GATE-POLICY-2`: five `Policy` fields an adopter could set
were merged, printed by `ls`, reported in the receipt and enforced by nothing. Before the mission
was written that read as a duplicate surface, since the sandbox and the egress policy cover the
same ground and do enforce. It read differently afterwards — an adopter choosing a policy is doing
the thing the mission exists for, and five of the choices were inert. #263 deleted them, and O6 is
`held`.

## Held, and cited by nothing (as it stood before #273)

Kept as the record of how the count above came to exist. When this section was written the
*claimed by no objective* section listed only the unsettled gates no objective was blocked on,
and a second pool it could not see -- thirty-nine gates `held` and cited by no row -- was
invisible to the very section that existed to find surface serving no direction. #273 counted
that pool and wrote the number into the section above, where the test has recomputed it ever
since; O11's three carriers came out of it, then O13's nine, then the sixteen PR-20 cited. What
was in there was not junk. It was directions nobody had written down yet, and now they are.
