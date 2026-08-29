# Getting started

`in-lockstep` treats the software lifecycle as a program. Not a program that *generates* a
pipeline — the Python module you write is the thing that runs. There is no compilation step, no
YAML output, and no intermediate configuration layer.

```bash
uv tool install 'in-lockstep[anthropic]'
in-lockstep init
```

That writes two files. `lockstep.py` is your lifecycle. `.github/workflows/lockstep.yml` is a
trampoline: it invokes the CLI and contains no lifecycle logic, because a CI host requires its own
YAML and that YAML belongs to the host.

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
in-lockstep show-prompt security     # exactly what the model is told, offline, no key
```

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
