# Keeping what happened, and acting on it

A dashboard shows today. The question that actually decides whether an agentic SDLC is working is
whether it is getting **better or worse** — and that needs two windows, both of which have to still
exist.

They do not, by default. GitHub artifacts expire, job logs rotate, and gh-aw's transcripts go with
them. A repository that retains nothing cannot tell you whether last month's prompt change helped,
because the runs it would compare are gone.

```yaml
history:
  branch: pipeline-history
```

That is the whole configuration. Every metered run appends one line to a ledger on that branch.

---

## One line per run

```json
{"run_id": "4821", "workflow": "review", "finished": "2026-08-24T10:04:12+00:00",
 "credits": 75.5, "cost_usd": 0.1092, "priced_fraction": 0.69, "attempt": 1,
 "wall_seconds": 210, "busy_seconds": 298, "jobs": 3, "failed": ["aw-tests-reviewer"],
 "agents": {"security-reviewer": {"outcome": "success", "seconds": 210}}}
```

Small enough that ten thousand runs are a few megabytes, plain enough to read with `grep`, and
durable in the only place a repository always has.

`priced_fraction` travels with the cost deliberately. A reader three months from now has no other
way to tell a genuine `cost_usd: 0` from one that means "no rate table was configured".

**No content, ever.** No prompt, completion, diff or source. The same line the metrics draw, for a
sharper reason: this file is as readable as the repository, so a transcript in it is a transcript in
everybody's clone forever. The reasoning stays in gh-aw's own artifacts under that repository's
access controls, and each record carries `run_url` so you can go and get it while it lasts.

That is a real limitation rather than a design flourish. **Transcripts still expire.** If replaying a
run's reasoning six months later matters to you, the ledger tells you which run to look at and
nothing more; copying transcripts into a branch is a decision about what your repository holds, and
it is yours to make deliberately.

### Appended, never rewritten

Several pipelines finish at once. The publishing step re-clones the branch, appends, and pushes; a
push that loses the race is retried against the branch **as it now is**, and appends commute, so
both records survive. A rewrite would keep whichever run pushed last.

Losing a record is a warning, not a failure. The run did its work, and failing a pipeline over
bookkeeping teaches people to turn the bookkeeping off.

---

## Reading it

```bash
pipeline-exec run-history --branch=pipeline-history --days=30 --output=history.json
```

Everything is arithmetic over the lines runs wrote — deliberately, because a retro agent that
computed its own averages would produce different ones each time it ran, and a trend nobody can
reproduce is an anecdote.

Every subject carries a `change` that has to be read before its numbers:

| `change` | Means |
|---|---|
| `compared` | Both windows had enough runs. The deltas are real. |
| `too few runs` | The report does not know. Not a hint to look harder. |
| `new` / `gone` | No baseline. Nothing moved; something started or stopped. |

**A window below five runs reports no trend.** Two runs against two is noise, and noise presented as
a direction is worse than silence — somebody acts on it. And a comparison with no baseline at all
says `compared: false`, because a first window read as a trend makes everything in it look like a
change.

**A metric nothing measured is absent, not zero.** The ledger records what a *run* spent and cannot
attribute it to the agents inside it — gh-aw's usage artifacts carry no name to join on. So
per-agent `mean_credits` is missing from the report rather than reported as `0.0`, which would have
said "unchanged" about a number nobody ever measured.

### Outliers

Runs costing several times the **median** of their own workflow. The median because one runaway drags
a mean far enough to hide the next one; per workflow because a review costing ten times a triage is
two pipelines, not an anomaly.

A cost outlier is rarely about money. It is an agent in a retry loop, a prompt that grew a tool call,
or a context filling with something irrelevant — which is why it is the most actionable line in the
report and the easiest to scroll past.

`totals.reruns` is worth the same attention. A human re-running a pipeline is a human saying it did
not work the first time, and that signal appears nowhere else.

---

## The retro

Shipped as a pipeline. Weekly by default.

```
1. Read what the pipelines did   → builtin: run-history
2. Say what to change            → agent: retro-analyst   → files one issue
```

The split is the point: code computes every number, and the agent does the part arithmetic cannot —
saying what a movement probably means and what to do about it.

