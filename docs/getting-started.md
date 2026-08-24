# Getting started

You are going to build a pipeline that writes its own API contract tests, runs them against a real
service every weekday morning, publishes a dashboard, and costs nothing to run once it has settled.

By the end you will have met every part of the framework, and you will have seen what happens to all
of it once it lives on GitHub: how changes get reviewed, where output goes, and how reports survive
long enough to show a trend.

The example is real and public. You can run every command here.

---

## What you'll build

The target is [httpbin.org](https://httpbin.org) — a public HTTP request-and-response service. The
pipeline holds six of its endpoints to a contract:

```
list the endpoints ──▶ write a test for each ──▶ check the tests ──▶ run them ──▶ report ──▶ propose
   (script)              (agent, ×4 parallel)      (builtin)         (builtin)   (builtin)   (PR)
   deterministic         the only AI in the run    deterministic     ...         ...         ...
```

One AI step. Five deterministic ones. That ratio is the point: the agent is asked only to do the part
that genuinely needs judgement — deciding what a good test for an endpoint looks like — and
everything around it is code that runs the same way every time.

```bash
git clone <this repo> && cd lockstep
uv run lockstep compile --root examples/httpbin
```

That directory is the whole example. Let's read it.

---

## Part 1 — The spec

Everything you author lives in markdown. Nothing under `.github/` is written by hand.

### `pipeline.yaml` — the manifest

```yaml
spec: 1
name: httpbin-contract

capabilities:
  actions: github.com/in-lockstep/lockstep/actions@actions-v1.0.0
  exec: in-lockstep-exec==0.1.0
  exec-image: ghcr.io/in-lockstep/pipeline-exec
  compiler: in-lockstep>=0.1,<1.0
  gh-aw: v0.86.2

targets:
  github-agentic:
    out: .github/workflows
    profiles: [httpbin]

budgets:
  per_run_ai_credits: 150
```

Three things worth noticing.

**Capabilities are addresses, not code.** A compiled pipeline references composite actions and a
container image; it never vendors them. `actions` and `exec-image` say *where* they are published —
any registry works for the image, `quay.io` as readily as `ghcr.io` — and `lockstep pin` resolves
each into an exact commit or digest recorded in `.pipeline/pins.lock`. A tag someone moves later
cannot change what your reviewed pipeline runs, and changing an address without re-pinning is a hard
error rather than a silent pull from where the image used to live.

There is no default for either. A compiler that quietly points at a repository you did not choose
would produce a workflow that runs somebody else's code.

> The two addresses above are the ones in this repository's examples, and **they have never been
> published** — their pins are forty zeros. `lockstep compile` says so on every run and `doctor`
> reports it as an error. Change them to your own before expecting anything to run.

**The target block is where GitHub-specific decisions live**, so the rest of the spec stays about
your pipeline rather than about GitHub.

**The budget is not optional.** A scheduled pipeline with an unbounded agent is an unbounded bill.

### `commands/validate-api.md` — the pipeline itself

A command is an ordered list of steps. This is the entire orchestration layer:

```markdown
1. **List the API surface** → script: scripts/list-endpoints.py
   - id: list-endpoints
   - args: --output={output_dir}/endpoints.json --only="{endpoints}"
   - fingerprint: curl -sf {api_url}/spec.json | shasum -a 256 | cut -d' ' -f1

2. **Write a contract test for each endpoint** → agent: test-writer
   - foreach: endpoint in {output_dir}/endpoints.json
   - output: {output_dir}/test-scripts
   - parallel: 4
   - min-success-rate: 0.9
   (if not --skip-generation)

3. **Check the generated tests are well formed** → builtin: validate-schema
   - args: --dir={output_dir}/test-scripts --require=storyId,testSteps

4. **Run the contract tests** → builtin: test-runner
   - args: --scripts-dir={output_dir}/test-scripts --run-dir={output_dir}/runs/current --parallel=4

5. **Render and publish the report** → builtin: report
   - args: --run-dir={output_dir}/runs/current --output-dir={output_dir}
```

There are four kinds of step:

| Kind | What it is | Costs |
|---|---|---|
| `script:` | your own code, run by extension (`.py`, `.sh`, `.ts`, `.js`, `.rb`, `.go`) | nothing |
| `agent:` | a model call, compiled into its own hardened workflow | credits |
| `builtin:` | something `pipeline-exec` provides — test running, validation, reporting | nothing |
| `command:` | another command, called as a reusable workflow | its own cost |

And the modifiers on those steps each buy you something specific:

- **`id:`** names the step. Overlays anchor on ids, so renaming the *label* later never breaks
  someone's customization.
- **`foreach:` + `parallel:`** fan the step out — one matrix leg per item, four at a time.
- **`min-success-rate:`** decides what a partly-failed fan-out means. Without it, one bad leg fails
  everything downstream. With it, the run continues if 90% of endpoints produced a test, and a
  separate gate job says so explicitly.
- **`(if not --skip-generation)`** makes the step conditional on a workflow input. The other form,
  `(if security in {state.pending})`, gates on a value an earlier step computed and published with
  `emits:` — which is how a pipeline branches on something it worked out for itself.
- **`fingerprint:`** is subtle and important — see caching below.

### `scripts/list-endpoints.py` — deterministic first

This script decides *what* gets tested. It's a plain Python file returning a JSON array where each
entry has a `key`:

```python
{"key": "uuid", "method": "GET", "path": "/uuid", "expects": 200,
 "describes": "Returns a JSON object with a `uuid` field. The value differs every call."}
```

That `key` becomes one matrix leg, and one output file. Everything else in the entry is context the
agent will need.

It's a script rather than an agent on purpose. The work list is then reproducible, reviewable in a
diff, and free. **`lockstep lint` will warn you** if an agent looks like it's doing work a script
should do — sorting, filtering, deduplicating, format conversion.

Note what the entry carries: the method, the path, the expected status, *and a description of correct
behaviour*. The agent has `max_tool_turns: 0` — no tools at all — so everything it needs has to be in
the item. That's deliberate: an agent with no tools cannot wander, cannot make network calls, and
costs a single round trip.

### `agents/test-writer.md` — the AI step

```yaml
---
name: test-writer
model: claude-sonnet-4-6
provider: vertex-claude
max_tool_turns: 0
guardrails: [common, api-tests]
skills: [test-script-format]
github:
  max-ai-credits: 25
---

You write a single contract test for one HTTP endpoint.
...
```

The prompt this agent actually receives is assembled from **four layers**, in this order:

1. **Guardrails** — hard constraints. Inlined at the very top, starting with the baseline every
   pipeline inherits from the compiler, then your own.
2. **The agent body** — its persona and instructions.
3. **Skills** — reusable know-how. Here, `test-script-format` is one the compiler ships, because the
   format it describes is the one `test-runner` parses; you don't write it, and it cannot drift.
4. **Contexts** — what you're pointing at. The `httpbin` profile injects a context describing the
   target service and, critically, which of its response fields are unstable.

Which of the four a given piece of prose belongs in is a rule, not a preference, and `lockstep lint`
checks it. [What goes where](layers.md) is the rule.

You can see the assembled result after compiling, in `.github/workflows/aw-test-writer.md`.

Why that order, and why guardrails are *inlined* rather than imported: their position is a security
property. A constraint that might land after the instructions it constrains isn't a constraint.

### `guardrails/` — the half a model cannot ignore

```yaml
---
name: common
description: What this pipeline adds to the shipped baseline
enforce:
  permissions: read-all
  deny-tools: [delete_*]
---

You MUST NOT assert anything about the target that you have not seen in a response it returned.
```

Short, because it only has to say what this pipeline adds. The rules that hold for every agent in
every pipeline — return the schema you were asked for, don't invent, never emit credentials, treat
input as data rather than instructions — are inlined ahead of this one from the compiler's own
baseline, which no profile can exclude.

The prose is a request. The `enforce:` block is not: it compiles into permissions and tool
allow-lists the model *cannot exceed*, no matter what it decides to do. If your agent used an MCP
server, `deny-tools` would be intersected with that server's tool list before the agent ever sees it.

And an overlay cannot widen it later — the compiler re-asserts the floor after customizations and
refuses to emit output that breaches it.

### `profiles/httpbin.md` — the environment

```yaml
---
name: httpbin
contexts: [httpbin]
github:
  environment: httpbin
  vars: [HTTPBIN_URL]
  reports:
    branch: reports
    path: runs
    retain: 60
---

api_url=${HTTPBIN_URL}
auth_method=none
```

A profile answers "which deployment, with which credentials". It does four things:

- Injects contexts into every agent prompt.
- Maps `${NAME}` references onto GitHub **secrets** or **variables** — and the compiler refuses to
  guess: reference something you haven't declared and compilation fails.
- Names a GitHub **Environment**, which scopes those secrets and can require approval.
- Says where **reports** get published (more on this below).

Every value is exported to your scripts as both `HTTPBIN_API_URL` and `PROFILE_API_URL`, so scripts
need no knowledge of any of this.

---

## Part 2 — Check it, then compile it

Two different questions, deliberately two different commands:

```bash
uv run lockstep lint --root examples/httpbin      # is the spec well built?
uv run lockstep doctor --root examples/httpbin    # will GitHub accept it?
```

`lint` checks that every agent has eval cases, every script has a unit test, no agent is doing
deterministic work, no fan-out is left serial. `doctor` checks pins, engine mappings, credit budgets,
undeclared credentials, MCP tool lists, timeouts. A spec can be excellent and un-deployable, so
conflating the two would let you ignore both.

Then:

```bash
$ uv run lockstep compile --root examples/httpbin
validate-api: 5 steps -> 6 jobs · 1 agentic, 4 deterministic, 1 cacheable
  + .github/workflows/validate-api.yml
  + .github/workflows/aw-test-writer.md
  + .github/workflows/pipeline-ci.yml
  + .github/workflows/shared/guardrail-common.md
  + .github/workflows/shared/guardrail-api-tests.md
  + .github/workflows/shared/skill-test-script-format.md
  + .github/workflows/shared/context-httpbin.md
  + .pipeline/compile-manifest.json
  + .pipeline/step-defs/validate-api.list-endpoints.json
  + SECRETS.md
wrote 11 files
next: run `gh aw compile` to produce the .lock.yml files the orchestrators call
```

### What came out, and why

**`validate-api.yml`** is ordinary GitHub Actions YAML — jobs, `needs:`, a matrix. Orchestration is
deterministic mechanics, so none of it is agentic. The fan-out looks like this:

```yaml
write-a-contract-test-for-each-endpoint:
  needs: list-endpoints
  if: ${{ inputs.skip_generation != true }}
  strategy:
    fail-fast: false
    max-parallel: 4
    matrix:
      item: ${{ fromJSON(needs.list-endpoints.outputs.items_write_a_contract_test_for_each_endpoint) }}
  uses: ./.github/workflows/aw-test-writer.lock.yml
  with:
    item: ${{ toJSON(matrix.item) }}
    output_path: outputs/test-scripts/${{ matrix.item.key }}.json
```

The item list comes from a step injected into the producing job:

```
pipeline-exec fanout --input=outputs/endpoints.json --key=key --max=256 \
  --only-missing --output-dir=outputs/test-scripts --no-shard
```

`--only-missing` drops endpoints whose test already exists, so a resumed run fans out only what's
left. `--no-shard` is there because an agent leg is a whole workflow run and can't host more than one
item.

**`aw-test-writer.md`** is the agent, compiled into a [gh-aw](https://github.github.com/gh-aw/)
agentic workflow: `permissions: read-all`, an egress allow-list, a credit budget, and the four prompt
layers assembled in order. `gh aw compile` turns it into the `.lock.yml` the orchestrator calls —
that lock file is what actually runs, and it's committed, so agent behaviour is reviewable in a diff.

**`shared/*.md`** are the flattened prompt layers, written out so the layering is auditable.

**`SECRETS.md`** lists every secret and variable the pipeline needs, with the `gh` commands to set
them.

**`.pipeline/`** holds `pins.lock` (what versions resolve to), `step-defs/` (each cached step's
normalized definition — see caching), and `compile-manifest.json` (what the compiler owns, so it can
prune files it no longer generates).

**`pipeline-ci.yml`** is how the repository checks itself. More on that shortly.

---

## Part 3 — On GitHub

```bash
gh repo create my-httpbin-pipeline --private --source=. --push
gh api -X PUT repos/:owner/:repo/environments/httpbin      # create the Environment
gh variable set HTTPBIN_URL --env httpbin --body https://httpbin.org
gh aw compile                                              # build the agentic lock files
git add -A && git commit -m "Compile" && git push
```

Then run it: **Actions → validate-api → Run workflow**.

### What a run does

```
list-endpoints ──▶ write-a-contract-test (×N, 4 at a time) ──▶ coverage gate
                                                                    │
      propose ◀── publish report ◀── validate + run tests ◀─────────┘
```

Six jobs. Only one calls a model. And the permissions are narrow and enumerated:

| Job | Permissions |
|---|---|
| `list-endpoints` | `contents: read`, `actions: read` |
| `write-a-contract-test-for-each-endpoint` | *(the agent workflow declares `read-all`)* |
| `verify-…` and `check-…` | inherited read-only |
| `render-and-publish-the-report` | `contents: write` |
| `propose-generated-artifacts` | `contents: write`, `pull-requests: write` |

Two jobs write, and neither of them runs a model or executes a test script. That separation is
deliberate: the job that executes test scripts never holds a write token, so a bad test can't do
anything a test shouldn't.

---

## Part 4 — Where the output goes

Four different lifetimes, four different mechanisms.

### Between jobs, within a run — artifacts

Jobs don't share a filesystem. Every job starts with a `restore` step and ends with a `save` step,
which carry the `outputs/` tree as artifacts. Matrix legs publish their own slice *as they finish* —
so if a run is interrupted, the next one resumes rather than starting over.

### Across runs — the two-layer cache

Any step declaring an output is wrapped in a content-addressed probe:

```yaml
- id: cache-list-endpoints
  uses: …/step-cache@<sha>
  with:
    step: list-endpoints
    key-prefix: ls-v1-httpbin-contract-validate-api-list-endpoints-<profile-hash>
    key-inputs: |
      .pipeline/step-defs/validate-api.list-endpoints.json
      scripts/list-endpoints.py
    outputs: outputs/endpoints.json
```

The key covers the step's normalized definition, the script's own contents, any upstream output it
reads, the profile, and any runtime input. Change the script and it re-runs. Change an argument in
markdown and it re-runs. Touch a file without changing it — as a fresh checkout does to everything —
and it does *not* re-run, because the key is over contents, not timestamps.

Two layers are consulted: a durable artifact from an earlier run first (it outlives the cache's
eviction window), then `actions/cache`.

**This is what `fingerprint:` is for.** Repo files can't describe a deployed service. If httpbin
changed its API tomorrow, no file in your repository would change, and a purely content-addressed
cache would happily serve you last month's endpoint list forever. The fingerprint command hashes
something *about the live target* and folds it into the key:

```
fingerprint: curl -sf {api_url}/spec.json | shasum -a 256 | cut -d' ' -f1
```

If it fails or returns nothing, the step fails — serving a stale result is the worse outcome.

### The generated tests — a pull request

This is the part that makes the pipeline cheap. The `propose` block in the command:

```yaml
github:
  propose:
    source: "{output_dir}/test-scripts"
    destination: test-scripts
    branch: pipeline/contract-tests
    title: "Generated contract tests"
```

compiles into a final job that opens a PR carrying whatever the agent produced. You read the tests,
you merge them, and **from that point on the pipeline runs committed files for nothing**. The agent
that wrote them still has no write permission at all — its output travels through an artifact into a
job that can only open a branch nobody merges without reading.

Run with `--skip-generation` and the agent job and its coverage gate are both skipped entirely; the
run executes the committed tests and costs zero credits.

### The reports — a long-lived branch

Artifacts expire. A dashboard nobody can open in three months can't show you a trend. So the profile
names a branch:

```yaml
reports:
  branch: reports
  path: runs
  retain: 60
```

and the report step publishes onto it:

```
reports (orphan branch)
├── README.md            regenerated index, newest run first
└── runs/
    ├── 17482910384/
    │   ├── dashboard.html
    │   └── dashboard-data.js
    └── 17469882201/…
```

It's an **orphan branch** — it shares no history with `main` and carries no source, so a normal clone
never pulls the report history down. The newest 60 runs are kept and older directories are pruned.
The report is published with `if: always()`, because a failing run is exactly the one whose report
someone needs to read, and each run's step summary links straight to it.

---

## Part 5 — Making changes

You never edit anything under `.github/`. You edit the spec and recompile:

```bash
$EDITOR commands/validate-api.md
lockstep compile
git checkout -b add-endpoint && git commit -am "Cover /gzip" && gh pr create
```

Your PR contains a small spec diff and a larger generated diff. The generated files are marked
`linguist-generated`, so GitHub collapses them by default — and you don't need to read them, because
four checks read them for you.

| Check | What it refuses |
|---|---|
| **Drift gate** | Committed output that doesn't match a fresh compile. A hand-edit can't merge; a spec change without a recompile can't either. |
| **Policy gate** | An unacknowledged change to the security surface — a new write permission, a new trigger, a new egress host, a widened tool allow-list. |
| **lint** | An agent without evals, a script without tests. |
| **doctor** | A floating pin, an undeclared credential, a missing budget. |

The policy gate is worth dwelling on. It compares against **the branch you're merging into**, not
against your working tree. Comparing against your working tree would only tell you whether you forgot
to recompile — which the drift gate already covers. What a reviewer actually needs to know is what
*merging* would change:

```
semantic diff (security and cost surface):
  [BLOCK] mcp-tools: aw-test-writer.md — {'jira': ['get_issue']} -> {'jira': ['get_issue', 'create_issue']}
1 blocking delta(s) — these require explicit acknowledgment
```

### When the spec can't say it

Three tiers, in order of preference:

1. **Edit the spec.** Reordering steps, changing a prompt, swapping a technique. No conflict surface
   — it *is* regeneration.
2. **Add an overlay** in `overlays/github/` — a strategic-merge patch applied at compile time, for
   the GitHub-specific things the spec deliberately doesn't model. Overlays are *inputs to*
   regeneration, so they survive it. Anchors key on step `id:`, and an anchor that matches nothing is
   a hard error with a nearest-match suggestion, never a silent no-op.
3. **`lockstep eject <file>`** takes ownership of one generated file. It snapshots the generation it
   forked from, and the drift gate then tells you when that source moves on — so the fork stays
   visible instead of quietly rotting.

---

## Part 6 — Optional state tracking

Most pipelines never need this. Reach for it only when steps need to share something that isn't a
file and isn't small enough for a job output.

A worked case: a repair loop that retries failing items, and needs to know how many attempts each
item has already had so it can give up rather than retrying forever. That count belongs to the
command, not to any one step.

Turn it on in the command's frontmatter and reference `{state_db}` wherever you need it:

```markdown
---
name: repair
state: true          # or `keep` to retain the database after the run
---

## Steps

1. **Collect failures** → builtin: collect-failures
   - args: --run-dir={output_dir}/runs/current --output={output_dir}/failures.json

2. **Record this attempt** → script: scripts/record-attempt.py
   - args: --state={state_db} --failures={output_dir}/failures.json
```

`{state_db}` expands to a real path — `outputs/.state/<pipeline>.db` — and the compiler wraps the
owning job with load and save steps:

```yaml
- name: Load state
  uses: …/state/load@<sha>
  with: { path: outputs/.state/httpbin-contract.db }
- name: Record this attempt
  run: uv run python3 scripts/record-attempt.py --state=outputs/.state/httpbin-contract.db …
- name: Save state
  if: ${{ always() }}
  uses: …/state/save@<sha>
  with: { path: outputs/.state/httpbin-contract.db, retain: false }
```

It's an ordinary SQLite file. Your script opens it, creates whatever tables it wants, and writes.
Nothing in the framework interprets it.

### The two rules the compiler enforces

The database travels between jobs as an artifact, which is **last-writer-wins**. That's perfectly
safe inside one job and quietly lossy across parallel ones — so rather than documenting the hazard,
the compiler refuses it.

**State must live in exactly one job.** Use `{state_db}` from steps that land in different jobs and
compilation fails, naming them:

```
LS200: commands/generate-tests.md — `{state_db}` is used by steps in 2 different jobs:
       2 ('Fetch issues from Jira'), 4 ('Build test manifest')
  hint: state travels between jobs as a last-writer-wins artifact; merge these steps into one
        job (remove the boundary between them) or pass values through step outputs
```

**State cannot be used inside a `foreach`.** Matrix legs run concurrently, so every leg would write
its own copy and all but one would be lost:

```
LS200: `{state_db}` is used inside a foreach step ('Repair each failing script')
  hint: matrix legs run in parallel; concurrent writes to one state artifact would be lost
```

If you declare `state: true` and then never reference `{state_db}`, the compiler tells you and emits
no database rather than carrying an empty file around.

### Reach for something else first

| You want to pass | Use |
|---|---|
| A small value between two jobs | a step output — `echo "x=1" >> $GITHUB_OUTPUT` |
| Files between jobs | the workspace; `restore`/`save` already carry it |
| A result that must outlive the run | the reports branch, or a proposed PR |
| Genuinely shared mutable state within one job | `state: true` |

`state: keep` retains the database as a long-lived artifact after the run, for when a later run needs
to read what an earlier one recorded.

---

## Part 7 — Steady state

Once the generated tests are merged, a scheduled run does this:

- `list-endpoints` — cache hit, unless the script, its arguments, or httpbin's own spec changed.
- The agent job — **skipped entirely**, because `fanout --only-missing` finds a test already
  committed for every endpoint and emits an empty matrix.
- Validate, run, report, publish — deterministic, no credits.

**Zero AI credits per run.** You pay a model when the surface changes and there's a new endpoint with
no test, and not otherwise. That's the whole economic argument for pushing everything that can be a
script into a script: the AI is used once to generate, and the generation is then an asset you own.

---

## Honest limits

- The composite actions and the executor image are contract-tested against exactly what the compiler
  emits, but they have **never executed on a real runner**. Your first live run will find things no
  local test can.
- `in-lockstep/lockstep/actions` is a placeholder. Publish the `actions/` directory somewhere and
  point `capabilities.actions` at it.
- Browser and CLI test execution is extracted and working, but the deploy modes that stand an
  application up for it aren't wired to the compiler yet. API tests against a deployed target — what
  this example does — are the supported path today.
- `docs/status.md` lists everything else that's designed but not built.

---

## Command reference

```bash
lockstep init --name=my-pipeline    # scaffold a working pipeline
lockstep pin                        # resolve capability tags to commits
lockstep compile                    # generate the workflows
lockstep compile --check            # drift gate
lockstep compile --check --semantic-diff --fail-on-blocking --base=origin/main   # what CI runs
lockstep lint                       # is the spec well built?
lockstep doctor                     # will GitHub accept it?
lockstep show-surface               # every target decision in one document
lockstep eject <file>               # take ownership of one generated file
lockstep uneject <file>             # hand it back
```

To start from scratch rather than from this example:

```bash
lockstep init --dir=my-pipeline --name=my-pipeline
cd my-pipeline && lockstep pin && lockstep compile
```

The scaffold is a working pipeline with the same shape as this one, and it lints clean from the first
commit.
