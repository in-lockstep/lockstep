# Inheriting a pipeline from another repository

Every other guide here builds a pipeline inside the repository that runs it. This one builds three
repositories: a **standards** repository owned by whoever is accountable for the rules, a **pipeline**
repository owned by whoever built the review bot, and a **consumer** that gets both and writes four
files.

The working copy is in this repository, under `tests/fixtures/` — `upstream-standards`,
`upstream-review` and `consumer`. Every command and every error message below is output from
compiling it. `tests/test_inherits.py` runs it on every build, so if this guide goes stale, CI says so.

---

## Part 1 — The standards repository

Nothing here is a pipeline. It is the floor every pipeline stands on.

```
upstream-standards/
├── pipeline.yaml            spec: 1 · name: acme-standards
├── guardrails/
│   └── data-handling.md     sealed
└── skills/
    └── citation-style.md
```

```markdown
<!-- guardrails/data-handling.md -->
---
name: data-handling
description: What may leave this organization
sealed: true
enforce:
  permissions: read-all
  deny-tools: [write_file, create_*, update_*, delete_*]
  max-turns: 8
  max-ai-credits: 200
  per-run-ai-credits: 200
---

NEVER reproduce customer records, credentials, or internal hostnames in output that leaves this
organization's infrastructure.

You MUST NOT quote a value you cannot tell is synthetic. Test fixtures in this organization are
seeded from production, so a plausible-looking order id usually belongs to somebody.
```

**`sealed: true` is the whole difference between a standard and a default.** A sealed guardrail:

- reaches **every agent in every consuming pipeline** without being named by any of them;
- **cannot be excluded** by a consumer's profile;
- **cannot be shadowed** by a local file of the same name;
- has its `enforce:` block re-asserted after overlays, so no downstream customization widens it.

The first of those is the one that matters most in practice. A guardrail each pipeline has to
remember to list is a guardrail one pipeline will forget, and it will be the pipeline nobody is
looking at.

> Sealing a guardrail in your *own* repository seals it against yourself, which means nothing. The
> loader ignores `sealed:` on a local definition rather than pretending it did something.

**The four numbers are ceilings, and they are the reason "every agent" is worth saying twice.** A
band bounds a dial on an agent this organization published; a ceiling bounds every agent in a
consuming repository — including the ones it wrote itself, which no upstream will ever see. A
repository under these can write whatever agents it likes, and none of them gets more than eight
turns or 200 credits, and no single run gets more than 200 in total.

`per-run-ai-credits` is the one that bounds a bill: the per-agent ceilings do not, because a
repository under them can add a second agent. It is checked against the consumer's own
`budgets.per_run_ai_credits`, and a consumer with no budget declared is refused rather than passed —
unbounded is not under the cap. `docs/sharing.md` has the full contrast with bands, including what
sealing does and does not defend against.

`daily-ai-credits` bounds a different axis, and it is the one people are usually surprised by. A run
budget bounds *one execution* and says nothing whatever about how many executions there are — a
chat-ops command is one comment away from running four hundred times in an afternoon, every one of
them comfortably inside its budget. This ceiling is checked against `budgets.per_agent_daily_ai_credits`
the same way, and unlike the others it does not stop at compile time: it lands in every agent as
gh-aw's `max-daily-ai-credits`, which refuses the run **before the agent starts**, backed by a usage
cache. That makes it the only budget here that a runaway loop cannot outrun.

Two things to be clear about, because both are easy to get wrong:

- **It is per agent workflow, per day** — not per repository. Seven agents under a ceiling of 5000
  can spend 35,000 credits in a day. The manifest key is named `per_agent_daily_ai_credits` for that
  reason, and `lockstep show-surface` prints the product rather than the configured number.
- **You already have one.** With nothing declared, gh-aw applies `vars.GH_AW_DEFAULT_MAX_DAILY_AI_CREDITS`
  or 5000 per agent per day. Declaring the budget replaces that default with a number your
  organization chose and can cap from an upstream; leaving it unset does not mean unlimited, it
  means somebody else's default.

---

## Part 2 — The pipeline repository

An ordinary pipeline. It does not know it is going to be inherited, and it names nothing outside
itself.

```
upstream-review/
├── pipeline.yaml
├── commands/review.md          → agent: reviewer
├── agents/reviewer.md          guardrails: [house] · skills: [verdict]
├── guardrails/house.md
├── skills/verdict.md
├── scripts/collect-diff.py
├── tests/test_collect_diff.py  ← ships with the script it tests
└── evals/reviewer/cases/       ← ships with the agent it tests
```

