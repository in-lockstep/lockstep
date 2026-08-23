# Adding a pipeline to a repository you already have

Every other guide here starts from an empty directory. This one starts from a repository that
already exists — with its own source, its own tests, and a GitHub Actions workflow that runs
`make ci` — and adds the [`/review` chat-ops pipeline](reviewing-pull-requests.md) to it without
disturbing any of that.

It then covers the question that decides whether any of this is usable on an open-source project:
**what happens when the pull request comes from a fork.**

---

## The repository we're starting from

```
├── .github/workflows/ci.yml     name: CI · on: [push, pull_request] · run: make ci
├── Makefile                     ci: lint test
├── src/app.py
└── tests/test_app.py
```

Ordinary. The CI workflow runs `make ci`, and the Makefile decides what that means — which is a good
arrangement, because it keeps CI reproducible locally, and it is the arrangement the pipeline is
going to leave completely alone.

---

## Part 1 — Where the pipeline goes

A pipeline is defined by directories: `commands/`, `agents/`, `guardrails/`, `skills/`, `contexts/`,
`profiles/`, `mcp/`, plus `scripts/`, `tests/` and `evals/`. In a repository that exists *for* the
pipeline those sit at the root. In a repository that already has source of its own, adding eight
top-level directories — two of which are called `scripts` and `tests` — is not acceptable.

So: **if `.lockstep/` exists, that is where the pipeline lives.**

```
├── .github/workflows/
│   ├── ci.yml                   yours, untouched
│   ├── review.yml               generated
│   ├── pipeline-ci.yml          generated
│   └── aw-security-reviewer.md  generated, one per reviewing agent
├── .lockstep/                   the entire pipeline, in one directory
│   ├── pipeline.yaml
│   ├── commands/  agents/  guardrails/  skills/  contexts/  profiles/
│   ├── scripts/  tests/  evals/  extensions/  overlays/
│   └── .pipeline/               pins and provenance
├── Makefile                     yours
├── src/                         yours
└── tests/                       yours
```

There is no configuration key for this. The directory either exists or it does not:

```python
def find_home(root: Path) -> tuple[Path, bool]:
    """Where this repository keeps its pipeline.

    `.lockstep/` when it is there, the repository root otherwise. A repository that exists for the
    pipeline can keep everything at the root; one that already has source of its own puts the whole
    pipeline in one directory, and nothing about the spec changes either way.
    """
```

**Nothing about the spec changes.** The same `commands/review.md` works in either layout. What
changes is what the compiler *emits*, because a generated workflow runs at the repository root and
has to name things from there:

```markdown
7. **Post one review per aspect** → script: scripts/post-reviews.sh
```

compiles to

```
bash .lockstep/scripts/post-reviews.sh --pr="…" --reviews=outputs/reviews …
```

Script paths are prefixed automatically, and so are the step definitions the cache hashes. For paths
the compiler cannot recognise as paths — a data directory, a template you pass to a script — write
`{lockstep}`, which expands to `.lockstep` or to `.` depending on the layout, so one spec serves
both.

### Adopting the example

```bash
mkdir .lockstep
cp -R path/to/examples/pr-review/. .lockstep/
rm -rf .lockstep/.github          # the example's own compiled output, not yours

lockstep lint      # no findings
lockstep compile
```

No edits. The pipeline reads no directories of its own at runtime, so nothing in the spec has to be
told where it now lives.

```
review: 7 steps -> 7 jobs · 4 agentic, 3 deterministic, 1 cacheable
wrote 14 files
```

Your repository root gained exactly one directory.

---

## Part 2 — Living alongside CI you already have

The generated workflows land in `.github/workflows/` beside `ci.yml`, and never touch it. They are
separate workflows with separate triggers:

| Workflow | Fires on | Does |
|---|---|---|
| `ci.yml` (yours) | push, pull_request | `make ci` |
| `review.yml` (generated) | `/review` in a comment | reviews the pull request |
| `pipeline-ci.yml` (generated) | changes under `.lockstep/**` | checks the pipeline itself |

`pipeline-ci.yml` only watches the pipeline's own files:

```yaml
on:
  pull_request:
    paths:
      - .lockstep/commands/**
      - .lockstep/agents/**
      - .lockstep/guardrails/**
      …
```

so an ordinary source change never triggers it, and a pipeline change never triggers a full CI run
you did not need. Its script-test job runs the *pipeline's* tests, not yours:

```
if [ -d .lockstep/tests ]; then uv run pytest .lockstep/tests -q; else echo "no .lockstep/tests directory"; fi
```

### If you would rather have one entry point

Some teams want `make ci` to mean *everything*, so a contributor can check the whole repository with
one command. Add the pipeline's checks to the Makefile:

