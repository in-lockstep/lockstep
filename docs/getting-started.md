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
  detected stack: python; tests: pytest; lint: ruff; provision: uv sync --locked
wrote .gitignore
wrote .github/workflows/lockstep.yml

One job, because reviewing is read-only. Add the privileged `apply` job the
day a verb of yours produces a change to write; the file says where.

What a run keeps:
  A recording holds the request verbatim. For `review` that is the composed
  prompt and the diff. For `implement` and `fix` it is more: every file the
  model chose to open, every command's output, and a whole copy of each file
  it wrote -- because a session re-sends its history every turn.
  Redaction masks the credential shapes it knows; it never masks source.

  Locally, only under --record: .lockstep/cassettes/<verb>.json, gitignored.
  In CI the recording is written OUTSIDE the checkout, and dies with the runner.
  What survives is the cases harvested from it: the question that was asked, and
  which files and commands the session reached for -- addresses, not contents.
  Each case says what it left out, with a count, a size and a digest, so the
  omission can be checked rather than taken on trust. They ride the run artifact,
  which says how many days it is kept.
  Run records are the exception and are meant to survive: an orphan branch.
```

That writes three files. `.lockstep/lockstep.py` is your lifecycle. `.github/workflows/lockstep.yml` is a
trampoline: it invokes the CLI and contains no lifecycle logic, because a CI host requires its own
YAML and that YAML belongs to the host. The `.gitignore` covers what a run writes — appended to
yours if you have one, and only the lines it is missing.

`init --review` adds a fourth, `.github/workflows/review.yml`: a reviewer comments `/review tests`
on a pull request and that one lens runs and posts its findings, from a job that holds no provider
key. The lens is resolved against the ones your module binds, so `in-lockstep ls` is the list a
comment may name. `--implement` and `--fix` scaffold the write verbs the same way.

The closing paragraph is the whole disclosure, and it is printed rather than filed because the
default it describes is on: the trampoline records every review it pays for. An inference nobody
kept is an opportunity spent and discarded, which is why recording is not a flag you remember to
pass. Delete the two steps in the trampoline if you would rather keep nothing; they are commented
where they sit and nothing else depends on them.

## Where things live

```
.lockstep/lockstep.py     your lifecycle, committed and reviewed
.lockstep/runs/           checkpoints         (gitignored)
.lockstep/cassettes/      recordings          (gitignored)
.lockstep/cases/          harvested cases     (gitignored)
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
job that made it. When that job is cancelled or its push refused, the bundle stays in the run's
artifact, and `in-lockstep history --from-artifacts lockstep-run --push` from a job holding the
write token absorbs what the host still holds, once each. `in-lockstep report --scm` says how many
are outstanding, and without `--scm` it says it did not ask rather than presenting the branch as the
whole record.

**The second engineer reads it without doing anything.** A fresh clone has no local
`lockstep-history`; it has `origin/lockstep-history`, and the ledger reads that when there is no
local branch, so `report` and `history --explain` on a colleague's clone show the runs the branch
holds rather than "no records yet". Nothing is created by reading. `report` ends with a `ledger`
line naming the ref it read and how many records each side holds that the other does not, and
`in-lockstep history --pull` is the act that brings the remote's records onto a local branch,
record by record, without pushing.

