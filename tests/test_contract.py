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
from lockstep.emit.builtins import AVAILABLE, INTERNAL, MATRIX_CAP

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
# Every pipeline in the repository, not just the fixture. A referenced action that does not exist is
# invisible if only one pipeline is checked and that pipeline happens not to reference it.
EXAMPLES = sorted(
    path
    for path in (Path(__file__).parent.parent / "examples").iterdir()
    if (path / "pipeline.yaml").is_file()
)
ALL_PIPELINES = [FIXTURE, *EXAMPLES]
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
    """Every runtime command is either spec surface or declared plumbing — never unaccounted for."""
    assert AVAILABLE | INTERNAL == set(exec_cli.commands)
    assert not AVAILABLE & INTERNAL


def test_every_command_the_compiler_emits_is_classified():
    emitted = {invocation.split()[1] for _, invocation in emitted_invocations()}
    assert emitted <= AVAILABLE | INTERNAL


def test_the_matrix_cap_agrees_across_both_packages():
    assert MATRIX_CAP == exec_items.MATRIX_CAP


def test_the_profile_environment_the_compiler_exports_is_the_one_the_executors_read():
    """The executors' only configuration channel is the `PROFILE_*` block every job carries."""
    from pipeline_exec.config import PROFILE_KEYS

    from lockstep.emit.profiles import env_block
    from lockstep.spec.load import load_spec

    profile = load_spec(FIXTURE).profiles["my-app"]
    exported = set(env_block(profile))

    declared = {key for key in PROFILE_KEYS if key in profile.values}
    assert declared, "the fixture profile should declare keys the executors consume"
    for key in declared:
        assert f"PROFILE_{key.upper()}" in exported


def test_no_generated_workflow_plumbs_step_outputs_by_hand():
    """`pipeline-exec` writes to $GITHUB_OUTPUT itself; a redirect would double-write."""
    for _, invocation in emitted_invocations():
        assert "$GITHUB_OUTPUT" not in invocation


# --- the composite actions the compiler references -------------------------

ACTIONS_ROOT = Path(__file__).parent.parent / "actions"
# The capability actions ship from `actions/` in this repository, so a `uses:` naming them starts
# with this. An extension may publish composite actions of its own from its own path; those are the
# extension's contract to keep, not this one's.
CAPABILITY_PREFIX = "in-lockstep/lockstep/actions/"


def _walk_actions(node, used: dict, outputs_read: set) -> None:
    if isinstance(node, dict):
        ref = node.get("uses")
        # `<owner>/<repo>/actions/<name>@<sha>` — the composite actions ship from a subdirectory
        # of the repository that builds them, so one tag covers the action and its own tests.
        if isinstance(ref, str) and ref.startswith(CAPABILITY_PREFIX):
            name = ref[len(CAPABILITY_PREFIX) :].split("@")[0]
            passed, read = used.setdefault(name, (set(), set()))
            passed.update((node.get("with") or {}).keys())
            step_id = node.get("id")
            if step_id:
                read.update(out for sid, out in outputs_read if sid == step_id)
        for value in node.values():
            _walk_actions(value, used, outputs_read)
    elif isinstance(node, list):
        for value in node:
            _walk_actions(value, used, outputs_read)


def referenced_actions() -> list[tuple[str, dict, list[str]]]:
    """Every capability action the compiler emits, with the inputs and outputs it relies on."""
    used: dict[str, tuple[set, set]] = {}
    files = {path: text for root in ALL_PIPELINES for path, text in compile_spec(root).files.items()}
    for path, text in files.items():
        if path.endswith(".yml"):
            blob = text
        elif "/aw-" in path:
            match = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", text, re.DOTALL)
            blob = match.group(1) if match else ""
        else:
            continue
        outputs_read = set(re.findall(r"steps\.([\w-]+)\.outputs\.([\w-]+)", text))

        _walk_actions(yaml.safe_load(blob) or {}, used, outputs_read)
    return [(name, {"with": sorted(w)}, sorted(o)) for name, (w, o) in sorted(used.items())]


def test_every_pipeline_in_the_repository_is_checked():
    """The guard against this file quietly checking one pipeline while others drift."""
    assert len(ALL_PIPELINES) >= 4


def action_definition(name: str) -> dict:
    path = ACTIONS_ROOT / name / "action.yml"
    assert path.is_file(), f"the compiler references {name!r} but actions/{name}/action.yml is missing"
    return yaml.safe_load(path.read_text()) or {}


