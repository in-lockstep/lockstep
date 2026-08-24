# What an eval case promises

`lockstep lint` has always refused an agent with no eval cases, on the grounds that an agent nobody
evaluates cannot be changed safely. What it checked was that a `.json` file existed in a directory.

The files existed. Every one of them looked like this:

```json
{
  "input": { "…": "…" },
  "expect": { "notes": "Finds the traversal. Cites the file. Says what an attacker does." }
}
```

`notes` was prose addressed to a human who was never going to read it, in a file no program opened.
The gate was a check on the file system, and an agent could be rewritten from scratch without a
single case noticing.

This is the contract that makes a case assert something.

---

## The two halves

The split is the one `enforce:` draws everywhere else in this framework: the part a machine can
settle, and the part that needs judgement.

```json
{
  "input": { "pull_request": { "files": [{ "path": "src/files.py", "patch": "…" }] } },
  "expect": {
    "schema": ["findings"],
    "count":  { "findings": { "min": 1 } },
    "contains": ["src/files.py"],
    "absent": ["TODO"],
    "rubric": "Says what an attacker does with it. Reports nothing about the missing test."
  }
}
```

**The deterministic half** — `schema`, `equals`, `contains`, `absent`, `count` — is applied by
`pipeline-exec eval-grade`. It means the same thing on every run, costs nothing, and needs no model.

| Expectation | Asks |
|---|---|
| `schema: [name, …]` | these are top-level fields of the output |
| `equals: {field: value}` | this field is exactly this value |
| `contains: [text, …]` | this text appears **somewhere** in the output — nested, in an array, in prose |
| `absent: [text, …]` | this text appears nowhere |
| `count: {field: 3}` / `{field: {min: 1, max: 4}}` | this field has this many entries |

**The judged half** — `rubric` — is prose, because *"says what an attacker does"* is not a substring
match, and pretending otherwise produces a test that passes on nonsense.

**A case must assert at least one of them.** A case that asserts nothing passed before anybody wrote
it, and `LNT008` refuses it.

---

## A rubric can ask for a score

Prompt work does not degrade all at once. An agent that used to name the exploit and now only
notices the unvalidated input is worse, and a rubric that answers *passed* reports both as green.

So a rubric may be prose, or a scale:

```json
"rubric": {
  "criteria": "Follows the new query parameter out of the diff and into the code it reaches.",
  "levels": {
    "5": "Traces `format` into export_report, names the join in src/reports/export.py, and says an attacker reads any file back in the response",
    "3": "Says the parameter is unvalidated and reaches a file read, without following it or saying what an attacker gets",
    "1": "Reports nothing, or only that input should be validated"
  },
  "min": 4
}
```

The scale is whatever the levels say it is — two levels or ten, 1-5 or 0-10. Three rules, all of
them there because a number nobody can compare across runs is worth no more than a boolean:

- **The levels are written down, and they travel to the judge.** A judge told only *"score this out
  of 5"* invents the scale on every call, and two runs of the same suite are then not comparable —
  which was the entire reason for scoring instead of deciding.
- **`min` is required.** It is the score this case has to reach. Without one the grader would be
  inventing the threshold it reports against.
- **A level says what earns it.** `"3": "okay"` is prose pretending to be a scale.

`min` decides the case; the *mean* decides the suite:

```yaml
evals:
  judge: eval-judge
  min-score: 4.0
```

That is the gate a pass rate cannot express. Every case can clear its own `min` while the suite
slides from 4.8 to 4.1, and `--min-pass-rate` reports 100% throughout. The grade report carries the
distribution as well as the mean, because four 5s and a 1 average the same as five 4.2s and are a
different agent:

```json
{ "pass_rate": 1.0, "mean_score": 4.2, "score_counts": {"1": 1, "5": 4}, "scores": {…} }
```

A floor set on a suite where nothing was scored decides nothing, and `eval-grade` says so rather
than reporting a pass the floor had no part in.

**Comparing this run to the last one is built, and it needs a noise floor to mean anything.** With
`history.branch` set, each suite run is recorded against a fingerprint of the prompt it scored, and
a pull request that changes a prompt is compared against what the previous one scored — past the
spread measured across repeated runs of that same prompt. `docs/history-and-retro.md` has it.

---

## A case can carry a repository

