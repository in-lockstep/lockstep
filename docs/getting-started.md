# Getting started

`in-lockstep` treats the software lifecycle as a program. Not a program that *generates* a
pipeline — the Python module you write is the thing that runs. There is no compilation step, no
YAML output, and no intermediate configuration layer.

```bash
uv tool install 'in-lockstep[anthropic]'
in-lockstep init
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
meant it to be — and framework types start appearing in code that never chose to depend on them. A
dot-directory is not a valid package name, so nothing under `.lockstep/` can be imported by
accident. The module is also loaded as `in_lockstep._lifecycle` rather than as `lockstep`, so it
cannot collide with a module of your own by that name.

**Run records go to an orphan branch, not the working tree.** A record is what the run spent, what
it decided and who approved it. Writing it into the tree makes it either untracked — which loses
it — or a commit on the branch under review, which puts framework output into a diff a person is
trying to read. `lockstep-history` shares no commit with any branch you work on.

```bash
in-lockstep history            # what has been recorded here
in-lockstep history --push     # publish it; needs push access, never automatic
```

Local runs append to a local ref and stop. Reaching a remote needs credentials and is a side
effect nobody asked for by typing a command in a terminal, so publishing is a separate act — which
in practice is CI, where the job that can push carries the record out as a bundle from the job that
made it.

## The module is the configuration

```python
from in_lockstep import Lockstep, Policy
from in_lockstep.adapters import PytestTest, RuffValidate
from in_lockstep.adapters.pytest_adapter import Test
from in_lockstep.adapters.ruff_adapter import Validate
from in_lockstep.core.spend import Budget
from in_lockstep.middleware import CostBudget, otel

lockstep = Lockstep.detect()

lockstep.bind(Test, PytestTest(args=["-q"]))
lockstep.bind(Validate, RuffValidate())

lockstep.budget = Budget(usd=2.00, wall_seconds=900)
lockstep.middleware += [otel(), CostBudget(usd=2.00)]
```

Because it is code, a change to your review policy is a diff in a pull request, with blame,
history and rollback. And because it is code, you can read what it resolves to:

```bash
in-lockstep ls
```

That prints the resolved container, the middleware chain and the policy stack. It exists because
config-as-code has one genuine disadvantage over a manifest — you can read a YAML file, but you
cannot read a container — and this is the answer to it.

## Verbs and outcomes

A workflow asks for a verb; a binding decides what serves it.

```python
from in_lockstep import workflow

@workflow(id="ci/check")
async def check(ctx, paths):
    validate = await ctx.do(Validate, ValidateSpec(paths=paths))
    if validate.blocked:
        return validate
    return await ctx.do(Test, TestSpec(paths=paths))
```

`ctx.do` returns an `Outcome`, and a red test suite is a `FAILED` outcome rather than an
exception. Failure is data: workflows branch on it, and only programmer error unwinds the stack.

An outcome carries a `status` and, separately, `decided`. Those answer different questions — "how
did it end" and "did it produce evidence". A test run that collected nothing succeeded and decided
nothing, and reporting it as a clean pass would be the reassuring number this framework tries hard
not to produce.

## Running a model

```bash
in-lockstep review --base origin/main --aspect security
in-lockstep implement --ticket '#42' --approve --budget 2.00 --out .lockstep/change
in-lockstep show-prompt security             # exactly what the model is told, offline, no key
in-lockstep show-prompt implement/oneshot
```

`review` reads and reports. `implement` reads a ticket, explores the repository with tools, and
**stages** a change into an artifact — it writes nothing itself. `apply-inline --from-artifact`
is the second half, and it re-runs the path guard on what the first half produced.

Implementing needs an approval path and egress enforcement before it will start, because the
adapter declares that it writes files and executes code. `docs/extending.md` has the three
refusals and what each one wants.

`show-prompt` matters more than it looks. Prompts are composed at runtime from guardrails, a body,
skills and contexts, and "what was the model actually told?" needs an answer that costs no run.

## Developing without keys or spend

```bash
in-lockstep review --dry-run     # canned answer; proves the wiring
in-lockstep review --offline     # replays a cassette; deterministic and free
```

Cassettes sit at the `LLMInput`/`LLMOutput` seam rather than at HTTP, so one recorded against a
provider replays against a different one, and they capture tool IO as well as model IO.

## Before running unattended

```bash
in-lockstep doctor
```

Moving model invocation in-process removed an execution substrate that was also an egress
firewall, an out-of-process spend ceiling and a privilege split. `doctor` checks what replaced
each, and `docs/controls-crosswalk.md` is the honest accounting — including the one control that
was lost rather than replaced.
