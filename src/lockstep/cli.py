"""Command-line interface."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from . import __version__
from .emit import compile_spec, show_surface
from .emit import semantic_diff as sd
from .emit.writer import check_plan, write_plan
from .errors import LockstepError

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
@click.option("--show-surface", "want_surface", is_flag=True, help="Print the GitHub surface and exit.")
@click.option("--no-prune", is_flag=True, help="Keep generated files that are no longer produced.")
def compile_cmd(
    root: Path,
    target: str,
    check: bool,
    want_diff: bool,
    want_surface: bool,
    no_prune: bool,
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

    diff = sd.against_disk(root, plan) if want_diff else None

    if check:
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
        sys.exit(EXIT_OK if report.clean else EXIT_DRIFT)

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
