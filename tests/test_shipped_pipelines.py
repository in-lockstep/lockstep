"""Pipelines the compiler ships, and the path that starts by running them.

Adopting this framework began with writing a pipeline. That is the wrong first step for a team whose
problem is that they have no AI-SDLC yet, and it is a strange thing to ask of people who are
adopting an opinion in the first place.

So the framework ships pipelines, and ships them the one way that does not trap anybody: they are
**inherited**, not copied. Inheriting is what makes every later step available without giving
anything up — tune an agent inside its band, overlay a step, or write a pipeline of your own that
runs beside them.

The property these protect above the rest is that a repository which authored *nothing* gets a
pipeline that lints clean and compiles.
"""

from __future__ import annotations

import json

import pytest
import yaml

from lockstep import library
from lockstep.checks import Severity, doctor, lint
from lockstep.emit import compile_spec
from lockstep.lifecycle import fetch, pin, write_pins
from lockstep.scaffold import scaffold
from lockstep.spec.load import load_manifest_only, load_spec

SHIPPED = sorted(library.pipelines())


@pytest.fixture
def adopter(tmp_path, basic_root):
    """A repository whose entire content is what `lockstep init --adopt` writes."""
    root = tmp_path / "adopter"
    scaffold(root, "acme-app", "repo", adopt=tuple(SHIPPED))
    # The pins the fixture already resolved; `pin` itself is exercised separately below.
    (root / ".pipeline").mkdir(exist_ok=True)
    pins = json.loads((basic_root / ".pipeline" / "pins.lock").read_text())
    # The scaffold retains run history, which puts a metering job in every workflow — and that job
    # names two actions this fixture's lock does not carry. `lockstep pin` adds them for a real
    # repository; a copied static lock has to be told.
    for action in ("actions/download-artifact", "actions/upload-artifact"):
        pins.setdefault("external", {})[action] = {"tag": "v5", "sha": "0" * 40}
    (root / ".pipeline" / "pins.lock").write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    manifest = root / "pipeline.yaml"
    tag = pins["capabilities"]["actions"]["tag"]
    manifest.write_text(
        manifest.read_text().replace("actions@actions-v1.0.0", f"actions@{tag}"), encoding="utf-8"
    )
    fetch(load_manifest_only(root), root)
    return root


# --- what ships -------------------------------------------------------------


def test_something_ships(tmp_path):
    """The claim is that adopting does not begin with authoring. It needs something to be true."""
    assert SHIPPED, "no pipelines ship, so `--adopt` promises something that does not exist"


def test_every_shipped_pipeline_is_a_pipeline():
    for name, path in library.pipelines().items():
        assert (path / "pipeline.yaml").is_file(), name


def test_no_shipped_pipeline_carries_a_script():
    """A script here would be untested code arriving in every repository that adopts it.

    The repo's own suite cannot reach into the library, so `lockstep lint`'s "scripts need tests"
    rule would be enforced on adopters for code they did not write. Builtins and safe outputs only.
    """
    for name, path in library.pipelines().items():
        assert not list((path / "scripts").glob("*")) if (path / "scripts").is_dir() else True, name


def test_no_shipped_pipeline_claims_capabilities(tmp_path):
    """An inherited pipeline runs under the consumer's capabilities.

    A version pinned here would be a second opinion about which code runs, held by a repository
    that is not the one running it.
    """
    for name, path in library.pipelines().items():
        manifest = yaml.safe_load((path / "pipeline.yaml").read_text()) or {}
        assert "capabilities" not in manifest, name


# --- the zero-authoring path ------------------------------------------------


def test_adopting_writes_a_manifest_a_profile_and_the_two_layers_you_will_add(tmp_path):
    """The context and the guardrail are the customization every adopter makes.

    Shipped as working files rather than as advice, because each of them changes the prompt of every
    inherited agent — which is the situation the eval loop exists to verify, and the one an adopter
    would otherwise reach without noticing.
    """
    written = sorted(scaffold(tmp_path / "a", "acme", "repo", adopt=tuple(SHIPPED)))
    assert [p for p in written if not p.startswith("evals/")] == [
        ".gitignore",
        "README.md",
        "agents/eval-judge.md",
        "contexts/codebase.md",
        "guardrails/house-style.md",
        "pipeline.yaml",
        "profiles/repo.md",
    ]
    # The judge is an agent, and an agent with no cases cannot be changed safely — least of all the
    # one that decides whether every other agent passed.
    assert [p for p in written if p.startswith("evals/eval-judge/")]


