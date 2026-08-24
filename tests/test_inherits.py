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

from lockstep.checks import Severity, doctor, lint
from lockstep.conformance import simulate
from lockstep.emit import compile_spec
from lockstep.errors import EmitError, MissingDefinition, SpecError
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
    """Everything inherited arrives. The rest is what an upstream cannot know, plus what it owns.

    The `changelog` pipeline is the repository's own — no upstream describes it, and nothing about
    it was inherited. It is here because that is the case the organization's ceilings are for.
    """
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
        "agents/changelog-writer.md",
        "commands/changelog.md",
        "evals/changelog-writer/cases/one.json",
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


def test_sealed_standards_arrive_in_the_order_the_repository_declares_them(consumer):
    """Position carries meaning — a later instruction reads as a refinement of an earlier one.

    So the author decides it. Sorting the aliases made the authority order a consequence of what
    somebody named an upstream, which is the kind of thing that is right by luck until a rename.
    """
    extra = consumer / ".pipeline/inherited/review/guardrails/review-standard.md"
    extra.write_text("---\nname: review-standard\nsealed: true\n---\n\nA review rule.\n", encoding="utf-8")
    assert guardrail_order(consumer)[:3] == [
        "baseline",
        "standards/data-handling",
        "review/review-standard",
    ]

    edit(
        consumer,
        "pipeline.yaml",
        {
            "  standards: ../upstream-standards\n  review: ../upstream-review": (
                "  review: ../upstream-review\n  standards: ../upstream-standards"
            )
        },
    )
    assert guardrail_order(consumer)[:3] == [
        "baseline",
        "review/review-standard",
        "standards/data-handling",
    ]


def test_the_frameworks_baseline_stays_first_whatever_a_repository_declares(consumer):
    """The one position no repository chooses: a floor a consumer could push down is not a floor."""
    edit(
        consumer,
        "pipeline.yaml",
        {
            "  standards: ../upstream-standards\n  review: ../upstream-review": (
                "  review: ../upstream-review\n  standards: ../upstream-standards"
            )
        },
    )
    assert guardrail_order(consumer)[0] == "baseline"


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


# --- bands: what an upstream lets a consumer move ---------------------------


def credits(root):
    front = yaml.safe_load(compile_spec(root).files[AGENT].split("---")[1])
    return front["max-ai-credits"], front["engine"]["model"], front["timeout-minutes"]


def test_a_consumer_moves_a_dial_within_the_band(consumer):
    assert credits(consumer) == (150, "claude-opus-4-1", 20)


def test_an_untouched_band_keeps_the_upstream_default(consumer):
    """`timeout-minutes` is banded and not tuned, so the publishing repository's value stands."""
    assert credits(consumer)[2] == 20


def test_going_outside_the_band_is_refused_and_names_it(consumer):
    edit(consumer, "pipeline.yaml", {"max-ai-credits: 150": "max-ai-credits: 400"})
    with pytest.raises(SpecError) as excinfo:
        load_spec(consumer)
    rendered = excinfo.value.render()
    assert "outside the band 30–200" in rendered
    assert "ask them to widen it" in rendered


def test_a_choice_outside_the_allow_list_is_refused(consumer):
    edit(consumer, "pipeline.yaml", {"model: claude-opus-4-1": "model: gpt-9"})
    with pytest.raises(SpecError) as excinfo:
        load_spec(consumer)
    assert "claude-sonnet-4-6" in excinfo.value.render()


def test_a_field_with_no_band_is_fixed(consumer):
    """Refused rather than ignored: the failure to rule out is believing you raised something."""
    edit(consumer, "pipeline.yaml", {"max-ai-credits: 150": "max_tool_turns: 20"})
    with pytest.raises(SpecError) as excinfo:
        load_spec(consumer)
    rendered = excinfo.value.render()
    assert "is fixed by review" in rendered
    assert "Tunable here: max-ai-credits, model, timeout-minutes" in rendered


def test_capability_cannot_be_banded_at_all(consumer):
    """The answer to "can consumers raise max_tool_turns" is no, and it says why."""
    edit(
        consumer,
        ".pipeline/inherited/review/agents/reviewer.md",
        {"max_tool_turns: 4": "max_tool_turns: { default: 4, max: 30 }"},
    )
    with pytest.raises(SpecError) as excinfo:
        load_spec(consumer)
    rendered = excinfo.value.render()
    assert "cannot be banded" in rendered
    assert "never capability" in rendered


