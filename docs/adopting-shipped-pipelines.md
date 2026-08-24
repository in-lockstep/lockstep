# Starting with pipelines you did not write

Adopting this framework used to begin with writing a pipeline. That is the wrong first step for a
team whose problem is that they have no AI-SDLC yet, and a strange thing to ask of people who are
adopting an opinion in the first place.

```bash
lockstep init --name acme-app --adopt all
lockstep pin && lockstep fetch && lockstep compile
```

Three authored files — a manifest, a profile, a `.gitignore` — and a compiled, linted pipeline with
its gh-aw lock files beside it.

---

## Inherited, not copied

This is the decision everything else follows from.

A scaffold hands you a copy. From that moment it is your code: an improvement upstream is a merge
conflict, and the framework can never tell whether what you are running is still what it shipped.
Copying is how "getting started quickly" turns into a fork nobody can upgrade.

A shipped pipeline is an **upstream you inherit**, using the machinery that already existed for
inheriting an organization's standards. So the growth path is not a migration:

| You want to | You do | What you gave up |
|---|---|---|
| Run it | `inherits: {triage: lockstep:triage}` | — |
| Change a model or a budget | a `commands:` entry, inside the published band | — |
| Add your own guardrail to its agents | `add-guardrails:` on the same entry | — |
| Change what its steps do | an overlay keyed on a step `id:` | — |
| Write your own pipeline | a file in `commands/`, running beside them | — |

The last column is the point. Nothing is surrendered at any step, and there is no version of this
where you fork the shipped pipeline to make a small change.

---

## Pinned by the compiler

A shipped pipeline has no commit to record. It travels **inside the compiler**, so the version range
that decides which compiler runs is the same one that decides which pipelines you get:

```yaml
capabilities:
  compiler: in-lockstep>=0.1,<1.0
```

`lockstep pin` resolves that to an exact version, which is why `doctor` treats a `lockstep:` upstream
as pinned rather than reporting it the way it reports a local path. There is no second thing to pin,
and recording one would be a number that could disagree with the artifact it claims to describe.

Inheriting a pipeline this compiler does not ship is `DOC023`, and it names the ones that do — a
different compiler version may ship a different set.

---

## What ships

Four pipelines, which between them are a whole loop: an issue arrives, gets placed, gets
implemented, gets reviewed, and what broke gets fixed.

### `triage`

Reads one issue and places it: what kind of work it is, how urgent, what it is missing, and what to
say back to the reporter.

Two steps, deliberately. Everything tracker-specific happens in the first one and comes out in one
shape, so the agent that does the thinking has never heard of Jira or of GitHub — which is also what
lets its eval cases be about triage rather than about an API.

Its guardrail names the two failures that matter, because they pull in opposite directions:

- **Do not invent the requirement.** An issue reported as "export is broken" with no expected output
  has a missing acceptance criterion, and writing a plausible one is worse than reporting the gap —
  the next agent will implement your guess as though a human had asked for it.
- **Do not refuse to commit.** "Needs more information" on an issue that plainly says what is wrong
  is a way of doing nothing while appearing careful.

Four eval cases hold it to that, including a feature request whose reporter wrote "this is really
urgent" and an issue whose acceptance criteria a parser inferred from prose rather than read from a
field somebody filled in.

### `implement`

`/implement 412` reads the issue, interprets the requirements, plans the change, writes the tests,
writes the code, and opens a pull request with the plan rendered on it.

Nothing in it writes to the repository. The agents produce files under an output directory and the
command's `propose:` block turns those into a branch — which is what lets every agent be
`read-all` and why a prompt is never the thing standing between a model and `main`.

The two prompts worth reading are the ones that refuse to guess. `requirements-analyst` puts what the
issue does not decide into `unanswerable` rather than resolving it, because the next agent will
implement a guess with exactly the confidence it implements a requirement and nobody downstream can
tell which was which. `planner` carries those into `open_questions` on the pull request, where a
human sees them.

### `review`

`/review`, or `/review security intent`, posts one review per aspect: security, intent, performance,
test coverage.

Each aspect is an agent rather than a data file. That is what makes a lens testable — its eval cases
hold diffs with planted problems — and what lets a lens carry its own model and budget. It also
means the bot revises rather than repeats: each review carries a marker naming the commit it was
made against, so a second run on an unchanged branch posts nothing, and a run after new commits
edits the review already there.

### `fix`

`/fix 88` reproduces a bug, fixes it, and proves the fix.

**The shape is the argument.** The reproducer is written first and the pipeline *requires it to
fail*; only then is the fix written, and only a passing suite reaches the proposal. A pipeline that
wrote the test and the fix together would produce a test that passes either way and a change nobody
can tell worked.

`bug-analyst` is allowed to fail. Reporting `confidence: low` with a list of what it ruled out is a
real answer that saves the next person hours; a confident wrong cause costs them hours instead.

---

## Either tracker, one shape

`pipeline-exec issue-fetch --source=github|jira` produces the same document from either:

```json
{ "key": "PLAT-412", "summary": "…", "description": "…",
  "acceptance_criteria": ["…"], "criteria_source": "description", "labels": ["…"] }
```

What genuinely differs stays alongside rather than being flattened away — a Jira issue type is a
real thing and is not a GitHub label.

