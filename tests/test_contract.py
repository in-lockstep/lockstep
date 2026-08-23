"""The contract between the compiler and the runtime it emits calls to.

The compiler writes `pipeline-exec …` invocations as literal text into committed workflows, so a
renamed flag would not fail until a scheduled run at 2am. These tests parse every emitted invocation
against the real CLI. This is the reason both packages live in one repository.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import pytest
import yaml
from pipeline_exec import items as exec_items
from pipeline_exec.cli import main as exec_cli

from lockstep.emit import compile_spec
from lockstep.emit.builtins import AVAILABLE, MATRIX_CAP

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
REDIRECTION = re.compile(r"\s*>>?\s*\"?\$GITHUB_OUTPUT\"?\s*$")


def emitted_invocations() -> list[tuple[str, str]]:
    """Every `pipeline-exec …` command the compiler writes, with the workflow it came from."""
    found: list[tuple[str, str]] = []
    for path, text in compile_spec(FIXTURE).files.items():
        if not path.endswith(".yml"):
            continue
        data = yaml.safe_load(text) or {}
        for job in (data.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run = step.get("run", "")
                for line in run.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("pipeline-exec "):
                        found.append((path, stripped))
    return found


def parse(invocation: str) -> click.Context:
    """Parse without executing: Click validates flags and required options in make_context."""
    command = REDIRECTION.sub("", invocation)
    # Runtime expressions stand in for values the compiler cannot know; their content is irrelevant
    # to whether the flags parse.
    command = EXPRESSION.sub("EXPR", command)
    argv = shlex.split(command)[1:]
    name, rest = argv[0], argv[1:]
    subcommand = exec_cli.commands.get(name)
    assert subcommand is not None, f"pipeline-exec has no command {name!r}"
    return subcommand.make_context(name, rest, parent=click.Context(exec_cli))


def test_the_fixture_emits_invocations_worth_checking():
    assert len(emitted_invocations()) >= 3


@pytest.mark.parametrize(
    ("source", "invocation"),
    emitted_invocations(),
    ids=lambda value: value if isinstance(value, str) and " " not in value else None,
)
def test_every_emitted_invocation_parses_against_the_real_cli(source, invocation):
    parse(invocation)


def test_emitted_fanout_flags_are_the_ones_fanout_declares():
    invocation = next(cmd for _, cmd in emitted_invocations() if cmd.startswith("pipeline-exec fanout "))
    context = parse(invocation)
    assert context.params["key_field"] == "key"
    assert context.params["max_items"] == MATRIX_CAP


def test_agent_fan_out_forbids_sharding():
    """An agent leg is a whole gh-aw run; sharding would silently drop every item but the first."""
    agent_fanout = next(
        cmd
        for source, cmd in emitted_invocations()
        if "generate-tests" in source and cmd.startswith("pipeline-exec fanout ")
    )
    assert parse(agent_fanout).params["no_shard"] is True


def test_deterministic_fan_out_permits_sharding():
    script_fanout = next(
        cmd
        for source, cmd in emitted_invocations()
        if "repair" in source and cmd.startswith("pipeline-exec fanout ")
    )
    context = parse(script_fanout)
    assert context.params["no_shard"] is False
    assert context.params["shard_threshold"] > 0


def test_shard_run_receives_the_matrix_value_and_its_input():
    invocation = next(cmd for _, cmd in emitted_invocations() if cmd.startswith("pipeline-exec shard-run "))
    context = parse(invocation)
    assert context.params["slice_json"] == "EXPR"
    assert context.params["input_path"].name.endswith(".json")
    assert context.params["command"], "shard-run must be given a command to run"


def test_the_compilers_builtin_list_matches_the_runtime():
    assert AVAILABLE == set(exec_cli.commands)


def test_the_matrix_cap_agrees_across_both_packages():
    assert MATRIX_CAP == exec_items.MATRIX_CAP


def test_an_unknown_builtin_is_a_compile_error(basic_root):
    command = basic_root / "commands" / "discover.md"
    command.write_text(
        command.read_text().replace(
            "1. **Discover API surface** → script: scripts/discover-api.py",
            "1. **Discover API surface** → builtin: test-runner",
        )
    )
    from lockstep.errors import EmitError

    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "not provided by pipeline-exec" in excinfo.value.render()