def test_tuning_an_agent_the_command_does_not_run_is_refused(consumer):
    edit(consumer, "pipeline.yaml", {"      reviewer:": "      ghost:"})
    with pytest.raises(MissingDefinition) as excinfo:
        load_spec(consumer)
    assert "which it does not run" in excinfo.value.render()


def test_a_band_with_no_limits_is_refused(consumer):
    """Publishing an unlimited band means a consumer may set anything, which is not publishing one."""
    edit(
        consumer,
        ".pipeline/inherited/review/agents/reviewer.md",
        {"timeout-minutes: { default: 20, max: 60 }": "timeout-minutes: { default: 20 }"},
    )
    with pytest.raises(SpecError) as excinfo:
        load_spec(consumer)
    assert "band with no limits" in excinfo.value.render()


def test_a_raised_band_still_has_to_fit_the_run_budget(consumer):
    edit(consumer, "pipeline.yaml", {"per_run_ai_credits: 200": "per_run_ai_credits: 100"})
    report = doctor(load_spec(consumer), consumer)
    finding = next(f for f in report.findings if f.code == "DOC019")
    assert "can spend 150 credits" in finding.message
    assert "review/reviewer" in finding.hint


def test_what_a_consumer_tuned_is_recorded_for_the_fleet(consumer):
    import json

    manifest = json.loads(compile_spec(consumer).files[".pipeline/compile-manifest.json"])
    assert manifest["tuned"] == {"review/reviewer": {"max-ai-credits": 150, "model": "claude-opus-4-1"}}
    assert manifest["inherits"]["standards"].endswith("upstream-standards")


# --- ceilings: what an upstream sets on agents it will never see -------------
#
# A band bounds a dial on an agent the organization published. A ceiling bounds every agent in a
# consuming repository, including ones it wrote itself — which, before this, were the agents an
# organization had no say over at all.


def test_a_ceiling_reaches_an_agent_the_organization_never_wrote(consumer):
    """The `changelog-writer` is the consumer's own. It still compiles under the standards."""
    agent = yaml.safe_load(
        compile_spec(consumer).files[".github/workflows/aw-changelog-writer.md"].split("---")[1]
    )
    assert agent["max-turns"] == 3
    assert agent["max-ai-credits"] == 40


def test_a_local_agent_over_the_credit_ceiling_is_refused(consumer):
    edit(consumer, "agents/changelog-writer.md", {"max-ai-credits: 40": "max-ai-credits: 400"})
    with pytest.raises(EmitError) as error:
        compile_spec(consumer)
    assert "over the ceiling of 200" in error.value.message


def test_a_local_agent_over_the_turn_ceiling_is_refused(consumer):
    edit(consumer, "agents/changelog-writer.md", {"max_tool_turns: 3": "max_tool_turns: 30"})
    with pytest.raises(EmitError) as error:
        compile_spec(consumer)
    assert "over the ceiling of 8" in error.value.message


def test_the_refusal_says_the_ceiling_is_not_the_consumers_to_move(consumer):
    """The useful half of the message: where the limit came from, and that editing it is upstream's."""
    edit(consumer, "agents/changelog-writer.md", {"max-ai-credits: 40": "max-ai-credits: 400"})
    with pytest.raises(EmitError) as error:
        compile_spec(consumer)
    assert "sealed guardrail" in error.value.hint


def test_an_overlay_cannot_raise_an_agent_past_the_ceiling(consumer):
    """Overlays are a customization tier, not an override of the enforceable half of a guardrail."""
    overlay = consumer / "overlays" / "github" / "raise-it.yml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        "target: workflows/aw-changelog-writer.md\n"
        "frontmatter:\n"
        "  - op: merge\n"
        "    at: max-ai-credits\n"
        "    value: 900\n",
        encoding="utf-8",
    )
    with pytest.raises(EmitError) as error:
        compile_spec(consumer)
    assert "over the ceiling of 200" in error.value.message


# --- the ceiling that actually bounds a bill --------------------------------


