# Sharing pipelines across an organization

One security team owns the review standards. Two hundred repositories should follow them, pick up
changes without anybody copying a file, and still be able to say what *their* codebase is.

**Tier 1 and sealing are built** — see [Inheriting a pipeline from another repository](inheriting.md)
for the walkthrough. `tests/fixtures/` holds the whole arrangement — an
`upstream-standards` repository that publishes sealed guardrails and nothing else, an
`upstream-review` repository that publishes a pipeline, and a `consumer` whose entire contents are a
manifest, a profile, a context and one house rule. `tests/test_inherits.py` compiles it.

Bands (Part 4) and the updater (Part 7) are designed here and not built.

---

## The taxonomy already says what is shareable

[What goes where](layers.md) distinguishes the prompt layers by **what binds them**. Read that table
again with an organization in mind and it answers the distribution question directly:

| Layer | Bound by | Varies per | Owner |
|---|---|---|---|
| guardrail | agent + command | the organization | **upstream** |
| skill | agent | the organization | **upstream** |
| agent | a command's step | the organization | **upstream** |
| command | the trigger | the organization | **upstream** |
| **context** | **profile** | **the repository** | **downstream** |
| **profile** | the invocation | **the deployment** | **downstream** |

That is not a coincidence and it is not a new rule. A context is the only layer permitted to name a
product, and a profile holds the URLs and secret names of one deployment — so those two are exactly
the things a shared pipeline cannot know. Everything else is exactly the thing an organization wants
to say once.

**A consuming repository writes a profile and a context. It inherits the rest.**

---

## Three tiers, matching the customization ladder

The existing ladder is spec → overlay → eject. Sharing adds a rung above it.

| Tier | What the repo holds | Customization | For |
|---|---|---|---|
| **0 — call it** | one caller workflow | none | the long tail; repos that want the bot and no opinions |
| **1 — inherit it** | `pipeline.yaml`, a profile, a context | its own subject knowledge | the normal case |
| **2 — fork a piece** | the above, plus overlays or an ejected file | anything | the few repos that genuinely differ |

### Tier 0 — call the upstream workflow

The upstream repository compiles its pipelines and commits the output. A consumer adds one file:

```yaml
# .github/workflows/review.yml
name: review
on:
  issue_comment: { types: [created] }
jobs:
  review:
    uses: acme/pipelines/.github/workflows/review.yml@3f2a91c
    secrets: inherit
```

This is plain GitHub reusable-workflow behaviour and needs nothing from Lockstep. It gets a review
bot with no knowledge of the repository — no "all database access goes through `src/repo.py`", no
conventions that are deliberate and are not findings. Generic reviews are worth something, and this
tier costs one file, so it is the right answer for repositories nobody is going to invest in.

Its limit is the point: **there is nowhere to put a context**, which is the layer that makes a review
worth reading.

---

## Tier 1 — inherit

### The manifest

```yaml
# .lockstep/pipeline.yaml
spec: 1
name: acme-web

inherits:
  standards: github.com/acme/pipeline-standards@v3
  review:    github.com/acme/pipeline-pr-review@v2

commands:
  review:
    from: review          # instantiate the inherited command

capabilities:
  actions: github.com/acme/pipeline-actions@v1.4.0
  exec-image: quay.io/acme/pipeline-exec
  compiler: lockstep>=0.1,<1.0
  gh-aw: v0.34.0

targets:
  github-agentic:
    out: .github/workflows
    profiles: [repo]
```

Plus two files the repository actually has to write:

```
.lockstep/profiles/repo.md      environment, secrets, which contexts to inject
.lockstep/contexts/repo.md      what this codebase is
```

That is the whole consuming repository. Nine other directories stay empty.

### Where imports come from, and how they are pinned

A git ref — a tag *or a branch* — resolved to a commit and recorded beside the capability pins:

