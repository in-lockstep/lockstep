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
from lockstep.spec.load import load_spec

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
# This repository, which inherits the shipped commands that opt into an event source.
FIXTURE_LIBRARY = Path(__file__).parent.parent
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


def test_the_expectations_lint_knows_are_the_ones_the_grader_applies():
    """Two copies of one contract: the compiler must not import the runtime, so a test holds them.

    A key lint accepts and the grader ignores is an expectation that passes review and never runs.
    """
    from pipeline_exec.evals import EXPECT_KEYS as runtime_keys

    from lockstep.checks import EXPECT_KEYS as compiler_keys

    assert set(compiler_keys) == set(runtime_keys)


def test_the_rubric_keys_lint_knows_are_the_ones_the_grader_reads():
    """The same two-copies problem one level down.

    A scored rubric is validated by the compiler and applied by the runtime. A key lint accepts and
    the grader ignores is a threshold that passes review and never gates anything.
    """
    from pipeline_exec.evals import RUBRIC_KEYS as runtime_keys

    from lockstep.checks import RUBRIC_KEYS as compiler_keys

    assert set(compiler_keys) == set(runtime_keys)


def test_the_key_a_fixture_path_is_written_into_is_the_one_lint_reserves(tmp_path):
    """Lint refuses a case that sets it; the runtime writes it. Two names would let both be true.

    The directory a fixture is looked up in has to agree too — lint checking one place while the
    grader reads another is a green review of a suite that cannot run.
    """
    from pipeline_exec.evals import FIXTURES_DIR as runtime_dir
    from pipeline_exec.evals import REPO_KEY

    from lockstep.checks import FIXTURES_DIR as compiler_dir
    from lockstep.checks import Report, _check_fixture

    assert compiler_dir == runtime_dir

    fixture = tmp_path / "cases" / ".." / runtime_dir / "tree"
    fixture.mkdir(parents=True)
    (fixture / "main.py").write_text("x = 1\n", encoding="utf-8")

    report = Report()
    _check_fixture("tree", {REPO_KEY: "/elsewhere"}, tmp_path / "cases", "one.json", report)
    assert [f.code for f in report.findings] == ["LNT009"]


# --- the executor has to exist where it is invoked ------------------------------------------------
#
# `/review` by comment failed with `pipeline-exec: command not found`. The command-gate job runs on
# a bare runner rather than in the executor container, and the composite action it calls parses the
# comment with `pipeline-exec parse-command`.
#
# It failed on the comment path alone, because every other trigger short-circuits before the parse —
# so `/review` by dispatch worked, and `/review` by comment, which is the entire point of a chat-ops
# command, had never run. Nothing offline noticed, because the invocation is inside a composite
# action rather than in the emitted workflow, and the emitted workflow is all any check reads.


def _installs_executor(job: dict) -> bool:
    for step in job.get("steps") or []:
        if "uv tool install" in step.get("run", "") and "exec" in step.get("run", ""):
            return True
    return False


def _uses_executor(job: dict) -> bool:
    """Directly, or through a composite action known to need it."""
    for step in job.get("steps") or []:
        if "pipeline-exec " in step.get("run", ""):
            return True
        # The gate is the case that bit: the invocation lives inside the action, not here.
        if "/actions/command-gate@" in str(step.get("uses", "")):
            return True
    return False


@pytest.mark.parametrize("root", ALL_PIPELINES, ids=lambda p: p.name)
def test_every_job_that_runs_the_executor_can_reach_it(root):
    missing: list[str] = []
    for path, text in compile_spec(root).files.items():
        if not path.endswith(".yml"):
            continue
        for name, job in (yaml.safe_load(text) or {}).get("jobs", {}).items():
            if not isinstance(job, dict) or not _uses_executor(job):
                continue
            if job.get("container") or _installs_executor(job):
                continue
            missing.append(f"{path}:{name}")
    assert not missing, (
        "these jobs invoke `pipeline-exec` with neither the executor container nor an install, "
        "so they fail at run time with `command not found`:\n  " + "\n  ".join(missing)
    )