A case carries `input`. An agent asked to review code was therefore being handed a JSON object and
asked to reason about a patch fragment — which tests its ability to reason about patch fragments.

A case can name a fixture instead:

```
evals/security-reviewer/
  cases/format-from-the-query.json      "fixture": "format-from-the-query"
  fixtures/format-from-the-query/
    src/repo.py
    src/reports/routes.py               ← the file the diff touches
    src/reports/export.py               ← the file that makes it a vulnerability
```

Before the agent runs, that tree is copied to its own directory and the path is written into the
input the agent is handed, as `repo`. **A name, not a path**: a case that could write `../../..`
would be a way to hand an agent the repository running the eval. Its own directory per case, because
two cases sharing one checkout would let the first case's run change what the second one sees, and
from scratch each time, because a file left over from an earlier run is a fixture nobody wrote.

The example above is the shape worth copying. The diff adds a `?format=` query parameter and passes
it along; whether that is a finding depends entirely on `export.py`, which the diff never touches.
Its deterministic half is one line:

```json
"contains": ["src/reports/export.py"]
```

A review that names a file it was never shown read the repository. A patch-only reviewer cannot pass
that by luck.

**The agent has to be told this is how it works.** In a real run the checkout is at the workspace
root and `repo` is `"."`; in an eval it is somewhere under `outputs/`. An agent that read its working
directory instead would review whatever repository the eval suite happened to be running in — so the
pipeline that feeds it in production sets `repo` too, and the eval sends the shape production sends.
`examples/pr-review` does both.

`LNT009` refuses a fixture that is not there, one that is empty, a name that is a path, and a case
that sets `input.repo` itself.

---

## Why `contains` searches everything

An agent may legitimately put a file path in a nested finding, in an array, or in a sentence. A case
that had to know the output's shape would be testing the shape rather than the answer, and would
break every time an agent restructured its report without changing what it said. So `contains`
searches the serialized output, case-insensitively. `schema` and `equals` are there for when the
shape *is* the point.

---

## What a pass means, and what it does not

This is the part worth being careful about.

`eval-grade` has no model. It cannot judge a rubric, and it does not pretend to:

```json
{ "case": "path-traversal", "deterministic_passed": true, "rubric_pending": true, "passed": false }
```

A case carrying a rubric is **never reported as passed** by the grader. It is reported as decided on
its deterministic half and awaiting judgement on the rest. The roll-up counts those cases separately
rather than as passes:

```json
{ "total": 4, "passed": 2, "failed": [], "pending_rubric": ["path-traversal", "nothing-to-find"] }
```

A suite that reported `4/4` while half of it had never been judged would be the reassuring number
this whole contract exists to remove. `--min-pass-rate` applies to the cases that were actually
decided.

**A missing output is a failure, not a skip.** If a case has no answer file, the agent was asked and
did not answer, which is exactly the regression a suite is for.

---

## Running it

```bash
pipeline-exec eval-grade \
  --cases=evals/security-reviewer/cases \
  --outputs=outputs/evals/security-reviewer \
  --output=outputs/evals/security-reviewer.json \
  --agent=security-reviewer \
  --min-pass-rate=0.9 \
  --min-score=4.0
```

It publishes `passed`, `pass_rate`, `pending_rubric` and `mean_score` as step outputs, so a later
step can gate on them.

What produces `--outputs` is the agent, and that is what `evals.yml` is for.

---

## The workflow

`lockstep compile` emits one eval suite for the repository, with a group of jobs per agent that has
cases. The shape is deliberately the ordinary one:

```
cases-<agent>   expand the cases into agent inputs and fixture trees, and list them
run-<agent>     the agent itself, once per case, as a matrix
prep-<agent>    pair each rubric with the answer it is about        (only with a judge)
judge-<agent>   judge those pairs, once per rubric                  (only with a judge)
grade-<agent>   apply the checks, fold in the verdicts, gate
```

`run-<agent>` calls **the same compiled workflow the pipeline calls** —
`./.github/workflows/aw-<agent>.lock.yml`, with `input_path` and `output_path`. An eval is not a
special way of running an agent; it is the ordinary way, with the input coming from a case file
instead of an earlier step. A suite that ran the agent some other way would be evidence about the
harness.

Two details worth reading twice:

