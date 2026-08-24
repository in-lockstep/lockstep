# What the people who would adopt this need next

Two lists of gaps already exist. [`status.md`](status.md) carries both: **what remains open** is
derived from this design — the things it set out to do and has not finished — and the **fullsend
comparison** was derived from reading another project, and is closed.

This is a third, and its provenance is the reason it is separate. It comes from asking who would
put this in front of their organization, what they arrive wanting, and what would make them put it
down. Nothing here was found by reading the code. Several of these are not features: two are a tag
push, three are documents, and one is the site itself.

The five are the platform engineer who owns the paved road, the security lead who signs off before
agents touch anything real, the engineering leader who funds it, the staff engineer who builds the
pipelines, and the five-person team who will author nothing and wants the loop by Friday.

| | Need | Who | Kind | Status |
|---|---|---|---|---|
| **N1** | Publish the capabilities | everyone | tag push | already open, now gating |
| **N2** | Run the loop on this repository | everyone | self-hosting | already listed, now gating |
| **N3** | A measured time to first value | leader, small team | measurement | new |
| **N4** | A shorter first day | small team | code | new |
| **N5** | A way to tell a working judge from a broken one | small team | code | new |
| **N6** | Aggregation across repositories | platform, security, leader | code | listed, reprioritized |
| **N7** | Migration across capability majors | platform | code | listed |
| **N8** | Outcome metrics, not pipeline metrics | leader | code | new |
| **N9** | Transcript retention as a supported decision | security, author | code | new |
| **N10** | A controls crosswalk | security | document | new |
| **N11** | An inner loop for prompt iteration | author | scope decision | new |
| **N12** | A quick reference for the layer taxonomy | author | document | new |
| **N13** | An entry surface that routes by persona | everyone | the site | new |

---

## The two that gate the rest

### N1 — Publish the capabilities

Every example in this repository pins `capabilities.actions` and `capabilities.exec-image` to forty
zeros, and the compiler prints *this output cannot run as emitted* on every run. That disclosure is
deliberate and a test enforces it.

It is also the answer to the first question every one of the five asks, in different words: who runs
this? Today, nobody has. The release workflows exist and the actions need no second repository — the
work is pushing the first `actions-v*` and `exec-v*` tags and running `lockstep pin`.

**Nothing else on this list can be claimed on a website until this is done**, because the framework
is honest enough to contradict the claim in its own output.

### N2 — Run the loop on this repository

The cheapest possible proof is the one already designed and not built: `/review` on this
repository's own pull requests, with the lenses `docs/self-hosting.md` already names.

A compiler whose own pull requests are reviewed by the pipelines it ships is a demonstration that
costs no case study, no consenting customer, and no new code. It is also the only evidence that will
exist for some time — there are no adopters yet, and the fleet dashboard (N6) is waiting on the same
absence.

---

## Adoption

### N3 — A measured time to first value

Nobody has timed the honest path from an empty repository to a first green run. The quickstart is
five commands, two of which need a network and one of which needs credentials that are not
mentioned until later.

The need is the measurement, not a target. If the honest number is forty minutes, the site must say
forty minutes; a quickstart that implies ten and delivers forty costs more trust than it buys, and
this is the one persona — the five-person team — whose entire evaluation happens inside that window.

### N4 — A shorter first day

`lockstep init --adopt all` writes three files, and then wants `pin`, `fetch`, `compile`, and
`gh aw compile` — across two toolchains, one of which is not this project's.

Each of those commands has a reason, and none of them is optional. But the persona this path was
built for is comparing against *install a GitHub App and be done*, and loses that comparison in the
first ten minutes regardless of what is true afterwards. Whether the answer is a composed command, a
`doctor` that names the next step at every stage, or a first-run mode that does all four and reports
what it did, it is a real decision rather than a cosmetic one.

### N5 — A way to tell a working judge from a broken one

Both scaffolds write `agents/eval-judge.md`, and the framework deliberately ships none — whose prose
decides whether your agents pass is not a compiler's call. That decision is right and should stand.

Its consequence is that the smallest team owns the most consequential prompt in the loop, and is the
least equipped to tune it. A judge that has quietly drifted produces a `pass_rate` that means
nothing, and every number downstream — the baseline, the noise floor, the merge gate — inherits the
drift silently. The scaffolded five deterministic cases are a floor, not a calibration.

What is missing is a report that says the judge is still agreeing with itself: the same cases, the
same answers, run over time. **This is the loop's one unguarded input**, and it is guarded by nothing
right now.

---

## Organization

### N6 — Aggregation across repositories

Everything the framework produces is per repository: one ledger, one surface document, one eval
verdict, one doctor report.

