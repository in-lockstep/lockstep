"""Command-line interface."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import click

from . import __version__
from .emit import compile_spec, show_surface
from .emit import semantic_diff as sd
from .emit.writer import check_plan, write_plan
from .errors import LockstepError

if TYPE_CHECKING:
    from .checks import Report
    from .spec.model import Spec

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_SPEC = 2

root_option = click.option(
    "--root",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Pipeline repo root (the directory holding pipeline.yaml).",
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="lockstep")
def main() -> None:
    """Compile pipeline definitions into GitHub Agentic Workflows."""


@main.command(name="compile")
@root_option
@click.option("--target", default="github-agentic", show_default=True, help="Compilation target.")
@click.option("--check", "check", is_flag=True, help="Drift gate: verify committed output matches.")
@click.option("--semantic-diff", "want_diff", is_flag=True, help="Report security/cost surface deltas.")
@click.option(
    "--base",
    default="",
    help="Compare the surface against this git ref instead of the working tree. In CI this is the "
    "branch being merged into — the only comparison that shows what merging would change.",
)
@click.option("--show-surface", "want_surface", is_flag=True, help="Print the GitHub surface and exit.")
@click.option("--no-prune", is_flag=True, help="Keep generated files that are no longer produced.")
@click.option(
    "--fail-on-blocking",
    is_flag=True,
    help="Fail when the semantic diff shows a security-surface change that was not acknowledged.",
)
def compile_cmd(
    root: Path,
    target: str,
    check: bool,
    want_diff: bool,
    want_surface: bool,
    no_prune: bool,
    fail_on_blocking: bool,
    base: str,
) -> None:
    """Compile the spec in ROOT into workflows."""
    if target != "github-agentic":
        raise click.BadParameter(f"unknown target {target!r}", param_hint="--target")

    if want_surface:
        click.echo(show_surface.render(root))
        return

    try:
        plan = compile_spec(root)
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)

    for line in plan.summaries:
        click.echo(line)
    for note in plan.notes:
        click.echo(click.style(f"note: {note}", fg="yellow"))

    diff = None
    if want_diff or fail_on_blocking:
        diff = sd.against_ref(root, plan, base) if base else sd.against_disk(root, plan)

    if check:
        _report_lifecycle(root, plan)
        report = check_plan(root, plan)
        if report.clean:
            click.echo(click.style("drift gate: clean", fg="green"))
        else:
            for path in report.missing:
                click.echo(click.style(f"missing:  {path}", fg="red"), err=True)
            for path in report.modified:
                click.echo(click.style(f"modified: {path}", fg="red"), err=True)
            for path in report.orphaned:
                click.echo(click.style(f"orphaned: {path}", fg="red"), err=True)
            click.echo(
                click.style(
                    "drift gate: committed output does not match a fresh compile — run `lockstep compile`",
                    fg="red",
                ),
                err=True,
            )
        _emit_diff(diff)
        blocked = fail_on_blocking and diff is not None and diff.blocking
        sys.exit(EXIT_OK if report.clean and not blocked else EXIT_DRIFT)

    written = write_plan(root, plan, prune=not no_prune)
    for path in written.created:
        click.echo(f"  + {path}")
    for path in written.updated:
        click.echo(f"  ~ {path}")
    for path in written.removed:
        click.echo(f"  - {path}")
    click.echo(
        f"wrote {len(written.created) + len(written.updated)} files "
        f"({len(written.unchanged)} unchanged, {len(written.removed)} pruned)"
    )
    click.echo("next: run `gh aw compile` to produce the .lock.yml files the orchestrators call")
    _emit_diff(diff)


def _report_lifecycle(root: Path, plan: object) -> None:
    """Surface the two things that go stale without anyone editing anything."""
    from .lifecycle import stale_ejections

    files = getattr(plan, "files", {})
    for relative in stale_ejections(root, files):
        click.echo(
            click.style(
                f"stale eject: {relative} has forked from a generation that has since changed",
                fg="yellow",
            )
        )


def _emit_diff(diff: sd.SemanticDiff | None) -> None:
    if diff is None:
        return
    click.echo("")
    click.echo("semantic diff (security and cost surface):")
    click.echo(diff.render())
    if diff.blocking:
        click.echo(
            click.style(
                f"{len(diff.blocking)} blocking delta(s) — these require explicit acknowledgment",
                fg="yellow",
            )
        )


def _run_checks(root: Path, report_fn: Callable[[Spec], Report], title: str, strict: bool) -> None:
    from .spec.load import load_spec

    try:
        spec = load_spec(root)
        report = report_fn(spec)
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)

    click.echo(f"{title}:")
    click.echo(report.render())
    errors, warnings = len(report.errors), len(report.warnings)
    click.echo(f"{errors} error(s), {warnings} warning(s)")
    if errors or (strict and warnings):
        sys.exit(EXIT_DRIFT)


@main.command()
@root_option
@click.option("--strict", is_flag=True, help="Treat warnings as failures.")
def lint(root: Path, strict: bool) -> None:
    """Check that the spec is well built: evals, tests, and deterministic-first."""
    from .checks import lint as run_lint

    _run_checks(root, run_lint, "lint", strict)


@main.command()
@root_option
@click.option("--target", default="github-agentic", show_default=True)
@click.option("--strict", is_flag=True, help="Treat warnings as failures.")
def doctor(root: Path, target: str, strict: bool) -> None:
    """Check that the target will accept this pipeline: pins, secrets, budgets, permissions."""
    from .checks import doctor as run_doctor

    if target != "github-agentic":
        raise click.BadParameter(f"unknown target {target!r}", param_hint="--target")
    _run_checks(root, lambda spec: run_doctor(spec, root), "doctor", strict)


@main.command()
@root_option
@click.option("--sha", "actions_sha", default="", help="Pin the capability actions to this commit.")
@click.option("--exec-digest", default="", help="Pin the executor image to this digest.")
@click.option("--offline", is_flag=True, help="Do not contact any remote; only apply what is given.")
def pin(root: Path, actions_sha: str, exec_digest: str, offline: bool) -> None:
    """Resolve capability tags into the commits and digests that will actually run."""
    from .lifecycle import pin as resolve_pins
    from .lifecycle import write_pins
    from .spec.load import load_spec

    try:
        data, notes = resolve_pins(
            load_spec(root), root, actions_sha=actions_sha, exec_digest=exec_digest, offline=offline
        )
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)

    path = write_pins(root, data)
    for note in notes:
        click.echo(f"  {note}")
    click.echo(f"wrote {path.relative_to(root)}")


@main.command()
@root_option
@click.argument("target")
def eject(root: Path, target: str) -> None:
    """Take ownership of one generated file. The compiler will stop maintaining it."""
    from .lifecycle import eject as do_eject

    try:
        plan = compile_spec(root)
        if target not in plan.files:
            raise LockstepError(
                f"{target!r} is not generated by this pipeline",
                hint="eject applies to generated files; anything else is already yours",
            )
        base = do_eject(root, target, plan.files[target])
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)

    click.echo(f"ejected {target}")
    click.echo(f"  the generation it forked from is kept at {base.relative_to(root)}")
    click.echo("  `lockstep compile --check` will report when its source moves on without it")


@main.command()
@root_option
@click.argument("target")
def uneject(root: Path, target: str) -> None:
    """Hand a file back to the compiler, discarding local changes to it."""
    from .lifecycle import uneject as do_uneject

    try:
        do_uneject(root, target)
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)
    click.echo(f"{target} is generated again; run `lockstep compile` to restore it")


@main.command(name="show-surface")
@root_option
def show_surface_cmd(root: Path) -> None:
    """Render every GitHub-target decision as one document."""
    try:
        click.echo(show_surface.render(root))
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)


if __name__ == "__main__":
    main()