`criteria_source` is the field worth knowing about. GitHub renders acceptance criteria as a heading
or a task list, both of which parse cleanly. Jira stores them in a custom field whose id differs per
instance, so the fetcher reads, in order: the field you configured (`JIRA_CRITERIA_FIELD`), then a
field that *looks* like acceptance criteria, then the description itself. Each fallback is weaker
than the one before, and the output says which one answered — because "this issue has no criteria"
and "we guessed" are different situations, and an agent handed them silently would treat them the
same. The shipped triage agent is told to read that field and say so.

### Writing back

On GitHub an agent's conclusions reach the issue through gh-aw's **safe outputs**: the agent emits a
request, and machinery it does not control validates and performs it. The agent never holds a
credential, which is what lets every agent here stay `read-all`.

Jira has no equivalent, so `jira-update` reproduces the *shape* rather than the mechanism — the
agent writes a JSON file, and a deterministic step is the only thing that writes to the tracker.
Shipped `triage` runs it as a third step, gated on what the fetch left outstanding:

```
3. **Write it back to Jira** → builtin: jira-update
   (if jira in {issue.writeback})
```

The condition asks the fetch step rather than reading the `source` parameter back, because the
question is which tracker actually answered. On GitHub the fetch reports nothing outstanding and the
step is skipped — a second write there would be a duplicate comment.

Three rules are enforced in code, because the safe-output caps that enforce them on the other side
do not exist here:

- **Labels are added, never replaced.** Sending `fields.labels` would replace the list and silently
  delete whatever a person put on the issue. That is the most destructive thing a write-back could
  do and the easiest to do by accident, so the additive `update` verb is the only one used.
- **There is a cap.** A model that decided on forty labels has misunderstood the task, and the place
  to find that out is before the issue, not on it.
- **Nothing transitions an issue.** A transition fires workflow automation, notifications and SLA
  timers. A triage bot should not start those, and a pipeline that wants to can add the step
  deliberately.

Commenting is idempotent: the comment carries a `[lockstep:triage]` marker and a second run revises
the comment it left last time rather than posting beside it. The marker is visible, because a Jira
comment is not HTML and there is nowhere to hide one — and a bot comment that says which bot wrote
it is better manners anyway. It only ever matches its own: editing somebody else's comment is the
kind of thing a bot has to do once to be turned off.

---

## Every shipped pull request names the work it came from

`implement` and `fix` open pull requests, and every commit they make carries the tracker reference as
a git trailer:

```
Implement 412

Issue: #412
```

That is the same line for either tracker — `#412` on GitHub, `PLAT-412` on Jira — and both read it
where it is. GitHub autolinks `#412` anywhere in a commit message; Jira's connector matches
`ABC-123` the same way. A trailer rather than a subject line because it survives a squash and is
parseable by anything that wants to walk the history later.

**Deliberately not a closing keyword.** `Fixes #412` would close the issue the moment the pull
request merges, and whether the work is actually done is a judgement for whoever reviewed it.

Two things make this a requirement rather than a convention:

- **The key comes from the tracker, not from the command line.** `propose.issue-from` names a file
  — the one the fetch step wrote — rather than interpolating the `{issue}` parameter. A run invoked
  with `412`, or with a pasted issue URL, still records the canonical `#412`.
- **A commit that cannot find the key fails.** Not a warning, not a commit without it. A change
  nobody can trace back to the work item that asked for it is precisely what this prevents, and it
  is not worth preventing softly.

A test holds the library to it: every shipped command that opens a pull request must declare
`issue-from`, and the test fails if fewer than two commands were checked, so the rule cannot quietly
stop applying.

This is not imposed on pipelines you write. A dependency bump has no work item, and demanding one
would be a gate with nothing behind it — `issue-from` is there when you want the same guarantee.

---

## Rules a shipped pipeline is held to

**No scripts.** A script here would be untested code arriving in every repository that adopts it:
this repo's own test suite cannot reach into the library, so `lockstep lint`'s "scripts need tests"
rule would be enforced on adopters for code they did not write. Builtins and safe outputs only, and
a test enforces it.

**No `capabilities:` block.** An inherited pipeline runs under the consumer's capabilities. A version
pinned here would be a second opinion about which code runs, held by a repository that is not the
one running it.

**Bands, not fixed values.** Every shipped agent publishes a model and a budget as a band, so the
first change anybody wants to make is a line in `commands:` rather than a fork.

**No context.** The framework cannot know your application. A shipped pipeline may carry agents,
guardrails and steps; it may not carry knowledge of a codebase it has never seen. See
[what goes where](layers.md).

---

## What this is not

It is not a product you install and walk away from. The pipelines are opinions about how work moves
from an issue to a merge, and they are opinions the framework can only hold in general — the parts
that make them good in *your* repository are the ones only you can write, which is what the growth
path above exists for.

What they are is a starting point that is *runnable* on day one and does not have to be abandoned on
day ninety. The parts that make a pipeline good in your repository — which layer was audited, which
handler has produced findings before, how your tests are laid out — are the parts only you can write.
Every shipped agent says so where it would otherwise be guessing, and `docs/layers.md` says where
those facts belong.

The `examples/` directory still exists, and is now the other thing: pipelines to *read* when you are
writing your own, rather than the only way to get one.