```json
// .lockstep/.pipeline/pins.lock
{
  "inherits": {
    "standards": { "repo": "acme/pipeline-standards", "ref": "v3",   "sha": "9c1f…" },
    "review":    { "repo": "acme/pipeline-pr-review", "ref": "main", "sha": "4ab7…" }
  }
}
```

This reuses `resolve_ref`, `check_pins_current` and the moved-ref warning — the same supply-chain
property the capability actions already get. A tag someone retags is caught on the next build rather
than silently changing what a reviewed pipeline runs. Changing the ref in the manifest drops the
recorded commit, so a stale pin cannot survive a redirection.

The fetched spec is **not committed**. `lockstep fetch` materializes each import at its pinned commit
into `.pipeline/inherited/<alias>/` — one commit, fetched by SHA, not a branch that happens to point
at it today — and the scaffolded `.gitignore` excludes it. `lockstep compile` refuses to run without
it, naming the command, and the generated `pipeline-ci.yml` runs `lockstep fetch` before every check
that compiles.

A path instead of a `github.com/` source is copied rather than cloned, which is what makes developing
an upstream and a consumer side by side bearable. `doctor` reports it as `DOC017`: unpinnable, so
nobody else can reproduce the build.

### The part that makes this work: the drift gate is the review mechanism

Compiled output *is* committed, and the flattened prompt layers are part of it:

```
.github/workflows/shared/guardrail-standards-data-handling.md
.github/workflows/shared/skill-standards-review-writing.md
```

So when the security team changes a guardrail and a bot bumps `standards: v3 → v4` in two hundred
repositories, **each pull request diff contains the changed guardrail text.** Not a version number —
the words. A reviewer sees what the model will now be told, `compile --check` proves the output
matches the spec at that pin, and `--semantic-diff --fail-on-blocking` reports if the change widened
permissions or budgets.

Nothing new is needed for this. It falls out of committing generated output, which this framework
already requires for a completely different reason.

Provenance comes along too. The compile manifest already stamps every source with a content hash, and
already distinguishes framework-shipped layers with a `lockstep:` prefix:

```json
"sources": [
  "lockstep:guardrails/baseline.md@f12c42fc",
  "standards:guardrails/data-handling.md@a71c0e94",
  "guardrails/house-style.md@3e8b1102"
]
```

Three tiers of ownership, visible in one line of a generated file.

### Names

An import's definitions are namespaced by its alias: `standards/data-handling`,
`review/security-reviewer`. The framework already supports paths in fragment names
(`skills/test/common.md` is `test/common`), so this needs no new resolution rule — only that a local
file cannot take an inherited name, which is refused rather than silently resolved either way.

**A definition resolves its references inside its own tree.** An inherited command's `agent:` step,
its guardrails and its scripts all scope to the alias it arrived under, so a consumer that happens to
have an agent by the same name does not capture it. Scripts reroot into the fetched tree, which is
what makes `bash .lockstep/.pipeline/inherited/review/scripts/post-reviews.sh` the emitted path.

Cross-alias references are deliberately not a thing: an inherited pipeline is self-contained, and
anything organization-wide reaches it by being **sealed** rather than by being named.

Provenance carries the alias, so a generated file says which upstream each layer came from:

```
# sources: review:agents/reviewer.md@efd67e10 lockstep:guardrails/baseline.md@f12c42fc
#          standards:guardrails/data-handling.md@addbefa2 guardrails/house-style.md@dde11b53
```

Framework, upstream pipeline, upstream standard, local — four tiers of ownership in one line.

### Evals and tests travel with what they test

An inherited agent is evalled by whoever published it: `LNT001` looks for its cases under
`.pipeline/inherited/<alias>/evals/<agent>/cases/`, not in the consumer. Same for `LNT002` and an
inherited script's unit tests. A consumer forced to write those would be testing somebody else's
prompt from the outside, against a copy of it — which is the drift this framework keeps refusing to
build in.

---

## Part 3 — Standards, not defaults

Inheriting is not enough for an enterprise. The difference that matters:

