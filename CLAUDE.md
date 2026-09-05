# Working in this repository

Three kinds of thing. First the mission, then what this framework is FOR — the objectives every
change is measured against. Then the repo-local facts an agent cannot infer from the file it
happens to have open, every one of which has already cost somebody a failed run.

Read the mission and the objectives before deciding what to build. Read the rest before deciding
how.

## The mission

> Enable teams of software engineers to work together using a framework to keep AI usage
> disciplined and structured, enabling collaborative development work to proceed on the hosted SCM
> of their choice using the provider(s) and model(s) of their choice constrained by the
> process(es) and policy of their choice.

Everything below is that sentence made checkable. The mission is wider than the ten objectives
are — it names collaboration between engineers, and choice of provider and model, which no
objective yet carries — and `design/objectives.md` records that gap rather than papering over it.
It also names *policy* of their choice, which O8 does carry and `GATE-POLICY-2` says is not yet
kept: five declared `Policy` fields are printed by `ls` and reported in the receipt and enforced
by nothing.

## What this framework is for

Ten objectives, numbered so a change can cite one. They are the direction; everything below this
section is detail about how to work here without breaking something.

Their status is not a matter of opinion. `design/objectives.md` is the ledger over them, joined to
`design/gates.md` and ratcheted the same way: an objective is `held` only when gates that hold
carry it, every gap it states is anchored in a named gate, and closing one of those gates fails
the build until somebody re-reads the row. 1 of 10 are `held` today. Read it before claiming a
change serves an objective, because the row will tell you what is already carrying it.

**Every change is measured against these.** A feature, a fix, a refactor or a deletion either moves
the framework toward an objective or it does not, and a change that moves it away is one to not
make — or, if it is already here, one to remove. Say which objective a change serves in its pull
request. "It seemed useful" is how a framework acquires surface nobody wants and cannot remove.

**O1 — Drop in, and reuse what the repository already has.** A repository that builds code already
says how it compiles, lints, validates, runs and tests itself. The framework discovers those parts
and calls them; it does not ask for them to be restated, and it does not ship its own. Detection
that guesses is worse than detection that declines: a target that is not in the file is not
invented.

**O2 — Onboarding is light.** Getting this running in a repository must not be a configuration
project. What a person has to write by hand is the thing nobody could have discovered for them.

**O3 — The same process at a terminal and in CI.** Every workflow runs locally and under the
repository's own SCM, GitHub or GitLab. A capability that exists only in a hosted pipeline is one
nobody can test on a laptop, and a pipeline that carries logic is logic with no tests.

**O4 — Every model call is recorded.** An inference nobody kept is an opportunity spent and
discarded. We cannot improve what we did not record, so recording is not an option a run turns on;
it is what a run does.

**O5 — The record is what teaches it.** The framework reads its own recorded inferences and proposes
improvements to the prompts that produced them — the shipped ones and an adopter's own alike — with
the evidence attached. Suggestions only: the output is a pull request a person reads.

**O6 — The model never holds a secret.** Everything a run needs is gathered before the model
container starts and handed in. Keys, tokens and credentials are not in scope where a model can
reach them, so a successful injection has nothing to take.

**O7 — Determinism first.** Anything a script can decide is decided by a script. A model is invoked
only where a judgment is genuinely required and no deterministic rule would do. Every verb should be
asked: what part of this is arithmetic wearing a prompt?

**O8 — Extended without forking.** An adopter enhances the shipped prompts, guardrails, skills and
context, or adds a verb the framework does not ship, without editing the framework. The path to do
it is discoverable from the docs and does not require reading our source.

**O9 — New aspects on a verb that already exists.** Adding a review lens of one's own is the same
kind of act as adding a verb, and must be as easy. The four shipped lenses are examples, not the set.

**O10 — It runs on itself.** This repository uses the framework for its own reviews, fixes,
implementations and measurement. A capability that cannot be dogfooded here is one we are asking
adopters to trust on our word.

## Tests

**Test classes end in `Tests`, not begin with `Test`.** `pyproject.toml` sets
`python_classes = ["*Tests"]` because `TestScript` is a dataclass rather than a test case, and
pytest's default `Test*` prefix collected it.

```python
class MeasuredRenderingTests:   # collected
class TestMeasuredRendering:    # silently collected by NOTHING
```

This is the failure mode worth understanding, because it does not look like a failure. A file full
of `Test*` classes runs green — `no tests ran` — and a test-first run reports `tdd.not_red`,
because the suite it was supposed to turn red did not change. One real run lost $21 to it. Prefer
module-level `def test_...` functions, which need no convention at all.

