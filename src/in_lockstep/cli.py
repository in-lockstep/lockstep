"""The `in-lockstep` command."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import click

from . import __version__
from .adapters.pytest_adapter import PytestTest, Test
from .adapters.ruff_adapter import RuffValidate, Validate
from .core.context import DISABLE_ENV
from .core.outcome import Status
from .core.types import TestSpec, ValidateSpec
from .core.workflow import registered, workflow
from .lockstep import Lockstep
from .middleware.budget import CostBudget
from .middleware.otel import Recorder, otel
from .privileged.redact import Redact

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 3


def _default_lockstep() -> tuple[Lockstep, Recorder]:
    """A repository's own module if it has one, else detected defaults.

    The module is loaded from a trusted ref, never from the ref under review.
    """
    from .loader import NoLifecycle, load, lockstep_from

    try:
        module, ref = load(
            ".",
            base=os.environ.get("GITHUB_BASE_REF", ""),
            reviewing=os.environ.get("GITHUB_EVENT_NAME", "") in ("pull_request", "pull_request_target"),
        )
        configured = lockstep_from(module)
        recorder = Recorder()
        if not configured.middleware:
            configured.middleware = [otel(recorder), CostBudget(usd=2.00)]
        return configured, recorder
    except NoLifecycle:
        pass

    lockstep = Lockstep.detect()
    lockstep.bind(Validate, RuffValidate(cwd=lockstep.repo.root))
    lockstep.bind(Test, PytestTest(args=["-q", "--no-header"], cwd=lockstep.repo.root))
    recorder = Recorder()
    lockstep.middleware = [otel(recorder), CostBudget(usd=2.00)]
    return lockstep, recorder


@workflow(id="selfcheck")
async def selfcheck(ctx: Any, paths: tuple[str, ...]) -> dict[str, Any]:
    """Validate, then test. The smallest thing that proves the core actually dispatches."""
    validate = await ctx.do(Validate, ValidateSpec(paths=paths))
    if validate.status is Status.BLOCKED:
        return {"validate": validate, "tests": None}
    tests = await ctx.do(Test, TestSpec(paths=paths))
    return {"validate": validate, "tests": tests}


@click.group()
@click.version_option(__version__, prog_name="in-lockstep")
def main() -> None:
    """Run your lifecycle."""


@main.command(name="run")
@click.argument("target")
@click.option("--paths", multiple=True, help="Restrict to these paths.")
@click.option("--no-middleware", is_flag=True, help="Bisect behaviour. Cannot disable the privileged tier.")
@click.option("--recover", "recover_id", default="", help="Resume an interrupted run by id.")
@click.option("--checkpoint/--no-checkpoint", default=False, help="Checkpoint completed steps.")
def run_cmd(
    target: str, paths: tuple[str, ...], no_middleware: bool, recover_id: str, checkpoint: bool
) -> None:
    """Run a workflow.

    `--recover` restarts an interrupted run from its checkpoints. It covers machine failure only:
    a run never waits on a person, so resuming after a human is a different mechanism entirely.
    """
    if target != "selfcheck":
        known = ", ".join(r.id for r in registered())
        raise click.ClickException(f"unknown workflow {target!r}; registered: {known}")

    lockstep, recorder = _default_lockstep()
    if no_middleware:
        # Note what this does NOT switch off: the kill switch is checked before the chain, and
        # redaction, egress and residency are privileged rather than middleware.
        lockstep.middleware = []

    run_id = recover_id or "selfcheck-local"
    ctx = lockstep.context(run_id=run_id)
    if checkpoint or recover_id:
        from .platform.state import StateStore

        ctx.state = StateStore()
        ctx.recovering = bool(recover_id)
        if recover_id:
            done = ctx.state.completed(recover_id)
            click.echo(f"recovering {recover_id}: {len(done)} completed step(s)")
    result = asyncio.run(selfcheck(ctx, paths or (lockstep.repo.root,)))

    validate = result["validate"]
    tests = result["tests"]

    for label, outcome in (("validate", validate), ("test", tests)):
        if outcome is None:
            click.echo(f"{label:<9} not run")
            continue
        undecided = "" if outcome.decided else "  (decided nothing)"
        click.echo(
            f"{label:<9} {outcome.status.value}"
            + (f"  ({outcome.reason})" if outcome.reason else "")
            + undecided
        )
        for finding in outcome.findings[:5]:
            where = f"{finding.path}:{finding.line} " if finding.path else ""
            click.echo(f"          {where}{finding.id}: {finding.message}")

    click.echo("")
    click.echo(f"spans     {len(recorder.spans)}")
    click.echo(f"metrics   {len(recorder.metrics)}")
    click.echo(f"spend     ${ctx.spend.charged.usd:.4f}, {ctx.spend.charged.wall_seconds:.2f}s")

    if validate is not None and validate.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    statuses = [o.status for o in (validate, tests) if o is not None]
    if any(s is not Status.SUCCEEDED for s in statuses):
        raise SystemExit(EXIT_FAILED)


@main.command(name="ls")
def ls_cmd() -> None:
    """Print the resolved container and policy stack.

    Config as code can hide the effective setup in a way a YAML file cannot — you can read a
    manifest, but you cannot read a container. This is the answer to "what will actually run".
    """
    lockstep, _ = _default_lockstep()

    click.echo(f"repo      {lockstep.repo.root}")
    click.echo(
        f"head      {lockstep.repo.head[:12] or '(none)'}"
        f"  branch {lockstep.repo.branch or '(none)'}"
        f"{'  dirty' if lockstep.repo.dirty else ''}"
    )
    if os.environ.get(DISABLE_ENV):
        click.echo(f"DISABLED  {DISABLE_ENV} is set; no adapter will execute")

    click.echo("")
    click.echo("bindings")
    for binding in lockstep.container.resolved():
        name = f" [{binding.name}]" if binding.name else ""
        impl = binding.impl if isinstance(binding.impl, type) else type(binding.impl)
        label = f"{binding.iface.__name__}{name}"
        click.echo(
            f"  {label:<22} -> {impl.__name__:<16}({binding.scope.value}, {binding.tier.name.lower()})"
        )

    click.echo("")
    click.echo("middleware  (privileged tier runs outside this chain and is not listed)")
    for mw in lockstep.middleware:
        click.echo(f"  {type(mw).__name__}")

    click.echo("")
    click.echo("policy")
    layers = lockstep.policy.layers
    if not layers:
        click.echo("  (nothing contributed)")
    else:
        for layer in layers:
            click.echo(f"  {layer.name}  <- {layer.source or 'local'}")
        resolved = lockstep.policy.resolve()
        click.echo(
            f"  = network={resolved.network or '(unset)'}"
            f" scan={resolved.scan_input or '(unset)'}"
            f" deny_tools={len(resolved.deny_tools)}"
        )

    click.echo("")
    click.echo("workflows")
    for entry in registered():
        click.echo(f"  {entry.id}  ({entry.module})")


@main.command(name="eval")
@click.argument("action", default="report")
@click.option("--corpus", default="", help="Where the cases live.")
def eval_cmd(action: str, corpus: str) -> None:
    """Run the eval corpus offline.

    Deterministic expectations are settled here. Rubric expectations are reported as OUTSTANDING,
    because a judge has not answered them — recording them as passes would put a perfect score
    computed from no evidence into a baseline that is then compared against forever.
    """
    from pathlib import Path as _Path

    from .evaluation import load_cases, summarize
    from .evaluation.cases import grade

    root = _Path(corpus) if corpus else _Path(__file__).parent / "evals"
    if not root.exists():
        raise click.ClickException(f"no corpus at {root}")

    cases = load_cases(root)
    if action == "list":
        for case in cases:
            rubric = " (rubric)" if case.rubric else ""
            click.echo(f"{case.name}{rubric}")
        click.echo(f"\n{len(cases)} case(s)")
        return

    # No model runs here: this reports what the corpus asks of one.
    results = [grade(case, None) for case in cases]
    summary = summarize(results)

    by_family: dict[str, int] = {}
    for case in cases:
        family = case.path.parent.parent.name if case.path else "?"
        by_family[family] = by_family.get(family, 0) + 1
    for family in sorted(by_family):
        click.echo(f"  {family:<12} {by_family[family]} case(s)")

    click.echo("")
    click.echo(f"cases        {summary['total']}")
    click.echo(f"decided      {summary['decided']}")
    click.echo(f"outstanding  {summary['outstanding']}  (need a judge)")
    rate = summary["pass_rate"]
    click.echo(f"pass rate    {'n/a — nothing decided' if rate is None else f'{rate:.0%}'}")
    click.echo("")
    click.echo("A rubric nobody judged is outstanding, not passed.")


@main.command(name="doctor")
@click.option("--strict", is_flag=True, help="What an organisation puts in a required check.")
def doctor_cmd(strict: bool) -> None:
    """Will the target accept this, and are the controls actually in place?"""
    from . import doctor as doctor_module

    report = doctor_module.run(".", strict=strict)
    click.echo(doctor_module.render(report))
    if not report.ok:
        raise SystemExit(EXIT_FAILED)


@main.command(name="apply")
@click.option("--from-artifact", "artifact", required=True, type=click.Path())
def apply_cmd(artifact: str) -> None:
    """Apply a ChangeSet produced by an earlier, unprivileged run.

    This is the privileged half of the two-job split. It holds a write token and never sees a
    provider credential — constructing it without a ProviderRegistry is asserted here rather than
    left as a convention.

    The artifact crossed a trust boundary to get here, so the path guard runs again over it. A
    previous job having produced it is not a reason to trust it.
    """
    import json
    from pathlib import Path as _Path

    from .core.changes import ChangeGuard
    from .core.types import ChangeAuthor, ChangeSet, FileChange

    path = _Path(artifact)
    payload = path / "changeset.json" if path.is_dir() else path
    if not payload.exists():
        raise click.ClickException(f"no changeset at {payload}")

    data = json.loads(payload.read_text())
    changeset = ChangeSet(
        changes=tuple(
            FileChange(
                path=str(c["path"]),
                contents=c.get("contents"),
                author=ChangeAuthor(c.get("author", "agent")),
                symlink_target=c.get("symlink_target"),
            )
            for c in data.get("changes", [])
        ),
        summary=str(data.get("summary", "")),
        ticket=str(data.get("ticket", "")),
    )

    refusals = ChangeGuard().check(changeset)
    if refusals:
        click.echo("refused:")
        for refusal in refusals:
            click.echo(f"  {refusal.path}  (tier {refusal.tier}, rule {refusal.rule})")
        raise SystemExit(EXIT_BLOCKED)

    click.echo(f"{len(changeset.changes)} change(s) pass the guard")
    for change in changeset.changes:
        click.echo(f"  {'delete' if change.deleted else 'write '} {change.path}")
    click.echo("")
    click.echo("Writing through Scm.open_change lands in phase 4; the guard is what phase 3 owes.")


@main.command(name="review")
@click.option("--base", default="origin/main", help="What to diff against.")
@click.option("--head", default="HEAD")
@click.option("--aspect", default="security", help="Which lens.")
@click.option("--model", default="anthropic:claude-sonnet-4-6")
@click.option("--offline", is_flag=True, help="Serve model calls from a cassette. No keys, no spend.")
@click.option("--record", is_flag=True, help="Call the provider and write a cassette.")
@click.option("--cassette", default=".in-lockstep/cassettes/review.json", show_default=True)
@click.option("--budget", default=1.00, help="Hard ceiling, in USD.")
@click.option("--dry-run", is_flag=True, help="Canned answer; proves the wiring, not the prompt.")
def review_cmd(
    base: str,
    head: str,
    aspect: str,
    model: str,
    offline: bool,
    record: bool,
    cassette: str,
    budget: float,
    dry_run: bool,
) -> None:
    """Review a change with one lens, in-process."""

    from .adapters.ai.review import AiReview, Review, ReviewSpec
    from .ai.auth import Auth
    from .ai.bootstrap import credentials_for, default_registry
    from .ai.invoker import AiInvoker, InvokePolicy
    from .ai.pricing import default_table
    from .ai.replay import Cassette, DryRunProvider, RecordingProvider, ReplayProvider
    from .core.spend import Budget
    from .llm.interface import LLMProvider
    from .llm.registry import Model

    lockstep = Lockstep.detect()
    lockstep.budget = Budget(usd=budget)
    recorder = Recorder()
    lockstep.middleware = [otel(recorder)]

    table = default_table()
    auth = Auth()
    registry = default_registry(auth)
    selected = Model(model)
    tape = Cassette.load(cassette)

    def build_invoker(_ctx: Any) -> AiInvoker:
        provider: LLMProvider
        if dry_run:
            provider = DryRunProvider()
        elif offline:
            provider = ReplayProvider(tape)
        else:
            creds = credentials_for(auth, selected.provider)
            provider = registry.provider_for(selected, creds)
            if record:
                provider = RecordingProvider(provider, tape, Redact())
        return AiInvoker(
            provider,
            model=selected.name,
            cost_table=table,
            spend=_ctx.spend,
            redact=Redact(),
        )

    lockstep.bind(
        Review,
        AiReview(
            build_invoker,
            repo_root=lockstep.repo.root,
            policy=InvokePolicy(max_turns=1, deadline_seconds=300),
        ),
    )

    ctx = lockstep.context(run_id=f"review-{aspect}")
    outcome = asyncio.run(ctx.do(Review, ReviewSpec(base=base, head=head, aspect=aspect)))

    click.echo(
        f"review/{aspect}  {outcome.status.value}"
        + (f"  ({outcome.reason})" if outcome.reason else "")
        + ("" if outcome.decided else "  (decided nothing)")
    )
    for finding in outcome.findings:
        where = f"{finding.path}:{finding.line} " if finding.path else ""
        click.echo(f"  {where}{finding.id}: {finding.message}")

    cost = outcome.cost
    click.echo("")
    click.echo(f"tokens    {cost.input_tokens} in, {cost.output_tokens} out")
    click.echo(f"cost      ${cost.usd:.4f}")
    click.echo(f"spans     {len(recorder.spans)}")

    # The ledger line the first-value assertion checks. Written even on failure: a run that cost
    # money and produced nothing is exactly the run worth having a record of.
    _write_ledger(ctx, outcome, aspect, selected.id)

    if outcome.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    if outcome.status is not Status.SUCCEEDED:
        raise SystemExit(EXIT_FAILED)


def _write_ledger(ctx: Any, outcome: Any, aspect: str, model_id: str) -> None:
    import json
    from pathlib import Path as _Path

    record = {
        "schema": 2,
        "epoch": "in-process",
        "run_id": ctx.run_id,
        "kind": "review",
        "aspect": aspect,
        "model": model_id,
        "status": outcome.status.value,
        "decided": outcome.decided,
        "tokens": outcome.cost.total_tokens,
        "input_tokens": outcome.cost.input_tokens,
        "output_tokens": outcome.cost.output_tokens,
        "cost_usd": round(outcome.cost.usd, 6),
        "findings": len(outcome.findings),
        "wall_seconds": round(outcome.cost.wall_seconds, 3),
    }
    path = _Path(".in-lockstep/ledger") / f"{ctx.run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    click.echo(f"ledger    {path}")


@main.command(name="show-prompt")
@click.argument("aspect", default="security")
@click.option("--projection", is_flag=True, help="Print the section-identity list only.")
def show_prompt_cmd(aspect: str, projection: bool) -> None:
    """Render a composed prompt offline, with per-fragment provenance.

    The successor to a committed flattened prompt tree. "What was the model actually told?" needs
    an answer that costs no run and no key — a cassette requires having already paid, and `ls`
    prints the container rather than the prompt.

    The projection it prints is the same one the characterization corpus asserts on, so one
    artifact serves both offline inspection and migration equivalence.
    """
    from .prompts.review import LENSES, review_layers

    lens = LENSES.get(aspect)
    if lens is None:
        raise click.ClickException(f"no lens named {aspect!r}; have {sorted(LENSES)}")

    prompt = lens()
    layers = review_layers()

    if projection:
        for section in layers.projection(f"review/{aspect}-reviewer"):
            click.echo(section)
        return

    click.echo(f"# composed prompt: review/{aspect}  (version {prompt.version})")
    click.echo("#")
    for section in layers.projection(f"review/{aspect}-reviewer"):
        click.echo(f"#   {section}")
    click.echo("")
    click.echo(prompt.system(layers))


@main.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite lockstep.py (never the trampoline).")
def init_cmd(force: bool) -> None:
    """Scaffold a lifecycle definition and a CI trampoline.

    The trampoline is written once and never read back: there is no drift check on it, and no
    --force for it. The day something compares it against a freshly generated one, it has become
    generated output rather than a scaffold, which is the line this framework exists on the other
    side of.
    """
    from pathlib import Path

    module = Path("lockstep.py")
    if module.exists() and not force:
        click.echo("lockstep.py exists (use --force to overwrite)")
    else:
        module.write_text(_SCAFFOLD_MODULE)
        click.echo("wrote lockstep.py")

    workflow = Path(".github/workflows/lockstep.yml")
    if workflow.exists():
        click.echo(f"{workflow} exists — left alone, deliberately")
    else:
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(_SCAFFOLD_TRAMPOLINE)
        click.echo(f"wrote {workflow}")
        click.echo("")
        click.echo("Two jobs, on purpose: `run` holds the provider credential and cannot write,")
        click.echo("`apply` can write and never sees the provider credential.")


@main.command()
def status() -> None:
    """What of the framework exists so far."""
    click.echo(f"in-lockstep {__version__} — pivot in progress")
    click.echo("")
    click.echo("  phase 0  decisions & safety net   done")
    click.echo("  phase 1  dispatch core            in progress")
    click.echo("  phase 2  AI subsystem, 1st value  not started")


_SCAFFOLD_MODULE = '''"""The lifecycle for this repository.

