# Getting started

`in-lockstep` treats the software lifecycle as a program. Not a program that *generates* a
pipeline: the Python module you write is the thing that runs. There is no compilation step, no
YAML output, and no intermediate configuration layer.

Every command below shows the output it actually prints, captured from a real run in a small
Python repository. Your paths, hashes and timings will differ; the shape will not. A test holds
the stable lines of these captures against the real commands, so this page cannot quietly
describe a previous version of the tool.

```bash
uv tool install 'in-lockstep[anthropic]'
in-lockstep init
```

```text
wrote .lockstep/lockstep.py
  detected stack: python; tests: pytest; lint: ruff
wrote .github/workflows/lockstep.yml

One job, because reviewing is read-only. Add the privileged `apply` job the
day a verb of yours produces a change to write; the file says where.
```

That writes two files. `.lockstep/lockstep.py` is your lifecycle. `.github/workflows/lockstep.yml` is a
trampoline: it invokes the CLI and contains no lifecycle logic, because a CI host requires its own
YAML and that YAML belongs to the host.

## Where things live

```
.lockstep/lockstep.py     your lifecycle, committed and reviewed
.lockstep/runs/           checkpoints         (gitignored)
.lockstep/cassettes/      recordings          (gitignored)
lockstep-history          an orphan branch    (run records)
```

Two decisions worth knowing.

**The lifecycle module is not at the repository root.** The root is on `sys.path` for anything run
from there, so a `lockstep.py` sitting in it is importable by your project whether or not anyone
meant it to be, and framework types start appearing in code that never chose to depend on them.

A dot-directory is not a valid package name, so nothing under `.lockstep/` can be imported by
accident. The module is also loaded as `in_lockstep._lifecycle` rather than as `lockstep`, so it
cannot collide with a module of your own by that name.

**Run records go to an orphan branch, not the working tree.** A record is what the run spent, what
it decided and who approved it. Writing it into the tree makes it either untracked (which loses
it) or a commit on the branch under review, which puts framework output into a diff a person is
trying to read. `lockstep-history` shares no commit with any branch you work on.

```bash
in-lockstep history            # what has been recorded here
in-lockstep history --push     # publish it; needs push access, never automatic
```

Local runs append to a local ref and stop. Reaching a remote needs credentials and is a side
effect nobody asked for by typing a command in a terminal, so publishing is a separate act. In
practice that act is CI, where the job that can push carries the record out as a bundle from the
job that made it.

## The module is the configuration

`init` detected pytest and ruff above, so the scaffold it wrote already binds them:

```python
from in_lockstep import Lockstep
from in_lockstep.adapters import PytestTest, RuffValidate, Test, Validate
from in_lockstep.middleware import CostBudget, otel
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress

lockstep = Lockstep.detect()

lockstep.bind(Test, PytestTest(args=["-q"]))
lockstep.bind(Validate, RuffValidate())

# The scaffold's one opt-out, with a long comment in the real file saying exactly why it is
# defensible for read-only review and must be re-decided before any verb that writes.
lockstep.bind(EgressPolicy, UnsandboxedEgress())

lockstep.middleware += [otel(), CostBudget(usd=2.00)]
```

Detection reads the Makefile and package.json too. A `build` or `run` target, or a `build` or
`start` script, binds `Build` and `Run` the same way, to the command that is already there. A
target that is not in the file is not guessed: `ls` says `build: make build` only when the
Makefile has one.

Because it is code, a change to your review policy is a diff in a pull request, with blame,
history and rollback. And because it is code, you can read what it resolves to:

```bash
in-lockstep ls
```