Everything else about tests:

- `testpaths = ["tests"]`, `pythonpath = ["src"]`. A test outside `tests/` is not collected.
- `DeprecationWarning` is an **error**. A warning your change introduces fails the suite.
- Name a test after the property it protects, not the function it calls. The suite reads as a list
  of claims about the system: `test_a_stored_request_hashes_back_to_the_key_it_is_filed_under`.
- A test discharging a gate must name it, in the test name or the docstring — see *Gates* below.

## Commands

```
make check      # ruff format, ruff check, mypy --strict, pytest   <- run before every commit
make cov        # the coverage ratchet, which is two-sided
```

Everything runs through `uv`. `uv run pytest`, `uv run in-lockstep ...` — never a bare `python`.

`make cov` fails **in both directions**. Below `.coverage-floor` fails; more than two points above
it also fails, telling you to raise the floor. A one-directional ratchet with a stale floor is a
dead gate, so raising it is a step you take deliberately rather than a number that drifts.

## The four gates that will fail your change

These are tests, so they run in `make check` — but each fails for a reason that is not obvious from
the error, so read this before you fix one by working around it.

**Layering** (`tests/in_lockstep/test_layering.py`). Every package declares which packages it may
import, in an `ALLOWED` dict. A new top-level module needs an entry. Adding an import that the dict
forbids fails — and the fix is almost never to widen the allowance. `evaluation` and `metrics` are
leaves that may import nothing of ours, on purpose: the moment either reaches for a store it stops
being testable against a list somebody wrote by hand.

**Sinks** (`tests/in_lockstep/test_sinks.py`). Every write that leaves the process goes through
`privileged.sink`, which redacts. This walks the AST and fails on a raw `write_text`, `print`, or
`open(...).write`. It is inverted on purpose — it lists the *primitives*, not the sinks — because
an enumerated list of sinks had already missed five. If a module may not import `privileged`
(a leaf layer), **return the string and let the caller write it**.

**Gates** (`design/gates.md`, checked by `test_gates.py`). A two-sided ratchet on the status column:
a gate marked `held` must be named by some test, and a gate marked `unmet` must be named by none —
so implementing one fails the build until you update its row. Adding a load-bearing property
usually means adding a row and a test that cites it.

**Docs** (`tests/in_lockstep/test_docs.py`). The README capability matrix is checked both ways: a
`runs` row must name a symbol or command that exists, and a `planned` row must not. Python blocks in
the README, `getting-started.md`, `extending.md` and `trampoline.md` must parse; the cookbook's must
actually execute.

## Style

- Line length 110. `ruff format` decides everything else; do not hand-wrap.
- `mypy --strict` over `src`. Tests are exempt from `disallow_untyped_defs` and nothing else.
- Comments explain **why**, and especially why not the obvious alternative. The codebase is written
  so that a reader hitting a strange decision finds the reason next to it rather than in a commit
  message. Match that density; it is the house style, not decoration.
- Absent is not zero. A number nobody measured renders as `—`, never as `0`. This rule runs through
  the ledger, the eval corpus and the metrics — a reassuring figure computed from no evidence is
  the failure most of this repository's design exists to prevent.

## Commits and pull requests

**Conventional Commits, always** — `feat(scope):`, `fix(scope):`, `docs(scope):`. Workflow-created
commits are shaped by `conventional_subject()` and CI reads them.

End every commit message with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Say what was wrong and why the fix is the right shape, not what files changed — the diff already
says that.

## Things that are deliberate, so do not "fix" them

- **Prompt bodies are `.md` with a two-key header.** `name` and `description` only. A body is data
  a pack can ship, so a header that could choose the model or raise a turn cap would be an
  alternate configuration surface. Model and limits live in `.lockstep/lockstep.py`.
- **Cassettes are keyed on the whole composed prompt.** Editing a prompt invalidates every
  recording made against it. That is not a bug; re-recording is a real model call.
- **`blocked` is not a failure.** A run a budget ceiling or an approval gate stopped is the control
  working. Never fold it into a failure rate.
- **A rubric nobody judged is `outstanding`, not passed.** `pass_rate` is `None` rather than `1.0`
  when nothing was decided.
- **`decided` is separate from `status`.** A run can succeed and settle nothing, and the two must
  not be blurred.

## Running it

`in-lockstep review --offline` works with no key and no spend — a real recording ships. Use it to
check a change end to end before reaching for a credential.