Two of those directories are the point of the arrangement. **Evals and unit tests travel with what
they test.** `lockstep lint` looks for an inherited agent's eval cases under
`.pipeline/inherited/<alias>/evals/<agent>/cases/`, and for an inherited script's tests in that
repository's own `tests/` — never in the consumer.

A consumer forced to write those would be testing somebody else's prompt from the outside, against
its own copy of it, which is exactly the drift this framework keeps refusing to build in.

Notice what the pipeline does **not** contain: any reference to `data-handling`. It is not required
to know the standards exist. They arrive by being sealed.

---

## Part 3 — The consumer

```
consumer/
├── pipeline.yaml
├── profiles/repo.md
├── contexts/repo.md
└── guardrails/house-style.md
```

That is the entire repository. There is a test asserting it stays that way.

```yaml
# pipeline.yaml
spec: 1
name: acme-web

inherits:
  standards: ../upstream-standards        # a path while developing
  review: ../upstream-review              # in production: github.com/acme/pipeline-pr-review@v2

commands:
  review:
    from: review                          # instantiate the inherited command
    add-guardrails: [house-style]         # appended after the inherited ones

capabilities:
  actions: github.com/acme/pipeline-actions@v1.4.0
  exec: in-lockstep-exec==0.1.0
  exec-image: quay.io/acme/pipeline-exec
  compiler: in-lockstep>=0.1,<1.0
  gh-aw: v0.86.2

targets:
  github-agentic:
    out: .github/workflows
    profiles: [repo]
```

`from: review` names the alias. If that upstream defines more than one command, the compiler says so
and lists them; name one with `from: review/some-command`.

### The two files only this repository can write

```markdown
<!-- contexts/repo.md -->
---
name: repo
description: What this codebase is
---

A Python service. `ruff`, `mypy --strict` and `pytest` run on every pull request, so anything those
tools would catch is already caught.

All database access goes through `src/repo.py`.
```

The profile selects it — and that is what makes one file enough. **A context is bound to the profile,
not to an agent**, so it reaches every agent of every inherited pipeline without either upstream
naming it. Inherit three pipelines and they all learn what this codebase is from the same file.

Profiles are the one thing never inherited. They hold one deployment's secrets and choose its
contexts, and neither is knowable upstream.

---

## Part 4 — Running it

```bash
lockstep pin        # resolve each `inherits:` ref to a commit, into .pipeline/pins.lock
lockstep fetch      # materialize them at exactly those commits
lockstep compile
```

```
$ lockstep fetch
  review: copied from ../upstream-review (local, not pinned)
  standards: copied from ../upstream-standards (local, not pinned)
fetched 2 upstream(s)

$ lockstep compile
review: 2 steps -> 2 jobs · 1 agentic, 1 deterministic, 1 cacheable
  + .github/workflows/aw-review-reviewer.md
  + .github/workflows/review.yml
  + .github/workflows/shared/guardrail-baseline.md
  + .github/workflows/shared/guardrail-standards-data-handling.md
  + .github/workflows/shared/guardrail-review-house.md
  + .github/workflows/shared/guardrail-house-style.md
  + .github/workflows/shared/skill-review-verdict.md
  + .github/workflows/shared/context-repo.md
  + .github/workflows/pipeline-ci.yml
wrote 13 files (0 unchanged, 0 pruned)
```

### What is committed, and what is not

**Fetched definitions are not committed.** They land in `.pipeline/inherited/<alias>/`, which the
scaffolded `.gitignore` excludes. They are resolved state, like a virtualenv: the lock file records
which commit, and `lockstep fetch` puts it back. Committing them would make every upstream bump a
diff of somebody else's repository.

**Generated output is committed**, as always — and that is what makes an upstream change reviewable.
More on that in Part 6.

Compiling without fetching is an error that names the fix:

```
LS101: .pipeline/inherited/standards — 'standards' is inherited from ../upstream-standards
       but has not been fetched
      hint: run `lockstep fetch` — inherited definitions are resolved state, like a virtualenv,
            so they are not committed
```

The generated `pipeline-ci.yml` runs `lockstep fetch` before every check that compiles, so the drift
gate compares against the same commits your laptop did.

---