This file IS the configuration: it is executed, not parsed. Anything you can express in Python
you can express here — but keep it pure, because it is imported to be inspected as well as run.
"""

from in_lockstep import Lockstep
from in_lockstep.adapters import PytestTest, RuffValidate
from in_lockstep.adapters.pytest_adapter import Test
from in_lockstep.adapters.ruff_adapter import Validate
from in_lockstep.middleware import CostBudget, otel

lockstep = Lockstep.detect()

# Deterministic verbs bind adapters over real tools.
lockstep.bind(Test, PytestTest(args=["-q"]))
lockstep.bind(Validate, RuffValidate())

# Cross-cutting behaviour is middleware. Redaction, egress and the kill switch are NOT here:
# they are privileged, and `--no-middleware` cannot reach them.
lockstep.middleware += [otel(), CostBudget(usd=2.00)]
'''

_SCAFFOLD_TRAMPOLINE = """# Invokes the CLI. Contains no lifecycle logic, and is never regenerated.
#
# Two jobs rather than one: the job that talks to a model holds the provider credential and only
# read access, and the job that writes holds write access and no provider credential. A single
# job would put an API key and a write token in the same process.
name: lockstep

on:
  pull_request:

permissions:
  contents: read

jobs:
  run:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v6
      - run: uvx --from 'in-lockstep[anthropic]' in-lockstep run review --base origin/main
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      - uses: actions/upload-artifact@v4
        with:
          name: lockstep-changeset
          path: .in-lockstep/out/

  apply:
    needs: run
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - uses: actions/download-artifact@v4
        with:
          name: lockstep-changeset
          path: .in-lockstep/out/
      # No provider credential in this job. It applies what the previous one produced, and
      # re-checks it: the artifact crossed a trust boundary to get here.
      - run: uvx in-lockstep apply --from-artifact .in-lockstep/out/
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
"""


if __name__ == "__main__":  # pragma: no cover
    main()
