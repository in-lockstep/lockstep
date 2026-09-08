# What the people who would adopt this need next

Derived from asking who would put this in front of an organization, what they arrive wanting, and
what would make them put it down — rather than from reading the code. Five people: the platform
engineer who owns the paved road, the security lead who signs off before agents touch anything
real, the engineering leader who funds it, the staff engineer who builds the pipelines, and the
small team who will author nothing and wants the loop working by Friday.

The list predates the pivot from a compiler to a framework. It is kept because most of it survived
that change unaltered — which is itself the useful signal: the needs were about adoption, and
adoption did not care which architecture served it.

| | Need | Who | Status |
|---|---|---|---|
| **N1** | Publish the capabilities | everyone | **moot** — the composite actions and the exec image belonged to the compiler and went with it. What replaces them is a CLI and a twelve-line trampoline. |
| **N2** | Run the loop on this repository | everyone | **closed in principle, thin in fact.** The framework validates and tests itself in-process, `review` runs the whole path offline, and live model calls have now happened — `/implement` and `/fix` have both run from a comment, and the ledger carries their records. What is still missing is volume: a handful of runs is not a merge rate, and N3 stays open. |
| **N3** | A measured time to first value | leader, small team | open. `init` writes two files; nobody has timed the path from that to a first useful review. |
| **N4** | A shorter first day | small team | **improved.** The first day used to be a spec tree of seven directories. It is now `init`, then editing one Python file. |
| **N5** | A way to tell a working judge from a broken one | small team | **closed for judged rubrics.** `eval harvest` builds cases from real recordings and `eval run` settles their deterministic half against the answer that really came back. `eval run --judge` puts every rubric to the bound judge as a recorded run under a ceiling, keeps each verdict beside its case with the level, the reason and what it quoted, and replays it until the rubric or the answer changes -- so a judge's work can be read, and a broken one is one whose reasons do not match its levels. A rubric nobody judged still says `outstanding`, and the judge of last resort is still the person on the pull request. |
| **N6** | Aggregation across repositories | platform, security, leader | deferred to post-1.0 with workspaces. Still no demand evidence. |
| **N7** | Migration across capability majors | platform | **closed by decision.** No importer; 0.x specs are frozen and `in-lockstep==0.1.0` stays installable. Defensible only because there were no adopters — see ADR 0001. |
| **N8** | Outcome metrics, not pipeline metrics | leader | partially met. Every outcome carries a `Cost`, and `decided` is a metric dimension so a run that decided nothing cannot read as a success. Recordings now become cases (`eval harvest`), which is the half of the eval loop that was missing; cost-per-merged-change still needs the paid half. |
| **N9** | Transcript retention as a supported decision | security, author | **done.** Both tiers are decided, and each is stated where it takes effect rather than in a policy document. **Expiring** (`GATE-RECORD-1`): a CI recording lives in the runner's temporary directory and is destroyed with the runner; the artifact carrying what was harvested from it declares its own retention — 30 days here, 14 in what `init` scaffolds — rather than inheriting a vendor default nobody chose; and `doctor` says what is on a laptop and whether git would commit it (DOC168). **Durable** (`GATE-EVIDENCE-1`): promotion into `evidence/cases/` is publication, not storage. A case holds a whole composed prompt and a whole diff, `git rm` is not deletion, and a promoted case stays reachable in every clone forever — so it is bounded by a cap of 24 per family, gated by a pull request a person reads, and closed to this repository's own agents, which is enforced by a tier-1 `PathPolicy` entry rather than asserted in prose -- the first draft asserted it while `ChangeGuard` returned `None` for the path. `evidence/README.md` says all of that in the place somebody is standing when they are about to do it. The honest caveat, recorded rather than smoothed over: the durable answer is a bound and a review, not an erasure story. There is no erasure story for a published git object, and implying one would be the failure this table exists to prevent. |
| **N10** | A controls crosswalk | security | **done** — [`controls-crosswalk.md`](controls-crosswalk.md), and it says which control was lost rather than replaced. |
| **N11** | An inner loop for prompt iteration | author | **done** — `show-prompt` renders the composition offline with no key, and `--offline` replays a cassette. |
| **N12** | A quick reference for the layer taxonomy | author | **partly moot.** The compiler's three prompt layers are now guardrails, body, skills and contexts, composed in `Prompt.system` and frozen in the characterization corpus. |
| **N13** | An entry surface that routes by persona | everyone | **done** — the site at https://in-lockstep.github.io/lockstep/ routes by accountability rather than by feature: five roles, each with what the framework changed for them, plus a walkthrough that adopts the framework into a sample library and moves one change through the whole lifecycle. Terminal output on it is captured verbatim; quotes are attributed by role. |
| **N14** | Publish the Python distributions | everyone | **done** for 0.1.x. 1.0 reuses the name for a different product, which ADR 0001 records as a deliberate and slightly uncomfortable decision. |

## The one that matters most

N3, now. N2 was this section's subject for as long as nothing had run, and its row records what
changed. What the row does not record is how thin the record is, so here it is, read off the
history branch on 2026-09-06 with `in-lockstep report` and then record by record:

- **Eight runs against a real model**, all from a comment: seven `/implement` and one `/fix`
  between 2026-08-30 and 2026-09-02, $275 and 19.4 million tokens across four tickets, five of the
  runs and $210 of the money on one ticket. Two succeeded. Three were stopped by a ceiling or a
  gate, which is a control working; three failed or errored, which is the loop saying so.
- **Thirty-seven review records that are not model calls.** Every one is the shipped recording
  replayed offline on a laptop — 5,804 tokens and 16 milliseconds each, identical — appended
  because a run records by default and `review --offline` is what a contributor runs to check a
  change end to end. The report files them as replays. They show the path works, not that a model
  was asked.
- **The reviews a model was asked for were not on the branch.** Every pull request pays for a
  `review`, fifty runs of the `lockstep` workflow in the two days before this was written, and
  each wrote its record into a bundle that left the job as an artifact with a 30-day retention,
  because a read-only job cannot publish to the ledger. Nobody had reconciled one when this was
  filed as #294. Since #294 a `publish` job pushes each review's record as it finishes, a
  scheduled sweep absorbs what that misses, and `report --scm` counts what is still outstanding;
  the first sweep is what brings the 30-day window's worth onto the branch, and the count is what
  says whether it did. The volume N3 asks about exists, and the ledger this document leans on can
  now see it.
- **And the branch flagged itself with no way to answer.** Every report opened with `TAMPERED`,
  because the 2026-09-02 reconcile carried a laptop's fixed-run-id record over the branch's own
  record of the same id, and nothing could acknowledge that, so the flag was read past for four
  days. Filed as #295, which gave the flag an answer: `history --acknowledge` appends a note by
  name, and `report` prints the note where the alarm was. The acknowledgement of that rewrite is a
  person's act, and the report opens with the flag until somebody makes it.

So the open question is not whether anything has run. It is whether enough has run, somewhere the
report can read, to say what a first review is worth and how long the road to it is — and nobody
has timed that road (N3) or priced a merged change (N8). ADR 0001 weighted the abandon criteria
for exactly this: a time tripwire and a cost tripwire can say a build has stalled or is uneconomic,
and only volume against real changes can say whether the output was worth reading. Eight paid runs
on the branch and fifty in artifacts are not volume yet, and the second number is the one to go
and get.