## Part 5 — What actually reached the agent

Compile and read the generated agent. Two lines carry the whole story:

```
# sources: review:agents/reviewer.md@efd67e10 lockstep:guardrails/baseline.md@f12c42fc
#          standards:guardrails/data-handling.md@addbefa2 review:guardrails/house.md@3723d767
#          guardrails/house-style.md@dde11b53 review:skills/verdict.md@0ead478c
#          contexts/repo.md@6eb5ea05
# prompt layers: guardrail:baseline | guardrail:standards/data-handling | guardrail:review/house
#                | guardrail:house-style | skill:review/verdict | context:repo
```

Four tiers of ownership, distinguishable at a glance:

| Prefix | Means | Example |
|---|---|---|
| `lockstep:` | ships inside the compiler | `guardrails/baseline.md` |
| `standards:` | inherited from that alias | `guardrails/data-handling.md` |
| `review:` | inherited from that alias | `agents/reviewer.md` |
| *(none)* | this repository wrote it | `contexts/repo.md` |

And the guardrail order in the compiled prompt is the order of authority:

```
baseline  →  standards/data-handling  →  review/house  →  house-style
(framework)      (sealed, organization)     (upstream)     (this repo)
```

Each may add to the one before it. None may weaken it.

The middle of that line is **the order this repository declares its upstreams in**, not alphabetical:
`inherits:` is a list somebody writes, so it is the list that decides which standard a later one
refines. Declare the broadest first. The two ends are not yours to move — the framework's baseline is
always first, because a floor a repository could push below itself is not a floor, and local
guardrails are always last.

### Namespacing

Everything inherited is namespaced by its alias, and **a definition resolves its references inside
its own tree**. An inherited command's `agent: reviewer` step resolves to `review/reviewer`, not to
whatever the consumer happens to have called `reviewer`. Scripts reroot into the fetched tree:

```
uv run python3 .pipeline/inherited/review/scripts/collect-diff.py --output=outputs/diff.json
```

A local file that takes an inherited name is refused rather than resolved in some direction:

```
LS200: guardrails/standards/data-handling.md — guardrail 'standards/data-handling' is defined twice
      hint: a local definition cannot take the name of an inherited one; rename yours
```

**Cross-alias references are deliberately not possible.** An inherited pipeline is self-contained;
anything organization-wide reaches it by being sealed. That rules out the whole class of "which
version of the shared thing does this pipeline want" without a resolver.

---

## Part 6 — Changing a standard

The security team edits `data-handling.md` and tags `v4`. In each consumer:

```bash
lockstep pin        # v4 → a new commit
lockstep fetch
lockstep compile
```

The pull request that carries this contains `shared/guardrail-standards-data-handling.md`, because
flattened prompt layers are committed output. **The diff is the guardrail text** — the words the
model will now be told, not a version number. `compile --check` proves the output matches the spec at
that pin, and `--semantic-diff --fail-on-blocking` reports if the change widened permissions or
budgets.

Nothing was designed for that. It falls out of committing generated output, which this framework
already requires for an unrelated reason.

### Or let the pipeline do it

The standards repository publishes an `update` command, and a consumer inherits it like any other:

```yaml
commands:
  update:
    from: standards/update
```

```markdown
<!-- upstream: commands/update.md -->
---
name: update
github:
  triggers:
    schedule: "17 6 * * *"
    repository_dispatch:
      types: [upstream-moved]
  propose:
    source: "{output_dir}/recompiled"
    destination: .
    branch: pipeline/upstream-bump
    title: "Update inherited pipelines"
    reuse-branch: true
---

## Steps

1. **Re-resolve every upstream** → script: scripts/repin.py
   - id: repin
   - emits: moved
   - uses-compiler: true

2. **Recompile at the new commits** → script: scripts/recompile.sh
   - id: recompile
   - uses-compiler: true
   - args: --output={output_dir}/recompiled
```

Four things in that are load-bearing.

**`uses-compiler: true`** drops the executor container and installs the pinned compiler. The image
deliberately does not carry `lockstep` — a runtime that could recompile would be a runtime that could
change what runs — and this is the one pipeline that legitimately needs it. `doctor` surfaces it as
`DOC020` for a human, and refuses outright (`DOC021`) if such a command has no `propose:` block: a
recompile that does not become a reviewable pull request either does nothing, or does something
nobody reviewed.