```text
config    local working tree
repo      /home/dev/code/demo
head      8b822684ed7a  branch main
detected  stack: python; tests: pytest; lint: ruff; ci: github

bindings
  EgressPolicy           -> UnsandboxedEgress(singleton, explicit)
  Test                   -> PytestTest      (singleton, explicit)
  Validate               -> RuffValidate    (singleton, explicit)

middleware  (privileged tier runs outside this chain and is not listed)
  OtelMiddleware
  CostBudget

standards  (in_lockstep.standards entry points; applied before this module's own lines)
  (none installed)

policy
  (nothing contributed)

workflows
  selfcheck  (in_lockstep.cli)
```

Config-as-code has one genuine disadvantage over a manifest: you can read a YAML file, but you
cannot read a container. `ls` is the answer to it.

The `standards` line is where an organisation's installed policy package would appear
([cookbook recipe 6](cookbook.md#6-ship-your-organisations-standards-as-a-package)). The `config`
line says which `lockstep.py` constrained this invocation, which matters the day it is a trusted
ref rather than your working tree.

## Verbs and outcomes

A workflow asks for a verb; a binding decides what serves it.

```python
from in_lockstep import RunContext, Test, Validate, workflow

@workflow(id="ci/check")
async def check(ctx: RunContext, paths):
    validate = await ctx.do(Validate(paths=paths))
    if validate.blocked:
        return validate
    return await ctx.do(Test(paths=paths))
```

The request is one object, `Test(paths=...)`, and its type is what the binding serves, so the
call reads as what it does: do this Test.

`ctx.do` returns an `Outcome`, and a red test suite is a `FAILED` outcome rather than an
exception. Failure is data: workflows branch on it, and only programmer error unwinds the stack.

An outcome carries a `status` and, separately, `decided`. Those answer different questions: "how
did it end" and "did it produce evidence". A test run that collected nothing succeeded and decided
nothing, and reporting it as a clean pass would be the reassuring number this framework tries hard
not to produce.

## The first model-shaped run costs nothing

A cassette ships in the package, recorded from a real model call against a real merged pull
request. Replaying it needs no key, no network and no spend:

```bash
in-lockstep review --offline
```

```text
config    local working tree
replaying the shipped fixture: in-lockstep/lockstep#48, security lens
review/security  succeeded
  actions/save/action.yml:29 review.security: Unquoted variable in `find` command allows word-splitting on paths with spaces or glob characters
  actions/save/action.yml:23 review.security: GitHub Actions expression `${{ inputs.paths }}` is interpolated directly into a shell script before variable assignment

tokens    5361 in, 443 out
cost      $0.0000  (replayed; nothing was billed)
spans     (lockstep.py declares its own middleware; the CLI is not in that chain)
ledger    lockstep-history:records/review-security.json  (local; `in-lockstep history --push` to publish)
```

Those findings are what the model actually said about that pull request; the replay is
deterministic and free. Cassettes sit at the `LLMInput`/`LLMOutput` seam rather than at HTTP, so
one recorded against a provider replays against a different one, and they capture tool IO as well
as model IO.

`--dry-run` is the cheaper cousin, a canned answer that proves the wiring. It needs a diff to
review, so a repository with a single commit refuses it with `review.no_content`, which is the
control working: make a second commit first.

```bash
in-lockstep review --dry-run --base HEAD~1
```

```text
config    local working tree
review/security  succeeded

tokens    10 in, 5 out
cost      $0.0000  (replayed; nothing was billed)
```

## Running a model for real

```bash
in-lockstep review --base origin/main --aspect security
in-lockstep implement --ticket '#42' --approve --budget 2.00 --out .lockstep/change
```

`review` reads and reports. `implement` reads a ticket, explores the repository with tools, and
**stages** a change into an artifact. It writes nothing itself. `apply-inline --from-artifact`
is the second half, and it re-runs the path guard on what the first half produced.

Implementing needs an approval path and egress enforcement before it will start, because the
adapter declares that it writes files and executes code. `docs/extending.md` has the three
refusals and what each one wants.

Before any of that, see exactly what the model would be told, composed at runtime from
guardrails, a body, skills and contexts. The command works offline, because "what was the model
actually told?" must not cost a run:

```bash
in-lockstep show-prompt security
```

```text
# composed prompt: review/security  (version 1)
# source: shipped
#
#   guardrail:baseline
#   guardrail:review/reviewing
#   body:review/security-reviewer
#   skill:review/review-format
#   skill:review/review-revision

<!-- Guardrails are inlined first, verbatim: their position is a security property and is not delegated to import merge order. -->
...
```

`source` says where that came from. It reads the prompt off the **bound adapter**, so once your
module binds a lens of its own, this renders yours rather than the framework's. `--diff` shows
what you changed, which is the question a reviewer actually has:

```bash
in-lockstep show-prompt security --diff
```

## What the ledger says afterwards

Every run leaves a record on the `lockstep-history` orphan branch. `report` aggregates them. It
also says whether the history it is summing has been rewritten:

```bash
in-lockstep report
```

```text
kind    runs  failed  tokens      cost      mean
review     1       0        15  $  0.0000  $0.0000

1 record(s); `in-lockstep history --explain <run>` for any one of them
history   append-only across the retained chain
```

Absent is not zero here: a column nobody measured renders `-` rather than a reassuring 0. One
run's record, every field, in words:

```bash
in-lockstep history --explain review-security
```

```text
run       review-security
what      review  security
status    succeeded
when      2026-08-30T02:47:19+00:00
head      309a05d0553d6ea7258317bb674e17eb2f2ac537
branch    main
config    local working tree
model     anthropic:claude-sonnet-4-6
spend     $0.0000  (15 tokens, 0.038s)
```

## Local models

A `local` provider is registered out of the box and points at Ollama, at
`http://localhost:11434` or wherever `OLLAMA_URL` says. The registration declares itself
`free`, so a local model needs no entry in the cost table: the run is priced at exactly zero
rather than refused as unpriced, and the tokens are still counted, because free is not the same
as unmeasured.

```bash
in-lockstep review --base origin/main --model local:qwen3-8b
```

Routing a verb to it in `lockstep.py` is one line. This repository routes its own triage that
way:

```python
lockstep.models.route(Verb.TRIAGE, "local:qwen3-8b")
```

Two things to know. The shipped registration declares `structured_output=False`, so lenses
depending on strict output shapes may want a larger model than the one that fits on a laptop.
And `in-lockstep doctor` warns (`DOC150`/`DOC151`) when a routed model names an unregistered
provider or an unpriced model. A route that a run would refuse at its first call is something you
see beforehand, where nothing has been spent, rather than after a wait.

## Before running unattended

```bash
in-lockstep doctor
```

```text
ERROR   DOC101  no provider-side organisation spend limit is attested
                 Set a hard monthly cap in the provider console and record it as
                 IN_LOCKSTEP_ORG_SPEND_LIMIT=<amount>. A per-run budget cannot bound a runaway trigger,
                 and the per-day ceiling the substrate enforced no longer exists.
ERROR   DOC121  the default branch has no protection rule, or it could not be read
                 The apply job holds an ambient repository token that can write any branch, so branch
                 protection is what keeps protected branches unreachable. Without it, 'writes go through
                 a pull request' is a convention rather than a guarantee.
WARNING DOC130  no egress enforcement is declared
                 Set IN_LOCKSTEP_EGRESS=enforced where the host constrains egress. Runs that hold write
                 or execute tools, or that read untrusted content, are refused without it.

3 finding(s), 2 error(s)
```

That is a fresh repository being told the truth, not a broken install: nothing is attested yet,
so `doctor` fails.

Moving model invocation in-process removed an execution substrate that was also an egress
firewall, an out-of-process spend ceiling and a privilege split. `doctor` checks what replaced
each, and `docs/controls-crosswalk.md` is the honest accounting, including what was lost rather
than replaced. The [cookbook](cookbook.md) has the recipes that turn each finding green.
