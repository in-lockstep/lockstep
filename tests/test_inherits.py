"""Inheriting pipelines and standards from upstream repositories.

One security team owns the rules; many repositories follow them. The fixtures model that literally:
`upstream-standards` publishes sealed guardrails and nothing else, `upstream-review` publishes a
pipeline, and `consumer` writes a profile, a context and one house rule.

The failures worth preventing are all silent ones — a standard that vanishes because a profile
excluded it, an inherited script that resolves to a path the runner does not have, provenance that
credits the consumer for a guardrail it only inherited.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from lockstep.checks import doctor, lint
from lockstep.conformance import simulate
from lockstep.emit import compile_spec
from lockstep.errors import MissingDefinition, SpecError
from lockstep.lifecycle import fetch
from lockstep.spec.load import load_manifest_only, load_spec

FIXTURES = Path(__file__).parent / "fixtures"
AGENT = ".github/workflows/aw-review-reviewer.md"


@pytest.fixture
def consumer(tmp_path):
    """A writable copy of the whole three-repository arrangement, already fetched."""
    for name in ("upstream-standards", "upstream-review", "consumer"):
        shutil.copytree(FIXTURES / name, tmp_path / name)
    root = tmp_path / "consumer"
    fetch(load_manifest_only(root), root)
    return root


def edit(root, path, replacements):
    target = root / path
    text = target.read_text()
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    target.write_text(text, encoding="utf-8")


def body(root):
    return compile_spec(root).files[AGENT].split("---", 2)[2]


def guardrail_order(root):
    import re

    return re.findall(r"guardrail: (\S+)", body(root))


# --- what a consumer actually writes ----------------------------------------


def test_a_consumer_writes_a_profile_a_context_and_its_own_rules(consumer):
    """Everything else arrives. These are the layers an upstream cannot know."""
    written = {
        str(path.relative_to(consumer))
        for path in consumer.rglob("*")
        if path.is_file() and ".pipeline" not in str(path)
    }
    assert written == {
        "pipeline.yaml",
        "profiles/repo.md",
        "contexts/repo.md",
        "guardrails/house-style.md",
    }


def test_the_inherited_pipeline_compiles_as_this_repositorys_own(consumer):
    files = compile_spec(consumer).files
    assert ".github/workflows/review.yml" in files
    assert AGENT in files


def test_an_unfetched_upstream_is_an_error_that_says_what_to_run(consumer):
    shutil.rmtree(consumer / ".pipeline" / "inherited" / "standards")
    with pytest.raises(MissingDefinition) as excinfo:
        load_spec(consumer)
    rendered = excinfo.value.render()
    assert "has not been fetched" in rendered
    assert "lockstep fetch" in rendered


# --- sealing ----------------------------------------------------------------


def test_a_sealed_standard_reaches_an_agent_that_never_named_it(consumer):
    """A guardrail every pipeline must remember to list is one that a pipeline will forget."""
    assert "guardrails" not in (FIXTURES / "upstream-review" / "agents" / "reviewer.md").read_text().split(
        "data-handling"
    )[0].split("---")[-1]
    assert "internal hostnames" in body(consumer)


def test_authority_runs_framework_then_organization_then_repository(consumer):
    assert guardrail_order(consumer) == [
        "baseline",
        "standards/data-handling",
        "review/house",
        "house-style",
    ]


def test_a_profile_cannot_exclude_a_sealed_standard(consumer):
    sealed = "contexts: [repo]\nexclude_guardrails: [standards/data-handling]"
    edit(consumer, "profiles/repo.md", {"contexts: [repo]": sealed})
    with pytest.raises(SpecError) as excinfo:
        load_spec(consumer)
    assert "sealed and cannot be excluded" in excinfo.value.render()


def test_a_profile_may_still_exclude_an_ordinary_inherited_guardrail(consumer):
    ordinary = "contexts: [repo]\nexclude_guardrails: [review/house]"
    edit(consumer, "profiles/repo.md", {"contexts: [repo]": ordinary})
    assert "review/house" not in guardrail_order(consumer)
    assert "standards/data-handling" in guardrail_order(consumer)


def test_a_local_file_cannot_take_an_inherited_name(consumer):
    (consumer / "guardrails" / "standards").mkdir()
    (consumer / "guardrails" / "standards" / "data-handling.md").write_text(
        "---\nname: standards/data-handling\n---\n\nAnything goes.\n", encoding="utf-8"
    )
    with pytest.raises(SpecError) as excinfo:
        load_spec(consumer)
    assert "defined twice" in excinfo.value.render()


def test_sealing_your_own_guardrail_seals_it_against_yourself(consumer):
    """So it does nothing, and the loader says so by ignoring it rather than pretending."""
    edit(consumer, "guardrails/house-style.md", {"description: What this repo adds": "sealed: true"})
    spec = load_spec(consumer)
    assert not spec.guardrails["house-style"].sealed


def test_the_enforce_floor_merges_across_tiers(consumer):
    front = yaml.safe_load(compile_spec(consumer).files[AGENT].split("---")[1])
    assert front["permissions"] == "read-all"


# --- namespacing ------------------------------------------------------------


def test_an_inherited_script_resolves_into_the_fetched_tree(consumer):
    workflow = yaml.safe_load(compile_spec(consumer).files[".github/workflows/review.yml"])
    run = " ".join(
        step.get("run", "")
        for job in workflow["jobs"].values()
        for step in job.get("steps", []) or []
    )
    assert ".pipeline/inherited/review/scripts/collect-diff.py" in run


def test_two_upstreams_keep_their_own_namespaces(consumer):
    spec = load_spec(consumer)
    assert spec.guardrails["standards/data-handling"].inherited_from == "standards"
    assert spec.guardrails["review/house"].inherited_from == "review"
    assert spec.guardrails["house-style"].inherited_from == ""


def test_provenance_names_the_upstream_each_layer_came_from(consumer):
    """Otherwise the diff that matters when a standard changes is unattributable."""
    text = compile_spec(consumer).files[AGENT]
    assert "standards:guardrails/data-handling.md@" in text
    assert "review:agents/reviewer.md@" in text
    assert "lockstep:guardrails/baseline.md@" in text
    assert " guardrails/house-style.md@" in text


# --- what the consumer may add ----------------------------------------------


def test_add_guardrails_appends_without_touching_the_inherited_pipeline(consumer):
    assert "house-style" in guardrail_order(consumer)
    assert "A formatter owns that here" in body(consumer)


def test_the_repositorys_context_reaches_the_inherited_agent(consumer):
    """A context is bound to the profile, so it arrives without either upstream naming it."""
    front = yaml.safe_load(compile_spec(consumer).files[AGENT].split("---")[1])
    assert "shared/context-repo.md" in front["imports"]
    assert "src/repo.py" in compile_spec(consumer).files[".github/workflows/shared/context-repo.md"]


def test_from_an_alias_that_is_not_inherited_is_refused(consumer):
    edit(consumer, "pipeline.yaml", {"from: review": "from: nope"})
    with pytest.raises(MissingDefinition) as excinfo:
        load_spec(consumer)
    assert "not an alias in `inherits:`" in excinfo.value.render()


def test_an_ambiguous_from_names_the_candidates(consumer):
    shutil.copy(
        consumer / ".pipeline/inherited/review/commands/review.md",
        consumer / ".pipeline/inherited/review/commands/second.md",
    )
    edit(consumer, ".pipeline/inherited/review/commands/second.md", {"name: review": "name: second"})
    with pytest.raises(SpecError) as excinfo:
        load_spec(consumer)
    rendered = excinfo.value.render()
    assert "ambiguous" in rendered
    assert "review/second" in rendered


# --- the checks hold --------------------------------------------------------


def test_the_consumer_lints_clean(consumer):
    assert lint(load_spec(consumer)).findings == []


def test_a_local_path_upstream_is_reported_as_unreproducible(consumer):
    codes = {f.code for f in doctor(load_spec(consumer), consumer).findings}
    assert "DOC017" in codes


def test_an_unpinned_remote_upstream_is_an_error(consumer):
    remote = {"standards: ../upstream-standards": "standards: github.com/acme/standards@v3"}
    edit(consumer, "pipeline.yaml", **{}) if False else edit(consumer, "pipeline.yaml", remote)
    report = doctor(load_manifest_only(consumer), consumer)
    assert "DOC018" in {f.code for f in report.findings}
    assert not report.ok


def test_the_generated_ci_fetches_before_it_compiles(consumer):
    ci = yaml.safe_load(compile_spec(consumer).files[".github/workflows/pipeline-ci.yml"])
    for job in ci["jobs"].values():
        runs = [step.get("run", "") for step in job["steps"]]
        if any("lockstep compile" in r or "lockstep lint" in r for r in runs):
            assert "lockstep fetch" in runs


def test_the_compiled_pipeline_is_reachable_end_to_end(consumer):
    workflow = yaml.safe_load(compile_spec(consumer).files[".github/workflows/review.yml"])
    assert simulate(workflow, {}, {}).order == ["diff", "review-it"]
