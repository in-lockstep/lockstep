"""Third-party commands.

A pipeline usually needs work this package will never ship — fetching from your issue tracker,
running your project's test suite, applying a patch the way your repository expects. Rather than
forking, a package advertises commands through an entry point and they appear in the CLI:

    [project.entry-points."pipeline_exec.commands"]
    jira-fetch = "my_extension.commands:jira_fetch"

The value is a `click.Command`. Its name in the entry point is the name a `builtin:` step uses, so
the spec never has to know which package a command came from.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import click

GROUP = "pipeline_exec.commands"


class PluginError(Exception):
    """A declared extension could not be loaded. Never silent: a missing command fails a run."""


def discover() -> dict[str, click.Command]:
    """Load every command third-party packages contribute, keyed by the name a spec would use."""
    found: dict[str, click.Command] = {}
    for entry in entry_points(group=GROUP):
        try:
            command = entry.load()
        except Exception as error:  # noqa: BLE001 - a broken plugin must name itself
            raise PluginError(f"extension {entry.name!r} failed to load: {error}") from error
        if not isinstance(command, click.Command):
            raise PluginError(f"extension {entry.name!r} is {type(command).__name__}, not a click.Command")
        found[entry.name] = command
    return found


def register(group: click.Group) -> list[str]:
    """Attach discovered commands, refusing to let an extension shadow a built-in one."""
    names: list[str] = []
    for name, command in sorted(discover().items()):
        if name in group.commands:
            raise PluginError(
                f"extension {name!r} would shadow a built-in command; rename it in your entry points"
            )
        group.add_command(command, name=name)
        names.append(name)
    return names
