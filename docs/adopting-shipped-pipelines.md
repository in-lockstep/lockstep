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

**Write-back is GitHub-only today.** The triage agent posts its comment and labels through gh-aw's
safe outputs, which are a GitHub mechanism. Reading from Jira works; writing back to Jira needs a
step you add. That is a real gap rather than a design position — `docs/status.md` carries it.

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

Nor is `triage` alone an AI-SDLC. Implement, review and fix are the rest of it, and today those
exist as `examples/` you copy rather than as pipelines you inherit. `docs/status.md` tracks that.