How a record travels from a run to `origin/lockstep-history` and back to a reader is drawn in [the ledger diagram](https://in-lockstep.github.io/lockstep/diagrams/ledger.html).

## The module is the configuration

`init` detected pytest, ruff and a `uv.lock` above, so the scaffold it wrote already binds them:

```python
from in_lockstep import Lockstep
from in_lockstep.adapters import (
    CommandProvision,
    Provision,
    PytestTest,
    RuffValidate,
    Test,
    Validate,
)
from in_lockstep.middleware import CostBudget, otel
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress

lockstep = Lockstep.detect()

lockstep.bind(Test, PytestTest(args=["-q"]))
lockstep.bind(Validate, RuffValidate())

lockstep.bind(Provision, CommandProvision([['uv', 'sync', '--locked']]))

# The scaffold's one opt-out, with a long comment in the real file saying exactly why it is
# defensible for read-only review and must be re-decided before any verb that writes.
lockstep.bind(EgressPolicy, UnsandboxedEgress())

lockstep.middleware += [otel(), CostBudget(usd=2.00)]
```

Because it is code, a change to your review policy is a diff in a pull request, with blame,
history and rollback. And because it is code, you can read what it resolves to:

```bash
in-lockstep ls
```

```text
config    local working tree
repo      /home/dev/code/demo
head      8b822684ed7a  branch main
detected  stack: python; tests: pytest; lint: ruff; provision: uv sync --locked; ci: github

bindings
  EgressPolicy           -> UnsandboxedEgress(singleton, explicit)
  Provision              -> CommandProvision(singleton, explicit)
                            uv  /home/dev/.local/bin/uv  (uv on PATH)
  Test                   -> PytestTest      (singleton, explicit)
                            python  /home/dev/code/demo/.venv/bin/python  (the repository's .venv)
  Validate               -> RuffValidate    (singleton, explicit)
                            ruff  /home/dev/code/demo/.venv/bin/ruff  (the repository's .venv)

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

The indented line under `Provision`, `Test` and `Validate` says where each adapter found its tool: the
repository's own `.venv`, then the interpreter running `in-lockstep` when it lives inside the
repository (`uv run` in the checkout does), then PATH. An installed copy's own interpreter is
never it: that one has no pytest and no ruff in it. A tool found nowhere shows as `not found`
with every place looked, and `doctor` reports it as `DOC180` before any run. In the pipeline `init`
writes, the work jobs build that environment first: `in-lockstep provision` runs whatever detection
bound from a lockfile that exists (`uv sync --locked` for a `uv.lock`, `npm ci` for a
`package-lock.json`) and says `not bound` when there is nothing to build, before `doctor` looks.

The `standards` line is where an organisation's installed policy package would appear
([cookbook recipe 6](cookbook.md#6-ship-your-organisations-standards-as-a-package)). The `config`
line says which `lockstep.py` constrained this invocation, which matters the day it is a trusted
ref rather than your working tree.

The `detected` line reads the Makefile and package.json as well as pyproject, and the native manifests of Rust, Go, the JVM, Ruby, PHP, Elixir, .NET, Swift, Bazel and CMake. Where no pytest or
ruff was found, a `test` or `lint` target serves `Test` or `Validate` with an exit code, and a
`build` or `run` target (or a `build` or `start` script) serves `Build` and `Run` the same way,
to the command that is already there. `init` writes those bindings into the module, and a
repository with no module runs on them directly. A target that is not in the file is not guessed:
`build: make build` appears only when the Makefile has one.

## Verbs and outcomes

A workflow asks for a verb; a binding decides what serves it.

```python
from typing import Any

from in_lockstep import Outcome, RunContext, Test, Validate, workflow

@workflow(id="ci/check")
async def check(ctx: RunContext, paths: tuple[str, ...]) -> Outcome[Any]:
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

Which command detection binds to which verb, and where each runs, is drawn in [deterministic verbs](https://in-lockstep.github.io/lockstep/diagrams/deterministic-verbs.html).

## The first model-shaped run costs nothing

A cassette ships in the package, recorded from a real model call against a real merged pull
request. Replaying it needs no key, no network and no spend:

```bash
in-lockstep review --offline
```

```text
config    local working tree
replaying the shipped fixture: in-lockstep/lockstep#48, security lens
  note: the system prompt moved since this fixture was recorded, so what follows is the
        model's answer to the prompt as recorded — not to the one composed just now, which
        `in-lockstep show-prompt review/security` prints. Re-recording is a real model call,
        which is the thing a reader trying this offline does not have.
review/security  succeeded
  actions/save/action.yml:29 review.security: Unquoted variable in `find` command allows word-splitting on paths with spaces or glob characters
  actions/save/action.yml:23 review.security: GitHub Actions expression `${{ inputs.paths }}` is interpolated directly into a shell script before variable assignment

tokens    5361 in, 443 out
cost      $0.0000  (replayed; nothing was billed)
spans     (lockstep.py declares its own middleware; the CLI is not in that chain)
ledger    lockstep-history:records/review-security.json  (local; `in-lockstep history --push` to publish)
```

Those findings are what the model actually said about that pull request; the replay is
deterministic and free. The note is the fixture being honest: a cassette is keyed on the whole
composed prompt, the shipped prompts have moved since this one was recorded, and rather than fail
on a key miss a reader cannot fix, the demo replays what was actually sent and says so. Cassettes sit at the `LLMInput`/`LLMOutput` seam rather than at HTTP, so
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

`review` reads and reports. `--aspect` names one of the lenses the repository's Review adapter
declares, and a name that is not one is refused before anything runs, with the list of the ones
that exist: a typo is an error, not a record. `implement` reads a ticket, explores the repository
with tools, and **stages** a change into an artifact. It writes nothing itself. `apply-inline --from-artifact`
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
subject   review/security SecurityReviewPrompt@1 on anthropic:claude-sonnet-4-6
spend     $0.0000  (15 tokens, 0.038s)
```

`subject` is which runs this one is comparable with — the verb, the lens, the prompt and the model,
identified by a **hash of the composed prompt** rather than by the declared version, because editing
a prompt without bumping its version is the normal way a prompt gets edited. The `@1` is that
declared version, carried for you to read and never for identity, so a measurement is right whether
or not anybody remembered to bump it.

This line prints the subject in words. To group runs by it:

```bash
in-lockstep report --by subject --by-kind
```

Once a prompt change has merged -- one the improve loop proposed, or one a person made -- the
question is whether the runs after it differ from the runs before it. `report --around` cuts the
ledger into those two windows on one subject and prints each metric's before, after and delta,
with the run count on each side and no verdict:

```bash
in-lockstep report --around #41            # the pull request's merge commit, from the host
in-lockstep report --around 17e034b --subject review/security
```

```text
around    17e034b259c4  (2026-09-07T17:09:46-04:00)
subject   review/security
before    107 run(s) on that subject, up to that moment
after     2 run(s) since

              before       after       delta
                   —           —           —   too few runs: 107 before and 2 after, and 5 on each side is the floor

measured over 107 and 2 runs; the delta is a number, not a verdict
```

The subject is derived from the one declared `Improvable` body the merge touched, or named with
`--subject` when the merge touched none. A window thinner than five runs prints a dash and says
so, and whether a lower failure rate means the change helped is your reading, not the command's.

`--by` selects what one row aggregates over, and it applies to that grouped table and to
`--format json` — not to the full report, which always groups by kind. Pass it without either and
the report says so rather than dropping your question. The one exception is `--by actor`, which
expands the full report's *who and how* section into a table per asker — outcome mix, turns and
spend per succeeded run, findings per run — and the spread between askers, each number with the
runs it came from. Askers are stable pseudonyms, numbered by first appearance, unless you pass
`--names`; the signal is the spread, not the person. A run nobody is recorded as asking for is a
`—` row with its count, and is in no spread. Every local run is such a run until the repository
opts in:

```python
from in_lockstep import GitAuthor

lockstep.identity = GitAuthor()
```

That records the configured git author — `Name <email>`, both required, as git would write it on
a commit — on every run made at a terminal here, as `identity`, beside `ci_actor` and
`approval.by` and never in place of them. It is a line nobody detects for you, because a report
that named people who never chose to be named is the leaderboard the pseudonyms exist to refuse.

A run whose model the framework did not choose carries no subject at all rather than a partial one,
and `implement` and `fix` carry none either: those strategies append the repository's house rules at
run time, so the prompt they composed up front is not the prompt they sent.

For the whole arc a `report --around` comparison sits at the end of, see [the learning loop](https://in-lockstep.github.io/lockstep/diagrams/learning-loop.html).

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
from in_lockstep import Verb

lockstep.models.route(Verb.TRIAGE, "local:qwen3-8b")
```

Two things to know. Every shipped verb asks the model for its answer in a schema, and a small
model may not honour one; the framework repairs the reply once and then reports
`*.unparseable`, which is two paid calls to learn what the registration could have said. A
registration that declares `ModelCaps(structured_output=False)` is refused by name before
anything is sent, and `in-lockstep doctor` warns (`DOC152`) about a route to one. The shipped
`local` registration declares it capable, because it covers every Ollama model and cannot know
which of them a repository runs; register the one that cannot under a name of your own if you
want the refusal. `doctor` also warns (`DOC150`/`DOC151`) when a routed model names an
unregistered provider or an unpriced model. A route that a run would refuse at its first call
is something you see beforehand, where nothing has been spent, rather than after a wait.

## Before running unattended

```bash
in-lockstep doctor
```

```text
ERROR   DOC101  no provider-side organisation spend limit is attested
                 Set a hard monthly cap in the provider console and record it as
                 IN_LOCKSTEP_ORG_SPEND_LIMIT=<amount>. A per-run budget cannot bound a runaway trigger,
                 and the per-day ceiling the substrate enforced no longer exists.
NOTE    DOC120  could not read this repository's default branch; branch protection was not checked
                 gh said: no git remotes found
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