The pitch is *across your organization*. Three personas land on this from different directions — the
platform engineer wants to know what is deployed and how it differs, the security lead wants one
place that says who granted what, and the leader wants a trend that is not one repository's.

It is listed as the fleet dashboard, waiting on real consumer repositories to report on. The persona
lens does not change that ordering — it changes what it is worth, from a reporting nicety to the
thing the organizational claim rests on.

### N7 — Migration across capability majors

Pinning, ejection and the drift gate are in place. What is not is automated migration of overlay
anchors when a capability major moves.

A consumer with overlays keyed on step ids across a dozen repositories does that by hand today, and
the platform engineer is the one who will discover it — after adoption, which is the worst time to
find out what the upgrade path costs.

---

## Evidence

### N8 — Outcome metrics, not pipeline metrics

The ledger records what a *run* did: duration, outcome, credits, dollars, reruns. Every number in it
is about the pipeline.

The persona funding this asks a different question — did the work move faster, and is the code
better. Issue-open to merge, pull-request-open to merge, review latency, and how often a change the
pipelines produced was reverted are all derivable from the trackers the shipped pipelines already
read. None is recorded.

The same honesty rules would apply: absent when unmeasured, no trend under five runs, and a stated
distinction between what was measured and what was derived. This is the largest genuinely new
capability on the list.

### N9 — Transcript retention as a supported decision

`docs/history-and-retro.md` states the limitation plainly: the ledger says which run to look at and
nothing more, transcripts expire with gh-aw's artifacts, and copying them into a branch is a decision
about what a repository holds.

Two personas need it and for opposite reasons. The security lead is asked what the agent actually
said, months later, by somebody who will not accept *it expired*. The pipeline author debugging a
prompt wants the run from last week.

The reason it is not the default is sound — a transcript in a branch is a transcript in everybody's
clone forever. The need is to make it an opt-in the framework supports, with the disclosure attached,
rather than a regret it documents.

### N10 — A controls crosswalk

Every control the security lead needs exists. What does not exist is the sentence that maps each one
to the framework they report against.

Read-only agents, safe outputs, named secrets, computed egress, sandboxed execution, enforced credit
ceilings, a blocking semantic diff on any widening, and a run record that holds no content — these
are already stronger than what most adopters run. They are described in the vocabulary of this
project rather than of SOC 2, NIST AI RMF, or ISO 42001, so today this persona writes the mapping
themselves before they can approve anything.

It is one page, and it is the highest ratio of persuasion to effort on this list.

---

## Authoring

### N11 — An inner loop for prompt iteration

The compile is local and fast. The agent only exists on GitHub. So changing a prompt and finding out
what it did is a push, a wait, and a log — which is the slowest feedback loop in a project whose
entire argument is fast, checkable feedback about prompts.

This is a scope decision rather than a task. The sibling runtime could execute an agent locally, and
this repository deliberately does not depend on it — the same decision that leaves round-trip evals
unbuilt. The need is to decide it deliberately and say so, not to leave the pipeline author to
discover the shape of their day.

### N12 — A quick reference for the layer taxonomy

`docs/layers.md` is the best-argued document here, and someone deciding whether a paragraph is a
guardrail or a skill at 4pm will not read an essay to find out.

The rule is short enough to fit on a card. The essay should stay exactly as it is, and gain a card.

---

## The site

### N13 — An entry surface that routes by persona

There is one door: a README that opens with a compilation diagram. It is accurate, and it answers
the question only one of the five personas is asking.

The documentation is genuinely good and should not be rewritten — it should be reachable. Four
routes into the same depth: governance and the enforcement floor, outcomes and cost, building
pipelines, and the Friday afternoon. This is the whole job of the Pages site, and the reason it is on
a needs list rather than a marketing plan: the framework's problem is not that it says too little.

---

## What the personas ask for that this deliberately will not do

**Become a hosted service.** The small team's comparison is against installing an app, and the answer
to N4 is a shorter first day rather than a product that holds their credentials and runs their agents
on someone else's account. The bring-your-own-engine model is a property, not an omission.

**Support a second forge.** Argued in `status.md`: much of what this framework *is* lives in the
substrate, and the intersection of three forges would be a weaker floor. Every persona who needs this
needs a different tool.

**Ship a fixed set of agents as the product.** Five pipelines ship and are inherited rather than
copied, precisely so that outgrowing them is a rung on a ladder and not a migration.

**Let the retro edit what it proposes.** A pipeline able to rewrite the guardrails that constrain it
has guardrails in name only. The leader who asks for a self-improving loop is asking for the one
thing the design refuses, and the eval gate is the honest version of what they want.