- **It never runs on every push.** A suite spends credits. It is dispatched, or it runs when a
  prompt layer changes — `agents/`, `guardrails/`, `skills/`, `contexts/`, `evals/` — which is
  exactly what an eval exists to gate, and the only thing that can move an agent's behaviour. Set
  `evals.on-prompt-change: false` to leave only the dispatch.
- **`grade` runs on `!cancelled()`**, not on success. A case whose agent run failed is a case the
  suite should report on, not one that takes the report down with it.

### The judge

Rubrics are judged by an agent **your pipeline declares**, not one the framework ships:

```yaml
evals:
  judge: eval-judge        # an agent in this pipeline
  min-pass-rate: 0.9
```

A framework-provided prompt deciding whether your agents pass is a strong opinion to impose, and it
could not be evaluated without evaluating the thing that evaluates it. So the framework ships none —
but **both scaffolds write one into your repository**, because without it the loop is decorative.

That is worth being blunt about. Every case worth writing carries a rubric; the deterministic half
cannot settle *"says what an attacker does with it"*. A rubric nobody judges is reported as
undecided, and a suite of undecided cases decides **nothing** — so the comparison has no evidence,
and the merge gate can never fire. `pipeline-exec eval-compare` reports that state as
`nothing decided` rather than as a verdict, and `pass_rate` comes back `null` rather than a
fabricated 1.0.

The judge is an agent like any other: it needs eval cases, and it gets its workflow compiled because
the eval suite is a caller even when no command runs it. Its own cases are purely deterministic —
a judge answers `{"passed": bool}` or `{"score": int}`, which `equals` settles exactly — so the one
agent whose cases would otherwise need a judge is the one agent that does not, and the recursion
never starts.

A judge naming an agent this compile produces no workflow for is a **compile error**. It used to be
ignored, which is the worst of the options: a typo silently left the whole suite deciding nothing
while everything kept reporting.

The judge reads `{case, rubric, scored, output}` and answers `{"passed": bool, "reason": str}` — or,
for a scored rubric, reads the `levels`, `scale` and `min_score` as well and answers
`{"score": int, "reason": str}`.

A verdict that cannot be read is **not** a pass: an agent that answered in an unexpected shape has
not judged anything, and treating that as approval is how a suite starts reporting green for the
wrong reason. That covers a boolean answer to a scored rubric — `true` is an `int` in Python and
would otherwise become a silent 1 — and a score outside the scale it was given.

Only cases that carry a rubric *and* produced an answer reach the judge. A case with no answer has
already failed for that reason, and judging it would spend a model call to be told so again.

---

## What still waits on a runner

Nothing has executed. The suite compiles, drift-checks and is covered by the gate like every other
workflow here, and it runs the first time an agent runs at all — which needs the capabilities
published. `docs/status.md` tracks that.

---

## Two copies of one contract

`lockstep` validates cases at lint time; `pipeline-exec` grades them. The compiler must not import
the runtime — a generated repository installs the runtime and never the compiler — so the list of
valid expectations exists in both, and `tests/test_contract.py` holds them to each other. A key that
lint accepts and the grader ignores would be an expectation that passes review and never runs.

---

## An outage is not a regression

A case with no answer file is a failure — the agent was asked and did not answer. But it is recorded
as `answered: false`, separately from a case the agent answered *wrongly*, and the comparison
excludes it from every verdict.

Without that distinction the chain is airtight and wrong: a provider outage or a rate limit produces
a missing answer, which counts as decided, which reads as a case that always passed and now fails,
which blocks the merge. A gate that fires on somebody else's downtime is one people route around.

---

## The rules

- **LNT001** — an agent with no cases at all.
- **LNT007** — a case that is not valid JSON, or has no `input`.
- **LNT008** — a case with an unknown expectation, or one that asserts nothing. An unrecognised key
  is not a stricter case; it is one that never runs. Also a rubric a judge could not apply the same
  way twice: no levels, one level, a level that says nothing about what earns it, or a `min` outside
  the scale.
- **LNT009** — a fixture that is not there, is empty, or is written as a path rather than a name;
  and a case that sets `input.repo`, which is where the fixture's path goes.

All four are errors. The migration that introduced them found two cases in `examples/httpbin` that
were asserting exact field values through keys nothing read — which is how `equals` came to exist.