def test_a_repository_that_authored_nothing_lints_clean(adopter):
    report = lint(load_spec(adopter))
    assert [f.code for f in report.findings if f.severity is Severity.ERROR] == []


def test_a_repository_that_authored_nothing_compiles(adopter):
    files = compile_spec(adopter).files
    assert any(path.endswith("triage-triage.yml") for path in files)
    assert any(path.startswith(".github/workflows/aw-") for path in files)


def test_the_shipped_agent_arrives_with_the_shipped_baseline(adopter):
    """Inherited or not, an agent gets the invariants. The two mechanisms have to compose."""
    files = compile_spec(adopter).files
    agent = next(text for path, text in files.items() if "/aw-" in path and path.endswith(".md"))
    assert "lockstep:guardrails/baseline.md" in agent


def test_adopting_an_unknown_pipeline_names_the_ones_that_exist(tmp_path):
    from lockstep.scaffold import ScaffoldError

    with pytest.raises(ScaffoldError, match="no shipped pipeline named"):
        scaffold(tmp_path / "a", "acme", "repo", adopt=("no-such-pipeline",))


# --- how it is pinned -------------------------------------------------------


def test_a_shipped_pipeline_is_pinned_by_the_compiler_not_a_commit(adopter):
    """There is no second thing to pin: the pipelines travel inside the compiler.

    So unlike a local path this *is* reproducible, and `doctor` must not report it as unpinned.
    """
    data, notes, unresolved = pin(load_manifest_only(adopter), adopter, offline=True)
    assert "inherits" not in data or data["inherits"] == {}
    assert any("shipped with the compiler" in note for note in notes)

    write_pins(adopter, data)
    codes = {f.code for f in doctor(load_spec(adopter), adopter).findings}
    assert "DOC017" not in codes, "reported as an unpinnable local path"
    assert "DOC018" not in codes, "reported as an unpinned upstream"


def test_inheriting_a_pipeline_this_compiler_does_not_ship_is_an_error(adopter):
    manifest = adopter / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace("lockstep:triage", "lockstep:no-such-thing"), encoding="utf-8"
    )
    report = doctor(load_spec(adopter), adopter)
    finding = next(f for f in report.findings if f.code == "DOC023")
    assert "does not ship" in finding.message


def test_fetching_one_that_does_not_ship_says_what_does(adopter):
    from lockstep.errors import LockstepError

    manifest = adopter / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace("lockstep:triage", "lockstep:no-such-thing"), encoding="utf-8"
    )
    with pytest.raises(LockstepError, match="ships with this compiler"):
        fetch(load_manifest_only(adopter), adopter)


# --- the growth path --------------------------------------------------------


def test_a_shipped_agent_can_be_tuned_without_forking_it(adopter):
    """Bands are why `uses:` is enough for the first change somebody wants to make."""
    manifest = adopter / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "capabilities:",
            "commands:\n  triage:\n    from: triage\n    agents:\n      triage-analyst:\n"
            "        max-ai-credits: 40\n\ncapabilities:",
        ),
        encoding="utf-8",
    )
    files = compile_spec(adopter).files
    agent = next(text for path, text in files.items() if "/aw-" in path and path.endswith(".md"))
    assert "max-ai-credits: 40" in agent


def test_a_pipeline_of_your_own_runs_beside_the_shipped_ones(adopter):
    """Nothing is given up by adding one. That is the whole argument for inheriting rather than copying."""
    (adopter / "commands").mkdir(exist_ok=True)
    (adopter / "commands" / "ours.md").write_text(
        "---\nname: ours\ndescription: Something this team does\n"
        "github:\n  triggers:\n    workflow_dispatch: true\n---\n\n"
        "## Steps\n\n1. **Read the issue** → builtin: issue-fetch\n"
        '   - args: --issue="1" --output={output_dir}/issue.json\n',
        encoding="utf-8",
    )
    files = compile_spec(adopter).files
    assert any(path.endswith("ours.yml") for path in files)
    assert any(path.endswith("triage-triage.yml") for path in files)