### It proposes. It does not edit.

The retro cannot write to an agent, a guardrail, or a workflow. Not because it would be hard, but
because **a pipeline able to rewrite the guardrails that constrain it has guardrails in name only.**

Its output is an issue. If a human agrees, `/implement` can act on it under the same review every
other change gets — which is the framework's whole thesis applied to its own prompts: they are
reviewable artifacts, so changing them goes through review.

Three things its guardrail insists on, each of which is how this kind of report goes bad:

- **A number is not a finding.** "Failure rate up 12 points" is the input. A finding names which
  prompt, which change, and what to do instead — or says the movement is unexplained and what would
  be needed to explain it.
- **Do not propose against noise.** `too few runs` is the report saying it does not know. A
  recommendation built on four runs gets acted on, and then it is wrong.
- **Say when nothing needs changing.** A retrospective that files proposals every week regardless is
  one people stop opening. Weeks where the pipelines behaved are the normal case, and reporting that
  plainly is what makes the other weeks worth reading.

Its eval cases hold it to all three, including a quiet window where the only correct answer is an
empty `findings` list.

---

## Closing the loop: was the change actually an improvement?

The retro says what to try. That is a judgement made by the same kind of thing as the agent being
changed, and it is worth exactly as much as one. The eval suite can say whether the attempt *worked*
— and with the ledger in place it can say it without paying to re-run the old prompt.

On a pull request that touches a prompt layer, the suite runs and the result is compared against
what the previous prompt scored:

```
### `security-reviewer` — regressed

| Metric     | Baseline | Now    | Delta   | Noise  |               |
|------------|----------|--------|---------|--------|---------------|
| pass_rate  | 0.833    | 0.667  | -0.167  | ±0.333 | within noise  |
| mean_score | 4.425    | 4.6    | +0.175  | ±0.4   | within noise  |

**Cases that passed every baseline run and fail now:** `path-traversal`
```

Read that example carefully, because it is the whole argument. The mean score went **up**. Both
aggregates are within the noise. And the change is still a regression, because a case that passed
every single run of the old prompt now fails — which is exactly what an average absorbs.

### The noise floor is the point

Agents are non-deterministic. Run the identical suite against the identical prompt twice and the
scores differ. So a before-and-after comparison, done naively, reports improvements and regressions
that are pure sampling — and a gate built on that is *worse than no gate*, because it blocks good
changes and waves through bad ones with equal confidence.

**A comparison that does not know its own noise floor is an opinion with arithmetic on it.**

Both halves come out of the ledger for free:

- **The baseline** is what the previous prompt scored. The default branch already ran it.
- **The noise floor** is the spread across runs *of that same prompt*. Those runs differed by
  nothing except sampling, which makes their variation the definition of a meaningless delta.

A delta inside the noise is reported as `within noise`. A baseline with fewer than three runs is
reported as **`no noise floor`** — the numbers are still shown, the *direction* is not claimed.

### Which means the schedule is not optional

```yaml
evals:
  baseline: '0 3 * * *'
```

Evals triggered only by a prompt change give each prompt exactly one run, and one run has no spread.
Without this, every comparison correctly reports that it cannot tell a movement from sampling.

These repeats cost credits, which is why it is a decision rather than a default. It is also the
cheapest honest way to know whether your prompt changes are working.

### Flaky cases are named, and decide nothing

A case that passed three baseline runs out of five was never evidence of anything, and its flip
today is not either. It is reported as flaky and excluded from the verdict — which is a defect in
the case rather than a finding about the agent, and worth fixing as one.

This is how a suite stops accumulating gates that fire at random.

### What is fingerprinted

The compiled agent workflow, hashed. That file *is* the prompt: its body, every guardrail, skill and
context, the model, the budget, the turn cap. Two runs share a fingerprint exactly when nothing that
could move behaviour differs — which is the condition under which their difference is sampling.

### The gate

A pull request fails when a case that passed **every** baseline run now fails. Only on a pull
request: a scheduled baseline run compares against the previous prompt every night, and failing it
would report the same regression forever — a red build nobody can clear is one people stop reading.