@pytest.mark.parametrize(
    ("name", "used", "outputs"), referenced_actions(), ids=lambda v: v if isinstance(v, str) else None
)
def test_referenced_actions_declare_what_the_compiler_passes(name, used, outputs):
    definition = action_definition(name)
    declared = set((definition.get("inputs") or {}).keys())
    undeclared = set(used["with"]) - declared
    assert not undeclared, f"{name} is passed {sorted(undeclared)}, which it does not declare"


@pytest.mark.parametrize(
    ("name", "used", "outputs"), referenced_actions(), ids=lambda v: v if isinstance(v, str) else None
)
def test_referenced_actions_declare_the_outputs_the_compiler_reads(name, used, outputs):
    definition = action_definition(name)
    declared = set((definition.get("outputs") or {}).keys())
    missing = set(outputs) - declared
    assert not missing, f"the compiler reads {sorted(missing)} from {name}, which it does not declare"


@pytest.mark.parametrize(
    ("name", "used", "outputs"), referenced_actions(), ids=lambda v: v if isinstance(v, str) else None
)
def test_required_action_inputs_are_all_supplied(name, used, outputs):
    definition = action_definition(name)
    required = {
        key
        for key, spec in (definition.get("inputs") or {}).items()
        if spec.get("required") and "default" not in spec
    }
    assert not required - set(used["with"]), (
        f"{name} requires {sorted(required - set(used['with']))}, which the compiler does not pass"
    )


def test_jobs_that_probe_the_cache_can_read_artifacts():
    """The durable cache layer looks up artifacts from earlier runs, which needs `actions: read`."""
    for path, text in compile_spec(FIXTURE).files.items():
        if not path.endswith(".yml"):
            continue
        for job_id, job in (yaml.safe_load(text).get("jobs") or {}).items():
            probes = any("step-cache@" in str(step.get("uses", "")) for step in job.get("steps") or [])
            if probes:
                assert (job.get("permissions") or {}).get("actions") == "read", (
                    f"{path}:{job_id} probes the cache without permission to read artifacts"
                )


def test_an_unknown_builtin_is_a_compile_error(basic_root):
    command = basic_root / "commands" / "discover.md"
    command.write_text(
        command.read_text().replace(
            "1. **Discover API surface** → script: scripts/discover-api.py",
            "1. **Discover API surface** → builtin: teleport",
        )
    )
    from lockstep.errors import EmitError

    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "not provided by pipeline-exec" in excinfo.value.render()


# --- the distributions this repository actually publishes -------------------


def _distribution_name(pyproject: Path) -> str:
    import tomllib

    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]


REPO = Path(__file__).parent.parent


def test_the_capability_names_are_distributions_this_repository_builds():
    """A generated gate runs `uv tool install "<capabilities.compiler>"` against a public index.

    If that string is not a distribution this project publishes, the best case is an install that
    fails and the worst is one that succeeds — resolving to whoever does own the name, whose code
    then runs inside every consumer's security gate. Both bare names (`lockstep`, `pipeline-exec`)
    belong to unrelated projects on PyPI, which is why the distributions carry the org prefix while
    the import name and the console script do not.
    """
    from lockstep.spec.load import load_spec

    compiler = _distribution_name(REPO / "pyproject.toml")
    runtime = _distribution_name(REPO / "packages/pipeline-exec/pyproject.toml")

    capabilities = load_spec(FIXTURE).manifest.capabilities
    assert capabilities.compiler.split(">")[0].split("=")[0].strip() == compiler
    assert capabilities.exec.partition("==")[0] == runtime


def test_no_shipped_spec_names_a_distribution_somebody_else_owns():
    """Every example and fixture, not just the one the contract tests compile."""
    compiler = _distribution_name(REPO / "pyproject.toml")
    runtime = _distribution_name(REPO / "packages/pipeline-exec/pyproject.toml")

    manifests = [
        *(REPO / "examples").glob("*/pipeline.yaml"),
        *(REPO / "tests/fixtures").glob("*/pipeline.yaml"),
        REPO / ".lockstep/pipeline.yaml",
    ]
    checked = 0
    for manifest in manifests:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            key, _, raw = line.strip().partition(":")
            value = raw.strip().strip("\"'")
            if key == "compiler" and value != ".":
                # `.` is this repository compiling itself from the checkout; every other spec names
                # a distribution, and it has to be one that exists.
                assert value.startswith(compiler), f"{manifest}: {value}"
                checked += 1
            elif key == "exec":
                assert value.startswith(runtime), f"{manifest}: {value}"
                checked += 1
    assert checked >= 12, f"only {checked} capability lines checked; the scan is not finding them"