# --- what the shipped set covers --------------------------------------------


def test_the_shipped_set_is_a_whole_sdlc():
    """Triage an issue, implement it, review the result, fix what it broke."""
    assert set(SHIPPED) >= {"triage", "implement", "review", "fix"}


def test_every_shipped_agent_publishes_bands_rather_than_fixed_values(adopter):
    """So the first change anybody wants to make is a line in `commands:`, not a fork."""
    spec = load_spec(adopter)
    shipped = [a for name, a in spec.agents.items() if a.inherited_from]
    assert shipped
    for agent in shipped:
        # The band is recorded beside the resolved default, keyed by the field it governs.
        assert "max-ai-credits" in agent.bands, agent.name
        assert "model" in agent.bands, agent.name


def test_every_shipped_agent_has_eval_cases(adopter):
    """LNT001 refuses an agent nobody evaluates. A shipped agent is not exempt from that."""
    for name, path in library.pipelines().items():
        for agent in sorted((path / "agents").glob("*.md")):
            cases = path / "evals" / agent.stem / "cases"
            assert cases.is_dir() and any(cases.glob("*.json")), f"{name}/{agent.stem}"


def test_a_fix_is_proven_by_a_test_that_failed_first(adopter):
    """The shape of the fix pipeline is its argument.

    A pipeline that wrote the reproducer and the fix together would produce a test that passes
    either way and a change nobody can tell worked. So the failing run gates the fix, and the
    passing run gates the proposal.
    """
    workflow = yaml.safe_load(compile_spec(adopter).files[".github/workflows/fix-fix.yml"])
    jobs = workflow["jobs"]

    def runs(job):
        return " ".join(step.get("run", "") for step in jobs[job].get("steps", []))

    def needs(job):
        value = jobs[job].get("needs")
        return [value] if isinstance(value, str) else list(value or [])

    assert "--expect=fail" in runs("reproduce")
    assert "--expect=pass" in runs("validate")
    # The fix cannot be written until the reproducer has failed.
    assert "reproduce" in needs("write-the-fix")
    # Nothing is proposed until the suite passes.
    assert "validate" in needs("review-the-fix")


def test_nothing_shipped_writes_to_the_repository_itself(adopter):
    """Agents produce files; `propose:` turns them into a pull request.

    That is what lets every shipped agent be read-only, and why a prompt is never the thing standing
    between a model and the default branch.
    """
    for path, text in compile_spec(adopter).files.items():
        if "/aw-" in path and path.endswith(".md"):
            assert yaml.safe_load(text.split("---")[1])["permissions"] == "read-all", path


def test_triage_writes_back_to_jira_and_only_to_jira(adopter):
    """On GitHub the agent's safe outputs already did it; a second write would duplicate the comment.

    The gate reads what the fetch step left outstanding rather than the `source` parameter, because
    the question is which tracker actually answered.
    """
    workflow = yaml.safe_load(compile_spec(adopter).files[".github/workflows/triage-triage.yml"])
    write_back = workflow["jobs"]["write-back"]
    assert "jira" in write_back["if"] and "needs.issue.outputs.writeback" in write_back["if"]
    command = " ".join(step.get("run", "") for step in write_back["steps"])
    assert "pipeline-exec jira-update" in command
    # Additive writes only. Nothing here transitions an issue.
    assert "--transition" not in command


def test_the_gate_reads_a_list_because_the_condition_is_a_membership_test(adopter):
    """`fromJSON` on a bare word fails the run, so the output it reads has to be a JSON array."""
    workflow = yaml.safe_load(compile_spec(adopter).files[".github/workflows/triage-triage.yml"])
    assert "fromJSON(needs.issue.outputs.writeback)" in workflow["jobs"]["write-back"]["if"]
    assert workflow["jobs"]["issue"]["outputs"]["writeback"]


# --- every shipped pull request is traceable --------------------------------


def shipped_commands():
    """Every command file the compiler ships, with its parsed front matter."""
    for name, path in sorted(library.pipelines().items()):
        for command in sorted((path / "commands").glob("*.md")):
            front = yaml.safe_load(command.read_text().split("---")[1])
            yield f"{name}/{command.stem}", front