# --- builtins that reach the GitHub API need a token ----------------------------------------------
#
# `pr-diff` failed at run time with the CLI's own advice printed at it: "To use GitHub CLI in a
# GitHub Actions workflow, set the GH_TOKEN environment variable." `gh` refuses to run inside
# Actions without it and falls back to nothing, so a builtin that shells out to it needs one in its
# own step environment.
#
# Declared rather than granted to everything: a `script:` step is code a pipeline author wrote, and
# handing it the repository token because a different step needed one is how a deterministic step
# gains reach nobody reviewed. Which makes the declaration something that can drift, so it is
# checked against the runtime here.


def test_the_token_list_names_only_real_builtins():
    from lockstep.emit.builtins import AVAILABLE, NEEDS_GITHUB_TOKEN

    assert NEEDS_GITHUB_TOKEN <= AVAILABLE, sorted(NEEDS_GITHUB_TOKEN - AVAILABLE)


def test_every_builtin_that_shells_out_to_gh_is_declared():
    """Read the runtime's source rather than trusting the list.

    A builtin that starts calling `gh` and is not added here fails at run time, in somebody's
    pipeline, with an error about an environment variable they never set.
    """
    from lockstep.emit.builtins import NEEDS_GITHUB_TOKEN

    source = (Path(__file__).parent.parent / "packages/pipeline-exec/src/pipeline_exec/cli.py").read_text(
        encoding="utf-8"
    )

    # Command functions are `def name(...)` with hyphens becoming underscores in the CLI name.
    reaching: set[str] = set()
    current = ""
    for line in source.splitlines():
        match = re.match(r"^def ([a-z_0-9]+)\(", line)
        if match:
            current = match.group(1)
        if current and ("_gh_json(" in line or "_gh_send(" in line):
            reaching.add(current.replace("_", "-"))

    # Helpers and internals are not spec surface; only compare what a `builtin:` step may name.
    declared_or_internal = NEEDS_GITHUB_TOKEN | INTERNAL | {"-gh-json", "-gh-send"}
    undeclared = {
        name
        for name in reaching
        if name not in declared_or_internal and name.replace("-", "_") not in {"_gh_json", "_gh_send"}
    }
    # `issue-fetch` reaches the API by calling `gh-issue-fetch`, which this scan attributes to the
    # callee — both are declared, so the set stays empty either way.
    assert not undeclared, (
        "these builtins call the GitHub API and are not in NEEDS_GITHUB_TOKEN, so the compiler "
        f"emits them without GH_TOKEN and they fail at run time: {sorted(undeclared)}"
    )


@pytest.mark.parametrize("root", ALL_PIPELINES, ids=lambda p: p.name)
def test_emitted_github_builtins_carry_a_token(root):
    from lockstep.emit.builtins import NEEDS_GITHUB_TOKEN

    missing: list[str] = []
    for path, text in compile_spec(root).files.items():
        if not path.endswith(".yml"):
            continue
        for job_name, job in (yaml.safe_load(text) or {}).get("jobs", {}).items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                run = step.get("run", "")
                for builtin in NEEDS_GITHUB_TOKEN:
                    if f"pipeline-exec {builtin} " in run and "GH_TOKEN" not in str(step.get("env", "")):
                        missing.append(f"{path}:{job_name}:{builtin}")
    assert not missing, "emitted without GH_TOKEN:\n  " + "\n  ".join(sorted(set(missing)))


# --- a cache key becomes an artifact name ---------------------------------------------------------
#
# GitHub refuses several characters in an artifact name, `/` among them, and the step cache uploads
# under `step-<key-prefix>`. An inherited command is namespaced by its alias, so `review/review`
# produced `step-ls-v1-lockstep-review/review-diff-…` and the upload failed with "The artifact name
# is not valid".
#
# Only an *inherited* pipeline can produce it, which is why every example passed: the consumer
# fixture is the one that inherits, and it is the shape `--adopt` gives everybody who writes least.

# Characters GitHub rejects in an artifact name.
ARTIFACT_FORBIDDEN = set('":<>|*?\r\n\\/')


