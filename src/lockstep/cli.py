"""Command-line interface."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import click

from . import __version__, ghaw
from .emit import compile_spec, show_surface
from .emit import semantic_diff as sd
from .emit.writer import check_plan, write_plan
from .errors import LockstepError

if TYPE_CHECKING:
    from .checks import Report
    from .emit.plan import CompilePlan
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

    from .spec.load import load_spec

    try:
        plan = compile_spec(root)
        spec = load_spec(root)
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
        try:
            locks = _check_locks(root, spec, plan)
        except LockstepError as error:
            click.echo(click.style(error.render(), fg="red"), err=True)
            sys.exit(EXIT_SPEC)
        _emit_diff(diff)
        blocked = fail_on_blocking and diff is not None and diff.unacknowledged
        sys.exit(EXIT_OK if report.clean and locks and not blocked else EXIT_DRIFT)

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
    try:
        _write_locks(root, spec, plan)
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)
    _emit_diff(diff)


def _workflows_dir(root: Path, spec: Spec) -> Path:
    return root / spec.manifest.target.out


def _prune_orphan_locks(workflows: Path) -> list[str]:
    """A lock file whose agent is gone is a workflow nobody generates and nothing can regenerate."""
    removed = []
    for lock in sorted(workflows.glob(f"*{ghaw.LOCK_SUFFIX}")):
        if not (workflows / (lock.name.removesuffix(ghaw.LOCK_SUFFIX) + ".md")).is_file():
            lock.unlink()
            removed.append(lock.name)
    return removed


def _write_locks(root: Path, spec: Spec, plan: CompilePlan) -> None:
    """Produce the `.lock.yml` files the orchestrators call.

    Part of compiling rather than a step afterwards: an orchestrator emitted by this compiler names
    these files, so a compile that does not produce them has emitted a workflow GitHub will reject.
    """
    workflows = _workflows_dir(root, spec)
    for name in _prune_orphan_locks(workflows):
        click.echo(f"  - {spec.manifest.target.out}/{name}")
    if not plan.stats.get("agentic"):
        return

    ghaw.require(spec.manifest.capabilities.gh_aw, cwd=root)
    produced = ghaw.compile_locks(workflows)
    changed = 0
    for name, content in produced.items():
        target = workflows / name
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            continue
        target.write_text(content, encoding="utf-8")
        click.echo(f"  ~ {spec.manifest.target.out}/{name}")
        changed += 1
    click.echo(f"gh-aw: {len(produced)} lock file(s), {changed} changed")


def _check_locks(root: Path, spec: Spec, plan: CompilePlan) -> bool:
    """Regenerate the lock files from the committed markdown and compare.

    This is the half of the gate that covers what actually runs. `gh aw` missing is a failure, not
    a skip — a check that could not look at the artifact has not checked it, and reporting success
    would be the most expensive kind of green.
    """
    workflows = _workflows_dir(root, spec)
    orphans = [
        lock.name
        for lock in sorted(workflows.glob(f"*{ghaw.LOCK_SUFFIX}"))
        if not (workflows / (lock.name.removesuffix(ghaw.LOCK_SUFFIX) + ".md")).is_file()
    ]
    if not plan.stats.get("agentic") and not orphans:
        return True

    ghaw.require(spec.manifest.capabilities.gh_aw, cwd=root)
    produced = ghaw.compile_locks(workflows)
    clean = True
    for name in orphans:
        click.echo(click.style(f"orphaned: {spec.manifest.target.out}/{name}", fg="red"), err=True)
        clean = False
    for name, content in sorted(produced.items()):
        target = workflows / name
        rel = f"{spec.manifest.target.out}/{name}"
        if not target.is_file():
            click.echo(click.style(f"missing:  {rel}", fg="red"), err=True)
            clean = False
        elif target.read_text(encoding="utf-8") != content:
            click.echo(click.style(f"modified: {rel}", fg="red"), err=True)
            clean = False
    if clean:
        click.echo(click.style(f"gh-aw: {len(produced)} lock file(s) match", fg="green"))
    else:
        click.echo(
            click.style(
                "the workflows that actually run do not match the agents they came from — "
                "run `lockstep compile`",
                fg="red",
            ),
            err=True,
        )
    return clean


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

    for category in diff.stale_acknowledgements:
        click.echo(
            click.style(
                f"note: `Security-Surface: {category}` acknowledges a category this change does not "
                "touch — an acknowledgment carried forward stops meaning anything",
                fg="yellow",
            )
        )

    outstanding = diff.unacknowledged
    if not outstanding:
        if diff.blocking:
            click.echo(
                click.style(
                    f"{len(diff.blocking)} security-surface delta(s), all acknowledged", fg="green"
                )
            )
        return

    categories = ", ".join(sorted({delta.category for delta in outstanding}))
    click.echo(
        click.style(
            f"{len(outstanding)} unacknowledged security-surface delta(s).\n"
            "Acknowledge them in a commit message on this branch, naming what moved:\n"
            f"\n    Security-Surface: {categories}\n\n"
            "The trailer stays in the history as the answer to why this pipeline has that surface.",
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


@main.command(name="fetch")
@root_option
def fetch_command(root: Path) -> None:
    """Materialize everything this pipeline inherits, at the commits its lock file records."""
    from .lifecycle import fetch as do_fetch
    from .spec.load import load_manifest_only

    try:
        notes = do_fetch(load_manifest_only(root), root)
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)

    if not notes:
        click.echo("nothing inherited")
        return
    for note in notes:
        click.echo(f"  {note}")
    click.echo(f"fetched {len(notes)} upstream(s)")


@main.command()
@root_option
@click.option("--sha", "actions_sha", default="", help="Pin the capability actions to this commit.")
@click.option("--exec-digest", default="", help="Pin the executor image to this digest.")
@click.option("--offline", is_flag=True, help="Do not contact any remote; only apply what is given.")
def pin(root: Path, actions_sha: str, exec_digest: str, offline: bool) -> None:
    """Resolve capability tags into the commits and digests that will actually run."""
    from .lifecycle import pin as resolve_pins
    from .lifecycle import write_pins
    from .spec.load import load_manifest_only

    try:
        data, notes, unresolved = resolve_pins(
            load_manifest_only(root),
            root,
            actions_sha=actions_sha,
            exec_digest=exec_digest,
            offline=offline,
        )
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)

    path = write_pins(root, data)
    for note in notes:
        click.echo(f"  {note}")
    click.echo(f"wrote {path.relative_to(root)}")
    for problem in unresolved:
        click.echo(click.style(f"  unresolved: {problem}", fg="yellow"), err=True)
    if unresolved:
        click.echo(
            click.style(
                f"{len(unresolved)} reference(s) still unpinned; `lockstep compile` will refuse to emit them",
                fg="yellow",
            ),
            err=True,
        )
        sys.exit(EXIT_DRIFT)


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


@main.command()
@click.option(
    "--dir",
    "root",
    default=".",
    type=click.Path(file_okay=False, path_type=Path),
    help="Where to create the pipeline.",
)
@click.option("--name", required=True, help="Pipeline name; also the name of its first command.")
@click.option("--profile", default="staging", show_default=True, help="Name of the first profile.")
@click.option("--target", default="github-agentic", show_default=True)
@click.option("--force", is_flag=True, help="Overwrite files that already exist.")
@click.option(
    "--adopt",
    default="",
    help="Comma-separated shipped pipelines to inherit instead of authoring one. `all` takes every one.",
)
def init(root: Path, name: str, profile: str, target: str, force: bool, adopt: str) -> None:
    """Scaffold a new pipeline repository.

    With `--adopt`, scaffolds a repository that *runs* the pipelines this compiler ships rather than
    one that defines its own — a manifest and a profile, and nothing else to write. They are
    inherited rather than copied, so tuning one, overlaying its steps, or adding a pipeline of your
    own beside them are all things that come later without giving anything up.
    """
    from . import library
    from .scaffold import scaffold

    if target != "github-agentic":
        raise click.BadParameter(f"unknown target {target!r}", param_hint="--target")

    chosen: tuple[str, ...] = ()
    if adopt:
        chosen = (
            tuple(sorted(library.pipelines()))
            if adopt.strip() == "all"
            else tuple(part.strip() for part in adopt.split(",") if part.strip())
        )
        if not chosen:
            raise click.BadParameter("no pipelines ship with this compiler", param_hint="--adopt")

    root.mkdir(parents=True, exist_ok=True)
    try:
        written = scaffold(root, name, profile, force=force, adopt=chosen)
    except LockstepError as error:
        click.echo(click.style(error.render(), fg="red"), err=True)
        sys.exit(EXIT_SPEC)

    for relative in written:
        click.echo(f"  + {relative}")
    click.echo(f"\ncreated {len(written)} files in {root}")
    click.echo("\nnext:")
    click.echo(f"  cd {root}")
    click.echo("  lockstep pin        # resolve capability tags to commits")
    # Whenever the manifest inherits anything, not only for `--adopt`. The scaffolded pipeline
    # inherits the retro, so `compile` refuses until its definitions are on disk — and for a
    # `lockstep:` upstream this copies from the installed compiler, so it is instant and offline.
    if chosen or "\ninherits:" in (root / "pipeline.yaml").read_text(encoding="utf-8"):
        click.echo("  lockstep fetch      # materialize the pipelines you inherited")
    click.echo("  lockstep compile    # generate the workflows")
    click.echo("  lockstep lint       # check the spec")


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