**`reuse-branch: true`** force-pushes one branch and edits the open pull request rather than opening
another. Three upstream bumps in a week leave one pull request showing the current state — and a
reviewer who already commented keeps their thread.

**The payload is ignored entirely.** `repository_dispatch` is data somebody sent, and a payload that
could name a ref would be a payload that could point a consumer at arbitrary code the moment a token
leaked. Every commit comes from resolving this repository's own `inherits:` against repositories it
already trusts. The design allows the payload to hint *which* alias moved as a scheduling
optimization; the build does not read it at all, which is simpler to defend.

**Polling is the baseline.** `schedule:` needs no privileged credential anywhere — this repository
asks its upstreams whether they moved. The dispatch is the same work sooner, for organizations that
need same-hour propagation, and it costs a GitHub App that can write to every consumer.

### The upstream side

```yaml
# acme/pipeline-standards — .github/workflows/notify-consumers.yml
on:
  workflow_run:
    workflows: [pipeline-ci]     # nothing fires until this repository's own checks pass
    types: [completed]
    branches: [main]
```

The App's installation list *is* the consumer list — `gh api /installation/repositories` — because a
second list kept in a file would poke repositories that uninstalled and miss ones that just arrived.
One unreachable consumer warns and the rest continue.

There is deliberately **no builtin** for any of this. `lockstep pin` already resolves every ref and
rewrites the lock; `repin.py` reads the lock, runs `pin`, and reads it again. A second implementation
of resolution could disagree with the first, and the one that disagreed would be the one deciding
whether to open a pull request.

---

## Part 7 — What a consumer may and may not do

**Add.** `add-guardrails:` and `add-skills:` attach local definitions to an inherited command's agents
without touching the inherited pipeline.

**Tune, within a band the upstream published.** A band governs cost and latency; it never governs
capability.

```markdown
<!-- upstream: agents/reviewer.md — a scalar is fixed, a mapping with `default:` opens a band -->
---
model: { default: claude-sonnet-4-6, allow: [claude-sonnet-4-6, claude-opus-4-1] }
max_tool_turns: 4
github:
  max-ai-credits: { default: 60, min: 30, max: 200 }
  timeout-minutes: { default: 20, max: 60 }
---
```

```yaml
# consumer: pipeline.yaml
commands:
  review:
    from: review
    agents:
      reviewer:
        max-ai-credits: 150
        model: claude-opus-4-1
```

Everything that changes what an agent can *do* stays fixed, and trying to publish a band for one is
refused at the upstream rather than at the consumer:

```
LS100: review:agents/reviewer.md — max_tool_turns cannot be banded
      hint: a band governs cost and latency, never capability. Bandable: max-ai-credits,
            timeout-minutes, model. max_tool_turns changes what this agent can do, so a consumer
            who needs a different value needs a different agent
```

Overriding a field with no band is refused rather than ignored — the failure worth ruling out is a
consumer who believes they raised a timeout and did not:

```
LS100: pipeline.yaml commands.review.agents.reviewer — max_tool_turns is fixed by review
      hint: agent 'review/reviewer' publishes no band for it.
            Tunable here: max-ai-credits, model, timeout-minutes
```

And a raised dial still has to fit the run budget:

```
DOC019: review:commands/review.md — command 'review' can spend 150 credits, over the run budget of 100
      hint: tuned here: review/reviewer. Lower it, or raise budgets.per_run_ai_credits
```

What a consumer moved lands in `.pipeline/compile-manifest.json`, so reading that file across a fleet
answers "who raised what, and against which band" without asking anyone:

```json
{ "tuned": { "review/reviewer": { "max-ai-credits": 150, "model": "claude-opus-4-1" } } }
```

**Exclude an ordinary inherited guardrail.** A profile's `exclude_guardrails` still works on anything
not sealed.

**Not exclude a standard:**

```
LS100: profiles/repo.md — guardrail 'standards/data-handling' is sealed and cannot be excluded
      hint: it is a standard 'standards' publishes, not a default; take it up with whoever owns
            that repository
```

**Overlay or eject.** Both work unchanged on inherited definitions. An overlay whose anchor an
upstream rename removed fails with `OVL404` naming the anchor — which is what turns an upstream
change into a build failure rather than a customization that quietly stopped applying.

---

## Honest limits