def test_every_shipped_command_that_opens_a_pull_request_records_the_work_item():
    """The hard requirement: a shipped pipeline never commits work nobody can trace back.

    Enforced over the library rather than over every pipeline, because a consumer may legitimately
    open a pull request that came from no tracker at all — a dependency bump has no issue.
    """
    checked = 0
    for name, front in shipped_commands():
        propose = (front.get("github") or {}).get("propose")
        if not propose:
            continue
        checked += 1
        assert propose.get("issue-from"), f"{name} opens a pull request without recording the issue"
    assert checked >= 2, f"only {checked} proposing command(s) checked; the rule is not being applied"


def test_the_key_comes_from_the_tracker_not_from_what_somebody_typed():
    """`{issue}` is the parameter. A run invoked with `412`, or a URL, must still record `#412`."""
    for name, front in shipped_commands():
        propose = (front.get("github") or {}).get("propose")
        if propose:
            assert "{issue}" != propose["issue-from"], name
            assert propose["issue-from"].endswith(".json"), name


def test_the_reference_reaches_the_compiled_workflow(adopter):
    files = compile_spec(adopter).files
    for path in (".github/workflows/implement-implement.yml", ".github/workflows/fix-fix.yml"):
        workflow = yaml.safe_load(files[path])
        step = workflow["jobs"]["propose-generated-artifacts"]["steps"][-1]
        assert step["with"]["issue-from"], path


def test_a_pipeline_that_does_not_ask_for_it_is_not_forced_to(basic_root):
    """A dependency bump has no work item, and demanding one would be a gate with nothing behind it."""
    files = compile_spec(basic_root).files
    for path, text in files.items():
        if not path.endswith(".yml"):
            continue
        jobs = (yaml.safe_load(text) or {}).get("jobs") or {}
        propose = jobs.get("propose-generated-artifacts")
        if propose:
            assert "issue-from" not in propose["steps"][-1]["with"]


# --- the retro proposes, it does not edit ------------------------------------


def test_retro_ships():
    assert "retro" in SHIPPED


def test_the_retro_computes_its_numbers_in_code_not_in_a_prompt(adopter):
    """Two runs over the same window must produce the same figures.

    A trend an agent averaged for itself would be a different trend each time, and one nobody could
    check against the ledger.
    """
    workflow = yaml.safe_load(compile_spec(adopter).files[".github/workflows/retro-retro.yml"])
    command = " ".join(
        step.get("run", "") for job in workflow["jobs"].values() for step in job.get("steps", []) or []
    )
    assert "pipeline-exec run-history" in command


def test_the_retro_cannot_edit_what_constrains_it(adopter):
    """A pipeline able to rewrite the guardrails that constrain it has guardrails in name only.

    So the retro's output is an issue somebody reads, not a change to a prompt. Every agent is
    read-only anyway; this checks the retro was not given a `propose:` block to get around it.
    """
    library_path = library.pipelines()["retro"]
    front = yaml.safe_load((library_path / "commands" / "retro.md").read_text().split("---")[1])
    assert "propose" not in (front.get("github") or {})

    workflow = yaml.safe_load(compile_spec(adopter).files[".github/workflows/retro-retro.yml"])
    assert "propose-generated-artifacts" not in workflow["jobs"]

    guardrail = (library_path / "guardrails" / "retro.md").read_text()
    for denied in ("create_pull_request", "merge_pull_request"):
        assert denied in guardrail


def test_the_retro_files_one_issue_and_no_more(adopter):
    files = compile_spec(adopter).files
    agent = next(t for p, t in files.items() if "aw-retro-" in p and p.endswith(".md"))
    front = yaml.safe_load(agent.split("---")[1])
    assert front["safe-outputs"]["create-issue"]["max"] == 1


def test_the_retro_runs_on_a_schedule_because_a_trend_is_not_an_event(adopter):
    workflow = yaml.safe_load(compile_spec(adopter).files[".github/workflows/retro-retro.yml"])
    triggers = workflow.get("on") or workflow.get(True)
    assert "schedule" in triggers