def test_a_run_budget_over_the_cap_is_refused(consumer):
    """Per-agent ceilings do not bound a bill: a repository under them can add another agent."""
    edit(consumer, "pipeline.yaml", {"per_run_ai_credits: 200": "per_run_ai_credits: 2000"})
    with pytest.raises(EmitError) as error:
        compile_spec(consumer)
    assert "over the cap of 200" in error.value.message


def test_no_run_budget_at_all_is_refused_when_a_cap_exists(consumer):
    """Unbounded is not under the cap; it is outside it."""
    edit(consumer, "pipeline.yaml", {"budgets:\n  per_run_ai_credits: 200": "budgets: {}"})
    with pytest.raises(EmitError) as error:
        compile_spec(consumer)
    assert "caps one at 200" in error.value.message


def test_the_lowest_ceiling_wins_not_the_last_one_read(consumer):
    """Two guardrails each setting one are two constraints; honouring only the last honours neither."""
    house = consumer / ".pipeline/inherited/review/guardrails/house.md"
    house.write_text(
        house.read_text().replace(
            "---\n\n",
            "enforce:\n  max-ai-credits: 25\n---\n\n",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(EmitError) as error:
        compile_spec(consumer)
    assert "over the ceiling of 25" in error.value.message


def test_a_second_upstream_cannot_reopen_egress_the_first_closed(consumer):
    """Two upstreams compose as constraints, not as a last-writer-wins settings merge.

    Only `deny-all` is enforced, so any other value clears it — which made this decidable by alias
    order alone: rename the alias, get a different security surface.
    """
    edit(
        consumer,
        ".pipeline/inherited/standards/guardrails/data-handling.md",
        {"  permissions: read-all": "  permissions: read-all\n  network: deny-all"},
    )
    house = consumer / ".pipeline/inherited/review/guardrails/house.md"
    house.write_text(
        house.read_text().replace("---\n\n", "enforce:\n  network: defaults\n---\n\n", 1),
        encoding="utf-8",
    )
    agent = yaml.safe_load(compile_spec(consumer).files[AGENT].split("---")[1])
    assert agent["network"] == {"allowed": []}


# --- the cost of refusing transitivity --------------------------------------


def upstream_inherits(consumer, block):
    """Give the fetched `review` tree an upstream of its own — the team-standards shape."""
    manifest = consumer / ".pipeline/inherited/review/pipeline.yaml"
    manifest.write_text(manifest.read_text() + block, encoding="utf-8")


def test_an_upstream_of_an_upstream_this_repository_lacks_is_reported(consumer):
    """A team publishes under an organization's standards; a consumer takes the team and forgets it.

    Nothing else catches this. It compiles, lints and doctors clean while the team's agents arrive
    with the organization's sealed guardrails stripped out.
    """
    upstream_inherits(consumer, "\ninherits:\n  org: github.com/acme/org-standards@v3.2.0\n")
    report = doctor(load_spec(consumer), consumer)
    finding = next(f for f in report.findings if f.code == "DOC022")
    assert finding.severity is Severity.WARNING
    assert "'review' inherits 'org'" in finding.message
    assert "github.com/acme/org-standards@v3.2.0" in finding.message
    assert "not transitive" in finding.hint


def test_it_is_quiet_when_this_repository_inherits_it_too(consumer):
    """The recommended shape: both upstreams named directly. Nothing to say."""
    upstream_inherits(consumer, "\ninherits:\n  org: ../upstream-standards\n")
    assert "DOC022" not in {f.code for f in doctor(load_spec(consumer), consumer).findings}


def test_a_different_ref_of_the_same_upstream_still_counts_as_having_it(consumer):
    """A consumer one version behind has the standards; it is not missing them."""
    edit(
        consumer,
        "pipeline.yaml",
        {"standards: ../upstream-standards": "standards: github.com/acme/std@v3.1.0"},
    )
    upstream_inherits(consumer, "\ninherits:\n  org: github.com/acme/std@v3.9.9\n")
    assert "DOC022" not in {f.code for f in doctor(load_spec(consumer), consumer).findings}


def test_an_unfetched_upstream_is_not_also_a_transitivity_warning(consumer):
    """One missing tree should produce one clear error, not a second confusing warning."""
    import shutil

    shutil.rmtree(consumer / ".pipeline/inherited/review")
    codes = set()
    try:
        codes = {f.code for f in doctor(load_spec(consumer), consumer).findings}
    except MissingDefinition:
        pass
    assert "DOC022" not in codes