- **Local paths are not reproducible.** `inherits: standards: ../upstream-standards` is copied rather
  than cloned, which is what makes developing an upstream and a consumer side by side bearable.
  `doctor` reports it as `DOC017`, and it should never reach a default branch. A remote source that is
  not pinned is `DOC018`, an error.
- **No transitive inheritance.** An inherited repository's own `inherits:` is not followed — that is a
  package manager, and this is not one. List both upstreams in the consumer, which is supported and
  is the normal shape for an organization with team-level standards: see *Many upstreams, one
  consumer* in [sharing.md](sharing.md). The cost of refusing transitivity is that a consumer can
  forget one, and nothing yet warns about it.
- **Three fields are bandable**, not because three is a principled number but because those three
  demonstrably reach the emitted workflow. `runs-on` looked like an obvious fourth and is not: an
  agentic workflow's runner does not come from the agent, so banding it would have published a dial
  connected to nothing.
- **Private repositories need a credential**, and `inherits-auth:` is where you declare it. See
  *Reading a private upstream* below.
- **Nothing has run on a real GitHub runner.** The capability actions and executor image these
  pipelines reference have never been published; see the note in the README.


---

## Reading a private upstream

A consumer's `GITHUB_TOKEN` is scoped to the repository it belongs to. It cannot read another
repository — this is not a permissions setting somebody forgot, it is what that token *is*. So an
upstream that is private needs a credential from somewhere else, and `lockstep fetch` fails with a
404 until it has one.

Declare it once, in the consumer's manifest:

```yaml
inherits:
  standards: github.com/acme/pipeline-standards@v3

inherits-auth:
  app-id: PIPELINE_APP_ID              # a repository or organization variable
  private-key: PIPELINE_APP_PRIVATE_KEY  # a secret
```

The compiler wires it into the generated drift gate — mint a short-lived token, then fetch with it:

```yaml
- name: Mint a token for the private upstreams
  id: inherits-token
  uses: actions/create-github-app-token@<sha>   # pinned like every other action
  with:
    app-id: ${{ vars.PIPELINE_APP_ID }}
    private-key: ${{ secrets.PIPELINE_APP_PRIVATE_KEY }}
    owner: ${{ github.repository_owner }}
- name: Fetch inherited pipelines
  run: lockstep fetch
  env:
    LOCKSTEP_FETCH_TOKEN: ${{ steps.inherits-token.outputs.token }}
```

`SECRETS.md` lists what to set, with the `gh` commands, like every other secret this pipeline needs.

### Use an App, not a PAT

Both work. `inherits-auth: {token: SOME_SECRET}` takes any token that can read the upstreams, and
skips the minting step. It is the wrong default, for reasons that have nothing to do with this
framework:

| | GitHub App | Personal access token |
|---|---|---|
| Lifetime | minted per run, expires in an hour | until somebody rotates it |
| Scope | the repositories the App is installed on | everything its owner can reach, or a list somebody maintains |
| Belongs to | the organization | a person, who may change teams or leave |
| Rotation | automatic | a calendar reminder |

The App you want probably already exists. The [upstream notifier](#or-let-the-pipeline-do-it) uses an
App's installation list as its consumer registry — the same App, installed on the standards
repository and on every repository that inherits from it, answers both questions: *who should I tell
when this moves* and *who may read this*.

### Setting it up

1. **Create an organization App.** Settings → Developer settings → GitHub Apps → New. No webhook, no
   user permissions. One repository permission: **Contents: Read-only**. That is all `lockstep fetch`
   does — a shallow fetch of one commit.
2. **Install it** on the upstream repositories *and* on every consuming repository. The consumer is
   where the token is minted, so the App has to be installed there to mint one at all; the upstream
   is what the token then reads.
3. **Record the credentials** at the organization level, so a new consuming repository inherits them
   rather than re-doing this: a variable `PIPELINE_APP_ID` and a secret `PIPELINE_APP_PRIVATE_KEY`.
4. **Declare `inherits-auth`** in each consumer and recompile. The diff is the two steps above.

### What the framework does not do

It does not create the App, install it, or rotate anything. Those are organization decisions with
real blast radius, and a compiler that made them by side effect would be making them for you.

What it does is make the wiring declared rather than folklore: the credential is named in the
manifest, appears in `SECRETS.md`, is pinned like every other action, and is covered by the drift
gate. And `lockstep fetch` never puts the token in a remote URL or echoes it in an error — a
credential in a build log is a leaked credential, and git is fond of quoting URLs back at you.