- a **default** is something downstream may replace;
- a **standard** is something downstream may extend and must not weaken.

Today a downstream profile could write `exclude_guardrails: [data-handling]` and the corporate rule
would silently vanish from every prompt. That is the hole sealing closes.

```markdown
---
name: data-handling
description: What may leave this organization
sealed: true
enforce:
  permissions: read-all
  deny-tools: [write_file, create_*, update_*]
---

NEVER include customer records, credentials, or internal hostnames in output that leaves this
organization's infrastructure.
```

Four compiler rules, all implemented, each mirroring something that already existed:

1. A sealed guardrail **cannot be excluded**. `exclude_guardrails` naming one is an error, not a
   silent drop — the same reasoning that made a spec guardrail named `baseline` an error rather than
   a file the author sees in the repository and never in the prompt.
2. A local file **cannot shadow** a sealed name. Already implemented for shipped guardrails.
3. Sealed guardrails are **inlined ahead of local ones**, and reach every agent without being named
   — a guardrail each pipeline has to remember to list is one that a pipeline will forget. Position
   is a security property; this extends the existing ordering rather than inventing one.
4. Their `enforce:` block is **re-asserted after overlays**, which the compiler already does for the
   floor it computes today.

That gives one order of monotonically decreasing authority:

```
framework baseline  →  sealed org standards  →  local guardrails  →  agent body  →  skills  →  contexts
   (cannot edit)         (cannot weaken)          (yours)
```

`enforce:` is the half that does not depend on a model cooperating. A sealed
`deny-tools: [write_file]` compiles into the workflow's tool allow-list, and no downstream overlay
can widen it — the compiler refuses to emit output that breaches the floor it computed.

---

## Part 4 — What a consumer may still change

Inheritance that permits nothing gets forked. The surface, smallest first:

**Add.** Write a local guardrail or skill and attach it without touching the inherited command:

```yaml
commands:
  review:
    from: review
    add-guardrails: [house-style]     # appended after the inherited ones
```

**Tune, within a band.** This is the piece with no precedent in the codebase and the one most likely
to sprawl, so it needs a rule before it needs a syntax:

> **A band governs cost and latency. It never governs capability.**

Anything that changes what an agent *can do* — its permissions, its tools, its network, how many
turns it gets, which MCP servers it can reach, its guardrails, its body — is not tunable at any
band. Those are the surface upstream's evals were written against and the surface a security review
signed off. A consumer that needs a different one needs a different agent, and that conversation
belongs upstream. Everything a band can move is a dial on the same machine.

| Field | Bandable | Why |
|---|---|---|
| `max-ai-credits` | yes | how much a run may spend |
| `timeout-minutes` | yes | how long it may take |
| `model` | yes, from a list | a cost/quality trade at the same capability surface |
| `runs-on` | yes, from a list | where it executes; a larger runner is not a wider agent |
| `max_tool_turns` | **no** | turns are reach. More turns is a different agent. |
| `permissions`, `deny-tools`, `network`, `mcp` | **no** | the enforced floor |
| guardrails, skills, the body | **no** | add to them; you cannot replace them |

Upstream declares the band. A scalar stays fixed; a mapping with `default:` opens it:

```markdown
<!-- acme/pipeline-pr-review — agents/security-reviewer.md -->
---
name: security-reviewer
model: { default: claude-sonnet-4-6, allow: [claude-sonnet-4-6, claude-opus-4-1] }
max_tool_turns: 6
github:
  max-ai-credits: { default: 90, min: 40, max: 200 }
  timeout-minutes: { default: 20, max: 60 }
---
```

The consumer moves it, or does not:

```yaml
commands:
  review:
    from: review
    agents:
      security-reviewer:
        max-ai-credits: 150
        model: claude-opus-4-1
```

Four rules, each with an error that names the thing rather than the rule:

1. **Outside the band is refused**, naming the band: *`max-ai-credits: 400` is outside the band
   `40–200` that `review` publishes for `security-reviewer`.*