def _key_prefixes(root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path, text in compile_spec(root).files.items():
        if not path.endswith(".yml"):
            continue
        for job in (yaml.safe_load(text) or {}).get("jobs", {}).values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                prefix = (step.get("with") or {}).get("key-prefix")
                if prefix:
                    found.append((path, str(prefix)))
    return found


@pytest.mark.parametrize("root", ALL_PIPELINES, ids=lambda p: p.name)
def test_every_cache_key_is_a_legal_artifact_name(root):
    bad = [f"{path}: {prefix}" for path, prefix in _key_prefixes(root) if ARTIFACT_FORBIDDEN & set(prefix)]
    assert not bad, "these become artifact names GitHub refuses:\n  " + "\n  ".join(bad)


# --- a consumer can ask for the token too ---------------------------------------------------------
#
# The framework's own builtins get one because the framework knows they call `gh`. A `script:` step
# or a builtin from `extensions.builtins` is code the framework has never seen, so it cannot be on
# that list — and without a way to declare it the only workaround is a personal access token in a
# profile secret, which is a standing credential where a job-scoped one would do.


def _step_env(root, workflow: str, step_id: str) -> dict:
    data = yaml.safe_load(compile_spec(root).files[workflow]) or {}
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if step.get("id") == step_id:
                return step.get("env") or {}
    raise AssertionError(f"no step {step_id!r} in {workflow}")


def _with_step(tmp_path, attribute: str) -> Path:
    """The basic fixture with one script step carrying the given attribute."""
    import shutil

    root = tmp_path / "spec"
    shutil.copytree(FIXTURE, root)
    command = root / "commands" / "discover.md"
    text = command.read_text(encoding="utf-8")
    marker = "2. **Discover UI structure** \u2192 script: scripts/discover-ui.py\n"
    assert marker in text, "fixture step shape changed"
    command.write_text(text.replace(marker, marker + "   - id: uistep\n" + attribute, 1), encoding="utf-8")
    return root


def test_a_script_step_can_ask_for_the_github_token(tmp_path):
    root = _with_step(tmp_path, "   - github-token: true\n")
    assert _step_env(root, ".github/workflows/discover.yml", "uistep") == {"GH_TOKEN": "${{ github.token }}"}


def test_a_step_that_does_not_ask_does_not_receive(tmp_path):
    """The default stays closed: a script is arbitrary code, and a token it did not ask for is
    reach nobody reviewed."""
    root = _with_step(tmp_path, "")
    assert "GH_TOKEN" not in _step_env(root, ".github/workflows/discover.yml", "uistep")


def test_the_declaration_is_not_silently_swallowed_as_an_argument(tmp_path):
    """Unknown step keys become `args`, so a typo'd or unparsed attribute would land in the command
    line instead of failing — and the step would run with the flag pasted onto it."""
    root = _with_step(tmp_path, "   - github-token: true\n")
    text = compile_spec(root).files[".github/workflows/discover.yml"]
    assert "github-token" not in text


# --- a declared default has to survive every trigger ----------------------------------------------
#
# `inputs` exists only for `workflow_dispatch` and `workflow_call`. On a comment or a schedule it is
# empty, and a parameter's declared default reaches the *dispatch input definition* rather than the
# expression a step is built from.
#
# So `/implement 18`, commented on an issue, ran `issue-fetch --source=""` and was refused:
# "Invalid value for '--source': '' is not one of 'github', 'jira'". `issue` survived only because
# it is a declared command argument and therefore had the gate to fall back to — which is why the
# failure looked like one parameter being wrong rather than every defaulted one.


def name_of(expression: str) -> str:
    return expression.split(".", 1)[1].split()[0].strip()


def _expansions(root: Path) -> list[tuple[str, str]]:
    """Every `${{ … }}` reading a command input, with the workflow it came from."""
    found: list[tuple[str, str]] = []
    for path, text in compile_spec(root).files.items():
        if not path.endswith(".yml"):
            continue
        for match in re.finditer(r"\$\{\{\s*(inputs\.[a-z_0-9]+[^}]*?)\s*\}\}", text):
            found.append((path, match.group(1)))
    return found


@pytest.mark.parametrize("root", ALL_PIPELINES, ids=lambda p: p.name)
def test_a_parameter_with_a_default_falls_back_to_it(root):
    """Every reference to a defaulted parameter ends in a literal, so no trigger can empty it."""
    spec = load_spec(root)
    defaulted = {
        parameter.input_name: parameter.default
        for command in spec.commands.values()
        for parameter in command.parameters
        if parameter.default
    }
    if not defaulted:
        pytest.skip("no defaulted parameters in this pipeline")

    bare: list[str] = []
    for path, expression in _expansions(root):
        # Value substitutions only. A step *condition* compiles to a comparison
        # (`inputs.skip_discovery != true`) which already reasons about the empty case — a
        # different construct with its own semantics, and not what this is about.
        if not re.fullmatch(r"inputs\.[a-z_0-9]+(\s*\|\|\s*[^|]+)*", expression.strip()):
            continue
        # The step-cache action's own `force` inputs are emitted by the caching layer rather than by
        # `expand()`, and are safe for a narrower reason: they default to false and the action tests
        # for the literal "true", so an empty string means the same thing. Left alone deliberately —
        # threading the command through `emit_probe` to make them consistent would be a signature
        # change for no behavioural gain.
        if name_of(expression) in ("force", "force_steps"):
            continue
        name = expression.split(".", 1)[1].split()[0].strip()
        if name in defaulted and "'" not in expression:
            bare.append(f"{path}: ${{{{ {expression} }}}}")
    assert not bare, (
        "these read a defaulted parameter with no literal fallback, so a comment or a schedule "
        "supplies the empty string:\n  " + "\n  ".join(sorted(set(bare)))
    )


def test_an_explicit_value_still_wins_over_the_default():
    """The default is last in the chain, so a dispatch or a comment overrides it."""
    from lockstep.emit.context import EmitContext  # noqa: F401

    text = compile_spec(FIXTURE).files[".github/workflows/generate-tests.yml"]
    for expression in re.findall(r"\$\{\{\s*inputs\.[^}]*?\|\|[^}]*?\}\}", text):
        parts = [part.strip() for part in expression.strip("${} ").split("||")]
        assert parts[0].startswith("inputs."), expression
        literals = [i for i, part in enumerate(parts) if part.startswith("'")]
        assert not literals or literals[0] == len(parts) - 1, (
            f"a literal default must come last so an explicit value wins: {expression}"
        )


# --- a parameter can take a fact from the event ---------------------------------------------------
#
# `/implement 18` commented **on issue #18** should not need the number repeated. The comment is on
# the issue and `github.event.issue.number` is in the payload — the gate already derives the sibling
# value, `pull_request`, the same way.
#
# Declared rather than inferred from the parameter's name: a pipeline whose parameter happens to be
# called `issue` should not silently acquire a meaning it did not ask for.


def test_the_event_source_lands_between_the_explicit_values_and_the_default():
    """Order is the whole behaviour. Explicit beats the payload; the payload beats a guess."""
    text = compile_spec(FIXTURE_LIBRARY).files[".github/workflows/implement-implement.yml"]
    match = re.search(r"issue-fetch --source=\"[^\"]*\" --issue=\"\$\{\{ ([^}]+) \}\}\"", text)
    assert match, "the issue-fetch invocation changed shape"
    parts = [part.strip() for part in match.group(1).split("||")]
    assert parts[0] == "inputs.issue"
    assert parts[1] == "needs.command-gate.outputs.issue"
    assert parts[2] == "github.event.issue.number"


def test_an_unknown_event_source_is_refused(tmp_path):
    """An unrecognised name would otherwise emit an empty expression and fail at run time."""
    import shutil

    from lockstep.errors import SpecError

    root = tmp_path / "spec"
    shutil.copytree(FIXTURE, root)
    command = root / "commands" / "generate-tests.md"
    text = command.read_text(encoding="utf-8")
    marker = "  - name: skip-discovery\n"
    assert marker in text
    command.write_text(
        text.replace(marker, marker + "    from-event: whatever-i-felt-like\n", 1), encoding="utf-8"
    )
    with pytest.raises(SpecError) as caught:
        compile_spec(root)
    # `render()` is what a user sees; the available list lives in the hint rather than the message,
    # and an error that refuses a name without saying which names exist is half an error.
    rendered = caught.value.render()
    assert "whatever-i-felt-like" in rendered
    assert "issue-number" in rendered, "the error should name what is available"


def test_a_parameter_without_the_field_is_unchanged(tmp_path):
    """Opt-in: nothing acquires a payload fallback by having a suggestive name."""
    text = compile_spec(FIXTURE).files[".github/workflows/generate-tests.yml"]
    assert "github.event.issue.number" not in text
