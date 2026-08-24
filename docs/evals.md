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
  --min-pass-rate=0.9
```

It publishes `passed`, `pass_rate` and `pending_rubric` as step outputs, so a later step can gate on
them.

What produces `--outputs` is the agent, and that is what `evals.yml` is for.

---

## The workflow

`lockstep compile` emits one eval suite for the repository, with a group of jobs per agent that has
cases. The shape is deliberately the ordinary one:

```
cases-<agent>   expand the case files into agent inputs, and list them
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
could not be evaluated without evaluating the thing that evaluates it. Without a judge the
deterministic half still runs and rubrics stay undecided, which is the honest answer rather than a
missing one. A judge naming an agent that does not exist is ignored rather than compiled into a job
calling a workflow nobody generated.

The judge reads `{case, rubric, output}` and answers `{"passed": bool, "reason": str}`. A verdict
that cannot be read is **not** a pass: an agent that answered in an unexpected shape has not judged
anything, and treating that as approval is how a suite starts reporting green for the wrong reason.

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

## The rules

- **LNT001** — an agent with no cases at all.
- **LNT007** — a case that is not valid JSON, or has no `input`.
- **LNT008** — a case with an unknown expectation, or one that asserts nothing. An unrecognised key
  is not a stricter case; it is one that never runs.

All three are errors. The migration that introduced them found two cases in `examples/httpbin` that
were asserting exact field values through keys nothing read — which is how `equals` came to exist.