2. **A field with no band is fixed.** Overriding it is refused rather than ignored — the failure mode
   worth ruling out is a consumer who believes they raised a timeout and did not.
3. **Two command-uses may not tune one agent differently.** The compiler already refuses an agent
   that resolves to different prompt layers in different commands; this is the same rule about the
   same thing, and the same error shape.
4. **A raised band still meets the run budget.** `budgets.per_run_ai_credits` is a ceiling over the
   whole run, and a band that lets one agent exceed it should fail at compile rather than at 3am.

Every override lands in `.pipeline/compile-manifest.json` beside the sources, so reading that file
across a fleet answers "who raised what, and against which band" without asking anyone.

**Overlay.** The existing strategic-merge patches work unchanged on inherited definitions, with
`OVL404` still failing loudly on an anchor that matches nothing — which is what turns an upstream
rename into a build failure instead of a customization that quietly stopped applying.

**Eject.** The escape hatch, unchanged: take ownership of one generated file, keep the merge base,
and `compile --check` reports when its source moves on without it.

The rungs are ordered by how loudly they break when upstream changes, which is the property that
matters: a repository that overlaid something should hear about it, and one that only wrote a context
should never have to think about it.

---

## Part 5 — Rollout, and knowing where you stand

**Changing a standard.** Merge upstream, and let the consumers notice — Part 7 is how, and why the
recompile has to happen in each consumer rather than centrally. A pull request lands in each one,
bumping the pin and committing the recompile. The diff shows the guardrail text. CI runs the drift
gate, the policy gate, and each repo's own evals. Merge is per-repo and reviewed.

**When it must be everywhere now.** Pinning means a repository that never merges the bump keeps
running v3, which is correct for routine changes and unacceptable for an urgent one. Two answers, and
an organization should choose deliberately:

- an org-level required workflow that fails if `inherits.standards` is older than a floor the
  standards repo publishes — enforcement without seizing the merge button;
- for the genuinely urgent case, Tier 0's `uses:` reference at a moving tag, accepting that unreviewed
  upstream changes take effect immediately. That trade should be made once, in the open, and not by
  default.

**Fleet visibility.** Every consumer already writes `.pipeline/compile-manifest.json` containing the
compiler version and every source with its hash. Reading that file across an org gives who is on what
version, which prompt layers they actually resolved, and who has ejected something — without asking
anyone to report. That is the fleet dashboard listed in `status.md` as open, and this is the input it
was waiting for.

---

## Part 6 — What it would take

| Piece | Status | Notes |
|---|---|---|
| `inherits:` in the manifest; `lockstep fetch` at a pinned commit | **built** | reuses `resolve_ref`, `pins.lock`, `check_pins_current` |
| Namespaced merge, script rerooting, aliased provenance | **built** | fragment names already carried paths |
| `sealed:` and its four rules | **built** | shadowing check and enforce-floor re-assertion already existed |
| `from:` / `add-guardrails:` / `add-skills:` in `commands:` | **built** | the manifest already carried a per-command map |
| Evals and script tests resolved upstream | **built** | `LNT001`, `LNT002` follow the definition |
| `DOC017` / `DOC018` — unpinned and unpinnable upstreams | **built** | |
| Bands on tunable fields | design | start with three fields |
| Transitive imports | — | **not in v1.** An import that imports is a package manager; require the consumer to list both, and say so |
| Private-repo fetch in CI | medium | **the real operational cost.** A consumer's `GITHUB_TOKEN` cannot read another private repository; this needs a GitHub App or a PAT, per consumer. Worth knowing before starting rather than after |

The `.lockstep/` convention already means a consuming repository pays one directory. Combined with
inheritance, adopting a corporate pipeline becomes: create `.lockstep/`, write a manifest, a profile
and a context, run `lockstep pin && lockstep compile`, and commit.

---

## Part 7 — Keeping consumers current

Pinning is what makes inheritance safe and what makes it go stale. The updater is itself an inherited
pipeline: upstream writes it once, every consumer compiles it, and nobody downstream authors anything.