```make
.PHONY: ci lint test pipeline

ci: lint test pipeline

lint:
	ruff check src

test:
	pytest tests

# The pipeline's own checks. `--check` recompiles and byte-compares, so a spec edit committed without
# recompiling fails here rather than in a scheduled run.
pipeline:
	lockstep lint
	lockstep doctor
	lockstep compile --check
	@[ -d .lockstep/tests ] && pytest .lockstep/tests -q || true
```

Then delete the generated `pipeline-ci.yml`:

```bash
lockstep eject .github/workflows/pipeline-ci.yml
rm .github/workflows/pipeline-ci.yml
```

Ejecting first matters. It tells the compiler you have taken ownership, so it will not regenerate the
file and the drift gate will not complain that it is missing.

**Which arrangement is better?** Separate workflows give you faster feedback — a spec change runs four
small jobs instead of your whole suite. One `make ci` gives you one thing to remember and one thing
to run locally. Pick the one that matches how your contributors already work; both are supported and
neither is clever.

### Two things that will bite

**`uv` or your toolchain.** The generated workflows install the pinned compiler with `uv tool
install`. If your repository standardises on something else, that is fine — the *generated* workflows
are the only place `uv` appears, and the pipeline's own scripts run inside the executor image, not
your project's environment.

**Your `.gitignore`.** Add `.lockstep/outputs/` — or wherever your `output_dir` points — or your first
local `lockstep compile` will offer to commit a pile of run artifacts.

---

## Part 3 — Pull requests from forks

This is where most "AI reviews your PR" setups quietly do not work, or work in a way you would not
want if you looked closely.

### Why the obvious approach fails

The instinct is `on: pull_request`. For a fork, GitHub deliberately makes that nearly useless:

| | `pull_request` from a fork | `pull_request_target` | `issue_comment` |
|---|---|---|---|
| Token | **read-only** | full write | full write |
| Secrets | **none** | available | available |
| Code checked out | the fork's | the **base** repo's | the base repo's |
| Workflow file used | the **base** repo's | the base repo's | the base repo's |
| Can post a review | **no** | yes | yes |

A `pull_request` run from a fork cannot post a review, cannot read your model credentials, and cannot
write anything. That is correct and deliberate: the code in that pull request is untrusted, and
GitHub is refusing to hand it anything.

`pull_request_target` is the usual workaround and it is **the single most dangerous pattern in GitHub
Actions**. It runs with full secrets in the base repository's context. The moment you check out the
fork's code and execute any of it — a build, a test, an install script, a linter that loads project
config — you have handed a stranger your secrets. Do not do this.

### Why chat-ops does work

`issue_comment` fires on the **base repository**, not on the fork. A comment on a pull request is an
event in *your* repository, so:

- The workflow that runs is the one on **your default branch**, not the one in the pull request. A
  fork cannot modify the workflow that reviews it.
- Secrets and a write token are available, because the trigger is a person commenting in your
  repository, not code arriving from outside it.
- Nothing from the fork is executed. The pipeline reads the diff through the API and hands it to a
  model. There is no build, no install, no test run.

That last point is the important one, and it is a property of *this* pipeline rather than of
chat-ops generally. `/review` reads a diff and writes text. It never checks out or runs fork code:

```python
pull = api(f"repos/{repo}/pulls/{pr}")
files = api(f"repos/{repo}/pulls/{pr}/files", paginate=True)
```

A pipeline that *did* need to run fork code — the [bug-fix](extending.md) or
[implement-issue](implementing-issues.md) pipelines, which build and test — would not be safe on fork
pull requests, and should be restricted to branches in your own repository. The distinction is not
"is it chat-ops" but **does it execute code it did not write**.

### The gate does the rest

An event firing in your repository is only half the problem. Anyone who can comment can fire it, and
on a public repository that is everyone on the internet. So the gate checks two independent things:

```bash
# The permission API answers "what can this account do here". On a public repository it is a weak
# signal: a passer-by can read it, so a `read` permission says nothing about trust.
# The author association, which GitHub puts in the event payload, answers the question that actually
# matters — is this person part of this project.
permission=$(gh api "repos/${GITHUB_REPOSITORY}/collaborators/${actor}/permission" --jq '.permission')
association=$(jq -r '.comment.author_association // "NONE"' "$GITHUB_EVENT_PATH")
…
[ "$by_permission" = "true" ] && [ "$by_association" = "true" ] && allowed=true
```

Both must pass. The defaults are deliberately closed:

```yaml
command:
  name: "/review"
  roles: [admin, maintain, write]
  associations: [OWNER, MEMBER, COLLABORATOR]
```

`author_association` is the signal that actually distinguishes people, and it comes free in the event
payload:

| Association | Who |
|---|---|
| `OWNER` | the repository owner |
| `MEMBER` | a member of the owning organisation |
| `COLLABORATOR` | invited to this repository |
| `CONTRIBUTOR` | has had a pull request merged before |
| `FIRST_TIME_CONTRIBUTOR` | first pull request |
| `NONE` | everyone else |

An outside contributor is `CONTRIBUTOR` or `NONE`, so by default their `/review` does nothing. That
is the safe posture, and it is a posture rather than an accident:

```python
def test_an_outside_contributor_is_denied_by_default():
    """A passer-by on a public repository must not be able to spend the project's AI budget."""
    default = ChatCommand(name="/x")
    assert "CONTRIBUTOR" not in default.associations
    assert "NONE" not in default.associations
```

---

## Part 4 — Open source contribution models

Being safe by default is easy. Being *useful* to contributors is the harder half, and it is a policy
choice rather than a technical one. Three arrangements, in increasing order of openness.

### Maintainers only — the default

```yaml
command:
  name: "/review"
  roles: [admin, maintain, write]
  associations: [OWNER, MEMBER, COLLABORATOR]
```

A maintainer reviewing an incoming pull request types `/review security`, and the review appears. The
contributor sees it and can act on it, but cannot trigger another one — they ask, and a maintainer
runs it.

Right for: most projects, anything with a model budget worth protecting, and any pipeline that does
more than read.

The cost is a round trip. A contributor who addresses the feedback has to wait for a maintainer to
re-run it, which is exactly the latency an automated reviewer was supposed to remove.

### Returning contributors

```yaml
associations: [OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR]
```

`CONTRIBUTOR` means GitHub has seen a merged pull request from this person before. They have been
trusted once already, by a human, which is a meaningfully stronger signal than "has a GitHub
account".

This is the arrangement I would default to for an established open-source project. It gives regulars
the fast loop — push, `/review security`, read, push again — while a first-time contributor still
needs a maintainer to start it.

Note that it composes properly with the re-run logic: a contributor who types `/review security`
twice without pushing gets nothing the second time, so opening this up does not open up a way to burn
budget in a loop.

### Anyone, with limits

```yaml
associations: [OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, NONE]
```

Only worth doing with the budget controls actually configured, because you are letting anyone spend
your model credits:

```yaml
budgets:
  per_run_ai_credits: 400        # a runaway run fails rather than continuing
```
```yaml
github:
  max-ai-credits: 70             # per agent, required by lint on this target
```
```yaml
concurrency:
  group: review-${{ github.event.issue.number }}
  cancel-in-progress: true       # one review at a time per pull request
```

Even then, four defences are doing the work: the pipeline never runs fork code; the agent has no
tools, no egress and no write permission; one job holds the only write; and asking again without
pushing does nothing.

What this does *not* protect against is somebody opening fifty pull requests and reviewing each one.
GitHub gives you no per-user rate limit here. If you open the command to `NONE`, watch your usage for
a fortnight before you stop watching it.

### The rule underneath all three

**Open the command as far as the pipeline's worst outcome allows, and no further.**

`/review` reads a diff and writes text. Its worst outcome is a wasted model call and a comment
somebody dismisses — so opening it to returning contributors is a reasonable trade.

A pipeline that writes code, runs a build, or touches a deployment has a worse worst outcome and
belongs behind `associations: [OWNER, MEMBER, COLLABORATOR]` regardless of how much friction that
adds. The framework will not stop you widening it. Nothing in it can tell the difference between a
review bot and a deploy bot — that judgement is yours, and it is worth writing down in the command
where the next person will see it.

---

## Part 5 — What running it looks like

```bash
gh api -X PUT repos/:owner/:repo/environments/repo
gh aw compile
git add .lockstep .github/workflows
git commit -m "Add the review pipeline"
git push
```

The workflow must be **on your default branch** before a comment can invoke it — that is the same
property that stops a fork from modifying it. Until it is merged, `/review` does nothing.

Then, on any pull request including one from a fork:

```
/review security intent
```

Two reviews appear. Your `make ci` runs as it always did, on the same pull request, knowing nothing
about any of this.

---

## Honest limits

- Nothing here has run on a real GitHub runner. The fork behaviour described is GitHub's documented
  model rather than something observed in this repository.
- The permission API's behaviour for non-collaborators on public repositories is not clearly
  documented, which is precisely why the gate does not rely on it alone. If you are relying on
  `roles:` for a public repository, check what it actually returns for a stranger before you trust it.
- `author_association` is reported by GitHub per comment. It is a good trust signal and not an
  identity check; an account that contributed once is `CONTRIBUTOR` forever.
- Adoption was verified end to end into a repository with an existing `make`-based CI workflow —
  lint clean, compile clean, the existing workflow untouched — but the resulting workflows have not
  been executed.
