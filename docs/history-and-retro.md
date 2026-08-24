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

## What this does not do

**Replay.** The ledger says which run to look at; it does not reconstruct one. gh-aw retains the
transcripts and `gh aw logs <run-id>` fetches them while they last.

**Attribute cost per agent.** The usage artifacts do not name the agent in a way that can be joined
to a job, so the report says so by leaving the figure out rather than guessing at it.

**Act on its own findings.** By construction, and permanently.