```markdown
<!-- acme/pipeline-standards — commands/update.md, inherited by every consumer -->
---
name: update
description: Open a pull request when an upstream this repository inherits has moved
github:
  triggers:
    repository_dispatch: [upstream-moved]
    schedule: "17 6 * * *"
    workflow_dispatch: true
---

## Steps

1. **Resolve every tracked ref to a commit** → builtin: check-upstreams
   - id: upstreams
   - emits: moved
   - args: --output={output_dir}/moved.json

2. **Recompile at the new pins** → script: scripts/repin.sh
   (if standards in {upstreams.moved})
   - args: --alias=standards

3. **Open or update one pull request** → builtin: propose-upstream-bump
   - args: --moved={output_dir}/moved.json
```

### The work has to happen downstream

Upstream needs an App with write access to dispatch at all — so why not have upstream open the pull
request itself, and skip the inherited pipeline?

Because **recompiling requires the consumer's environment**. That repository has its own capability
pins, its own `exec-image`, its own extensions, its own overlays and profiles. Upstream opening the
pull request would mean faithfully reproducing every consumer's toolchain, and getting it subtly
wrong in the ones that differ — which are exactly the ones where being wrong matters.

Running in the consumer's own CI makes it correct by construction. So the dispatch carries **a signal,
not content**: "something you inherit has moved." Everything else is resolved locally.

### Trigger and tracking ref are different questions

Merging to main upstream is a *trigger*. What each consumer *follows* is its own choice, declared in
its own manifest:

| Consumer declares | Picks up | Suits |
|---|---|---|
| `standards: …@main` | every merge upstream | one or two canary repositories |
| `standards: …@v3` | only when the tag moves | everyone else |
| `standards: …@v3.2.1` | never, until edited by hand | a repository that has opted out, visibly |

One dispatch serves all three. A consumer tracking `@v3` receives the signal, resolves `v3` to the
same commit it already has, finds nothing to do, and exits — so upstream does not need a consumer
list segmented by policy.

**Keep one or two canaries on `@main`.** An upstream merge that breaks a consumer's overlay surfaces
as `OVL404` in the canary's pull request, before the tag is cut, in the only place that can detect it:
a repository with real overlays. Upstream should not dispatch until its own drift gate and evals pass
on main, and the canary is the second gate after that.

### The payload is untrusted input

The dispatch payload is data somebody sent, and the framework's rule about issue text applies to it
unchanged: **never resolve a ref from the payload.** A payload naming a branch or a commit to fetch is
a payload that can point a consumer at arbitrary code the moment a token leaks.

The pipeline resolves each SHA from the consumer's *own* `inherits:` entry, against the repository it
already trusts. The payload may at most hint which alias moved, so a consumer can skip a run it has
nothing to do in — a scheduling optimization, never an input to what gets fetched.

### Push or poll

| | Poll — `schedule:` | Push — `repository_dispatch` |
|---|---|---|
| Upstream credentials | **none** | a GitHub App with `contents: write` on every consumer |
| Latency | the cron interval | seconds |
| Cost | one cheap run per repo per day | one run per repo per upstream merge |
| Setup | zero | an App, installed org-wide |

Poll is the baseline because it works with no privileged credential anywhere, and a day is a
reasonable latency for a standards change that is going to be reviewed by a human anyway. Push is the
opt-in for organizations that need same-hour propagation, and its cost is honest: an App that can
write to every repository is a serious credential, and it exists solely to say "go look."

### One pull request, not a stack

Three upstream merges in a week must not leave three open bump pull requests. This is the problem the
review pipeline already solved: a marker in the body, found and updated in place.

```
<!-- lockstep:upstream-bump aliases=standards,review -->
```

### When the recompile fails

An upstream change that breaks a consumer's overlay produces no output to commit. Do not swallow it
and do not open an issue somewhere else: open the pull request with the pin bump anyway and let CI
fail on it. The breakage then lives in the same review surface as the change that caused it, with the
`OVL404` message naming the anchor that no longer exists.

