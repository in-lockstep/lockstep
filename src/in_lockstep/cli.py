"""The `in-lockstep` command.

Phase 0 ships the entry point and nothing else — the real surface (`run`, `ls`, `doctor`,
`show-prompt`, `init`) lands in Phases 1 and 2. It exists now because the console script is
declared now: both `lockstep` and `in-lockstep` coexist through the pivot, and only this one
survives the decommission.
"""

from __future__ import annotations

import click

from . import __version__


@click.group()
@click.version_option(__version__, prog_name="in-lockstep")
def main() -> None:
    """Run your lifecycle. (Under construction — see design/adr/0001.)"""


@main.command()
def status() -> None:
    """What of the framework exists so far."""
    click.echo(f"in-lockstep {__version__} — pivot in progress")
    click.echo("")
    click.echo("  phase 0  decisions & safety net   in progress")
    click.echo("  phase 1  dispatch core            not started")
    click.echo("  phase 2  AI subsystem, 1st value  not started")
    click.echo("")
    click.echo("The compiler (`lockstep`) still ships and still works; it is deleted in phase 7.")


if __name__ == "__main__":  # pragma: no cover
    main()
