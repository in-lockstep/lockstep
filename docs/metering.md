# What a run did, and what it cost

A pipeline's bill is not something this framework measures. gh-aw measures it: every agent run
uploads a usage artifact carrying the credits it spent, the tokens behind them, and the model that
spent them. Nothing read it.

`otel:` adds one job that does — and while it is there, it answers the four questions that matter
more often than cost.

```yaml
otel:
  export: artifact          # artifact | endpoint | both
  endpoint: ${OTEL_ENDPOINT}
  service-name: pr-review
  pricing:
    claude-sonnet-4-6: 0.0021    # dollars per credit
    claude-haiku-4-5: 0.0004
```

---

## Five questions, not one

Cost is the question people ask last and the only one credits answer. The others are about
outcomes, timings and rates, and every one of them is observable without new instrumentation — the
Actions jobs API already knows when each job was created, started and finished, and how it ended.

| Question | What answers it |
|---|---|
| Is it working? | `lockstep.run.jobs` by outcome, `lockstep.run.pickup` |
| What just happened? | `lockstep.job.duration` and `lockstep.job.start_delay`, per job, attributed to the agent |
| Is it getting better or worse? | the same metrics over time, plus `lockstep.run.credits` per agent |
| Where should effort go? | outcome and duration split by `gen_ai.agent.name` |
| What does it cost? | `lockstep.run.cost.usd`, split by model |

The full set:

```
lockstep.run.credits              {credit}   measured by gh-aw
lockstep.run.tokens               {token}
lockstep.run.cost.usd             USD        derived from the rate table
lockstep.run.priced_fraction      1          how much of the run the table could price
lockstep.run.credits.by_model     {credit}   tagged gen_ai.request.model, gen_ai.system
lockstep.run.tokens.by_model      {token}
lockstep.run.cost.usd.by_model    USD
lockstep.run.duration             s          wall clock
lockstep.run.busy                 s          runner time, summed across jobs
lockstep.run.pickup               s          delay before anything started
lockstep.run.attempt              1          above 1, somebody re-ran a failure
lockstep.run.jobs                 {job}      by outcome
lockstep.job.duration             s          per job, tagged with the agent
lockstep.job.start_delay          s          per job
```

Per-model and per-agent points carry the [OTEL GenAI semantic conventions][semconv] —
`gen_ai.request.model`, `gen_ai.system`, `gen_ai.agent.name`, `gen_ai.operation.name` — so a backend
that already understands agent workloads gives you dashboards without being taught to. An
unrecognised model family gets **no** `gen_ai.system` rather than a guessed one.

[semconv]: https://opentelemetry.io/docs/specs/semconv/gen-ai/

---

## Wall clock is not the sum of a fan-out

Twelve reviewers finishing in four minutes took four minutes. `lockstep.run.duration` is the wall
clock; `lockstep.run.busy` is the runner time you are billed for. Reporting forty-eight minutes as
the duration would describe a pipeline nobody ran.

Similarly, **`start_delay` is not queue time.** The Actions API stamps every job's `created_at` when
the *run* is created, so a job that started four minutes in because it waited for the reviewer
before it has a four-minute "delay" that says nothing about runner availability. Only the first job
to start carries that signal, and it is reported separately as `lockstep.run.pickup`.

---

## Credits are measured. Dollars are derived.

The credit figure comes from the substrate. The dollar figure is that figure multiplied by a rate
somebody wrote in a manifest — a *statement about price* rather than an observation, and only as
current as the table.

`pricing` is longest-prefix matched, so `claude-sonnet-4-6` prices `claude-sonnet-4-6-20260101` too.
A table that had to name every dated snapshot would silently stop pricing things the day a provider
published one.

**A model with no rate is unpriced, never free.** The failure this exists to avoid is a report that
says $0.00 because it did not recognise a model name, so the total says how much of itself it could
not price:

> **$0.1092 covers 69% of this run's credits.** 31% went to models with no rate in the table:
> `gpt-5-mini`. The cost above is a floor, not a total.

The rate table is compiled into the workflow rather than read from a data file, so changing what a
credit costs shows up in a diff somebody reviews.

---

## Reconciliation, because the shape is not a contract

gh-aw's usage artifact belongs to a tool this repository pins but does not own. The meter takes
what it recognises rather than asserting a schema, and it has to decide which objects are
measurements and which are totals *of* those measurements — summing both would double a bill.

That inference is the part most likely to be wrong after an upstream change, and it fails quietly: a
double count still produces a confident number. So the totals gh-aw wrote are checked against the
totals we computed, per file, and a disagreement is **reported rather than resolved** — which of the
two is right is not something the meter can know:

> **These credits do not reconcile.** gh-aw reported 63.5 for the files that published a total;
> adding up the records in those same files gives 75.5.

Only files that published a total take part. A file with no roll-up has nothing to be checked
against, and counting its records against another file's total is how a perfectly healthy
multi-agent run starts reporting that it does not add up.

Finding nothing is reported as **nothing found**, never as a cost of zero. `pipeline-exec meter
--explain` prints every file read and every number matched, which is what you check once against a
real run.

---

## No content leaves the run

These metrics carry no prompt, completion, diff, or source — only counts, durations, model names and
outcomes. That is what makes exporting them to a shared or vendor backend a decision nobody has to
think hard about. Reasoning stays in gh-aw's own artifacts, under that repository's access controls,
and the meter does not copy it anywhere.

---

## When it runs

The metering job waits on the agent jobs whose spending it collects, and carries `if: !cancelled()`.

That condition is load-bearing. A job whose `if:` does not mention `cancelled()` gets the
skip-tolerant guard folded into it — `!failure() && !cancelled() && …` — and an `always()` ANDed
with `!failure()` is just `!failure()`. Written the obvious way, the meter would have skipped every
run that failed, which is precisely the set of runs whose cost somebody later wants to look up.

It also carries `continue-on-error`. Metering is bookkeeping about work that has already finished; a
collector being down is not a reason to turn a green pipeline red. The export step says so on its
own output and the run carries on.

---

## What this does not do

**Compare this run to last month's.** The metrics are per run. Trends, anomalies ("this review cost
ten times the median") and cost-per-outcome are what a backend is for — or what run history would
be, and `docs/status.md` tracks that as a separate item.

**Bound spending.** Metering reports; it does not gate. The enforced budgets are credits, checked
before an agent starts: `budgets.per_run_ai_credits` for one execution and
`budgets.per_agent_daily_ai_credits` for a day of them, both compiled into gh-aw and both cappable
from an upstream. See [inheriting](inheriting.md). Dollars are the reported half of that split, for
the same reason a guardrail has an enforced half and an advisory one.
