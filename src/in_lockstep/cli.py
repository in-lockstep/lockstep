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

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 3


def _default_lockstep() -> tuple[Lockstep, Recorder]:
    """The zero-config binding set. A real repo overrides any line of this in its own module."""
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
def run_cmd(target: str, paths: tuple[str, ...], no_middleware: bool) -> None:
    """Run a workflow."""
    if target != "selfcheck":
        known = ", ".join(r.id for r in registered())
        raise click.ClickException(f"unknown workflow {target!r}; registered: {known}")

    lockstep, recorder = _default_lockstep()
    if no_middleware:
        # Note what this does NOT switch off: the kill switch is checked before the chain, and
        # redaction, egress and residency are privileged rather than middleware.
        lockstep.middleware = []

    ctx = lockstep.context(run_id="selfcheck-local")
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


@main.command()
def status() -> None:
    """What of the framework exists so far."""
    click.echo(f"in-lockstep {__version__} — pivot in progress")
    click.echo("")
    click.echo("  phase 0  decisions & safety net   done")
    click.echo("  phase 1  dispatch core            in progress")
    click.echo("  phase 2  AI subsystem, 1st value  not started")


if __name__ == "__main__":  # pragma: no cover
    main()