### Two things it needs that do not exist yet

Writing the command out made both of them obvious, and neither is visible from the design sketch.

**The updater is the one pipeline that needs the compiler at runtime.** Every other job runs in the
`pipeline-exec` container, which deliberately does not contain `lockstep` — the compiler is a
development dependency, and a runtime that could recompile would be a runtime that could change what
runs. But re-pinning and recompiling is exactly this pipeline's job.

The answer is a step-level flag rather than putting the compiler in the image:

```markdown
2. **Re-pin and recompile** → script: scripts/repin.sh
   - uses-compiler: true
```

which emits a job that installs the pinned compiler with `uv tool install` and drops the exec
container — precisely what the generated `pipeline-ci.yml` jobs already do. Nothing else in the
framework may set it, and `doctor` should say so if anything else does: a pipeline that can recompile
itself is a pipeline whose committed output stopped being the reviewed artifact.

**`propose-pr` opens a new pull request every time.** Three upstream merges in a week would leave
three open bumps. It needs a `reuse-branch: true` input: a stable branch name, force-pushed, with
`gh pr edit` when one is already open. This is the same shape the review pipeline uses for revising
a review in place, and the same reason — a second artifact saying what the first one said is worse
than no artifact.

### What this would take

| Piece | Status |
|---|---|
| `propose-pr` composite action | **exists** — branch-scoped token, opens the pull request |
| `--semantic-diff` for the pull request body | **exists** — reports widened permissions and budgets |
| Drift gate proving the recompile | **exists** |
| `repository_dispatch` / `schedule` triggers | **exists** in the trigger block |
| `inherits:`, `lockstep pin`, `lockstep fetch` | **exists** — Part 1 is built |
| Resolving a branch ref, not only a tag | **exists** — `resolve_ref` handles both |
| `uses-compiler:` on a step | new, small |
| `reuse-branch:` on `propose-pr` | new, small |
| `scripts/repin.sh` — pin, diff the lock, emit `moved` | new; `lockstep pin` already reports what moved, so this is `git diff --exit-code` and an output |
| Fan-out job in upstream | new — iterate the App's installations and dispatch |

Note what is *not* on that list: a builtin to check upstream versions. `lockstep pin` already resolves
every ref and reports which ones moved, so the step is a `git diff` against the lock file it just
rewrote. A new builtin would have been a second implementation of resolution logic that could
disagree with the first.

**One caveat with no clean answer:** the updater is inherited and pinned like everything else, so a
consumer whose updater is broken cannot receive the fix that repairs it. Keep this command small,
keep it boring, and expect that repairing it across a fleet is a manual morning.

---

## Honest assessment

**What is genuinely good here.** The distribution boundary was not designed — it was derived from the
binding axis that already distinguishes the layers, which is a sign the taxonomy was carrying real
information. And the review mechanism for standards changes is the drift gate, which exists for an
unrelated reason and happens to be exactly right: the diff a reviewer sees is the prompt text, not a
version bump.

**What is uncomfortable.** Bands invite a configuration language. Every field somebody asks to tune
is a small, reasonable request, and the sum is a second spec format layered over the first. The rule
that a band governs cost and latency but never capability is what holds the line, and it will be
argued with — the first request will be for `max_tool_turns`, it will be reasonable, and granting it
would mean a consumer can widen an agent past the surface its evals cover.

**What could go wrong.** Two hundred repositories pinned to two hundred different standard versions,
with nobody merging the bump PRs, is worse than copy-paste — because copy-paste is at least visibly
stale. The required-workflow floor is what prevents that, and it should be built alongside
inheritance rather than after somebody notices the drift.

**What this does not solve.** A shared pipeline still needs each repository to write a real context,
and a repository that writes a perfunctory one gets perfunctory reviews. No mechanism fixes that; it
is the part that has to be worth doing.