A candidate's scores never become the baseline. Recording happens off the default branch only, or a
regression would establish itself as normal simply by being merged.

---

## The retro proposes cases, not just prose

A proposal changes a prompt once. A case makes the change checkable and *keeps* it checked — and the
verification above is only as good as the suite, so a failure the suite does not cover is one it
cannot confirm you fixed.

So a finding about an agent getting something wrong carries an `eval_case`: which agent, what it
should assert, and **the `run_url` to derive the input from**.

That last part is a hard constraint, and it is a consequence of what the ledger holds. The retro
reads counts, durations and outcomes — no prompts, no outputs, no diffs. **It has not seen what the
agent said.** So it can say what a case should assert and which run to build it from; it cannot
write the case's `input`, and one it invented would test its imagination rather than the failure.

It proposes no case for a finding about cost or duration. A suite does not measure those.

### The whole loop

1. Runs leave records in the ledger.
2. The retro reads them and proposes a change — and the case that would keep it checked.
3. Somebody, or `/implement`, makes the change.
4. The eval suite runs on the pull request and compares against the previous prompt, past the noise.
5. A case that used to pass and now fails blocks the merge.
6. Merging records a new baseline, and the schedule starts measuring its noise.

Step 4 is the one that makes the rest more than a suggestion.

---

## Customize an inherited agent, and you own its verification

This is the case most repositories reach first, and the one that used to be silent.

Adding a guardrail with `add-guardrails:`, tuning a band, writing an overlay — or, most commonly,
putting one `contexts:` entry in a profile — changes the compiled prompt of the agents involved. A
profile context reaches **every agent this repository compiles**, inherited ones included.

So the agent running here is not the agent its upstream evaluated. Their cases described their
prompt. Nothing describes yours.

`lockstep doctor` says so, once, naming the cause rather than repeating itself per agent:

```
DOC025: 14 inherited agent(s) are customized here and nothing evaluates them:
        fix/bug-analyst, fix/fix-reviewer, fix/fix-writer and 11 more
        this repository adds context:codebase, so their upstreams evaluated a different prompt.
```

The answer is to list the ones worth checking:

```yaml
evals:
  inherited: [review/security-reviewer]
```

Which runs **two sets of cases against your compiled agent**:

- **Upstream's**, fetched with the pipeline, as a regression contract — did what you added stop
  their lens finding what it used to?
- **Yours**, at `evals/<alias>/<agent>/cases/`, testing what the customization was *for*.

A name appearing in both is refused rather than resolved. Letting one win would silently drop an
upstream case, which is the check you least want to lose and least likely to notice going.

Upstream's *scores* never enter your ledger. The fingerprint is your compiled agent, so your
baseline is your own previous prompt and your noise floor is measured on your own runs. Comparing
against upstream's numbers would be comparing across a different prompt, a different profile and
possibly a different judge.

It is per agent rather than a flag, because a repository that adopted five pipelines has no reason
to pay for thirteen suites when it customized one lens.

**One mechanical consequence.** Inherited cases live under `.pipeline/`, which is resolved state
rather than committed source — gitignored, like a virtualenv. So a suite that reads them fetches
first, which needs the compiler, which the executor image deliberately does not carry. That job
therefore runs on the bare runner and installs both. A suite over your own agents reads committed
files and does none of this.

---

## What this does not do

**Replay.** The ledger says which run to look at; it does not reconstruct one. gh-aw retains the
transcripts and `gh aw logs <run-id>` fetches them while they last.

**Attribute cost per agent.** The usage artifacts do not name the agent in a way that can be joined
to a job, so the report says so by leaving the figure out rather than guessing at it.

**Act on its own findings.** By construction, and permanently.

**Verify an inherited agent you have *not* customized.** Its upstream already does, against the
prompt they wrote. Re-running their cases here would pay to re-test their lens.

**Work without a judge.** This is the one dependency worth stating plainly. Every case worth writing
carries a rubric, so a repository with `evals.judge` unset decides nothing at all — the comparison
reports `nothing decided` and the merge gate cannot fire. Both scaffolds write a judge into your
repository for exactly this reason. `docs/evals.md` has the argument.
