"""The receipt is derived, canonical, and honest about absence.

Three properties carry the whole idea, and each has a way of quietly failing:

**Derived.** Every field is read off an object that already declares it. A receipt that took an
author's word for what their code does would be a README with a hash on it.

**Canonical.** A receipt exists to be compared — published against re-derived — so two runs over
the same configuration must serialise identically, and a changed binding must change the digest.

**Honest about absence.** A repository with no corpus reports `null`, never the framework's own
case count. That field is how a reader learns nothing bound here has been measured, and borrowing
the shipped evidence would turn it into the reassuring number this project keeps refusing.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from in_lockstep.adapters.ai import AiReview, Review
from in_lockstep.ai.prompt import PromptLayers
from in_lockstep.cli import main
from in_lockstep.core.context import RepoInfo
from in_lockstep.core.policy import Policy
from in_lockstep.core.types import Test
from in_lockstep.lockstep import Lockstep
from in_lockstep.prompts.review import LENSES, review_layers
from in_lockstep.receipt import canonical, digest, receipt_for, render


def _lockstep(root: Path) -> Lockstep:
    step = Lockstep(repo=RepoInfo(root=str(root)))
    step.config_source = "test"
    return step


def test_capabilities_come_off_the_bound_object(tmp_path: Path) -> None:
    """The set every gate reads is the set the receipt reports.

    `ApprovalGate`, the budget refusal and `Retry` all key on `capabilities` off the bound
    adapter. A receipt keyed on anything else — the class, a declared kind, a manifest — would
    describe a different adapter than the one that runs, which is the failure mode that makes
    published metadata worthless.
    """
    step = _lockstep(tmp_path)
    step.bind(Review, AiReview())
    receipt = receipt_for(step, root=tmp_path)

    review = next(b for b in receipt["bindings"] if b["interface"] == "Review")
    assert review["implementation"] == "AiReview"
    assert review["verb"] == "review"
    assert review["capabilities"] == ["reads_repo", "spends_budget"]
    assert review["tier"] == "explicit"


def test_a_replaced_layer_stack_is_flagged_rather_than_refused(tmp_path: Path) -> None:
    """Constructing a fresh `PromptLayers` drops the shipped baseline. That is legal — and it is
    the one thing a reader of somebody else's extension most needs told, so it is a field."""
    step = _lockstep(tmp_path)
    step.bind(Review, AiReview(layers=PromptLayers(guardrails=(("acme/only-ours", "Do not xxx."),))))
    receipt = receipt_for(step, root=tmp_path)

    security = next(p for p in receipt["prompts"] if p["label"] == "review/security")
    assert security["guardrails_intact"] is False
    assert security["projection"][0] == "guardrail:acme/only-ours"
    assert "baseline does not lead" in "\n".join(render(receipt))


def test_appending_a_house_guardrail_keeps_the_baseline_intact(tmp_path: Path) -> None:
    """The other side of the same field: `plus` appends, so extending cannot silently drop it."""
    step = _lockstep(tmp_path)
    layers = review_layers().plus(guardrails=(("acme/house", "Never touch migrations."),))
    step.bind(Review, AiReview(lenses=LENSES, layers=layers))
    receipt = receipt_for(step, root=tmp_path)

    security = next(p for p in receipt["prompts"] if p["label"] == "review/security")
    assert security["guardrails_intact"] is True
    assert security["projection"][-1].startswith("skill:")


def test_no_corpus_is_null_and_never_the_frameworks_own(tmp_path: Path) -> None:
    """`0` would read as a measurement. `None` is what says nothing here has been measured."""
    step = _lockstep(tmp_path)
    receipt = receipt_for(step, root=tmp_path)
    assert receipt["corpus"] is None
    assert "nothing bound here has been measured" in "\n".join(render(receipt))


def test_a_repositorys_own_corpus_is_counted_by_what_can_be_settled(tmp_path: Path) -> None:
    """Deterministic and rubric counted apart, because a rubric nobody judged is not a pass."""
    case = tmp_path / "corpus" / "review" / "security-reviewer" / "finds-it.json"
    case.parent.mkdir(parents=True)
    case.write_text(json.dumps({"name": "finds-it", "input": {}, "expect": {"contains": ["sql"]}}))
    rubric = case.parent / "judged.json"
    rubric.write_text(
        json.dumps({"name": "judged", "input": {}, "expect": {"rubric": "names the mechanism"}})
    )

    receipt = receipt_for(_lockstep(tmp_path), root=tmp_path)
    assert receipt["corpus"] == {
        "path": "corpus",
        "cases": 2,
        "deterministic": 1,
        "rubric": 1,
        "families": {"review": 2},
    }


def test_an_unregistered_provider_is_unknown_rather_than_unpriced(tmp_path: Path) -> None:
    """Absent is not zero, applied to pricing: a route this cannot check is `None`, so a machine
    without credentials is not reported as a repository with a broken route."""
    step = _lockstep(tmp_path)
    step.models.route("review", "nosuchprovider:model-7")
    step.models.route("triage", "anthropic:no-such-model-9")
    step.models.route("implement", "anthropic:claude-sonnet-4-6")
    receipt = receipt_for(step, root=tmp_path)

    # All three states, asserted together: a test that only ever saw `None` would pass just as
    # well against an implementation that reported everything as unknown.
    assert {route["verb"]: route["priced"] for route in receipt["models"]} == {
        "review": None,  # the provider is not registered — this cannot know
        "triage": False,  # registered provider, no rate — a run would be refused
        "implement": True,
    }
    rendered = "\n".join(render(receipt))
    assert "unknown" in rendered and "UNPRICED" in rendered


def test_the_digest_is_over_the_canonical_form_and_excludes_itself(tmp_path: Path) -> None:
    """A receipt is compared by recomputation, so it may not hash its own hash."""
    step = _lockstep(tmp_path)
    step.bind(Review, AiReview())
    receipt = receipt_for(step, root=tmp_path)

    assert receipt["digest"] == digest(receipt), "recomputation over the full receipt agrees"
    assert receipt["digest"] not in canonical({k: v for k, v in receipt.items() if k != "digest"})
    assert canonical(receipt) == canonical(receipt_for(step, root=tmp_path)), "stable across runs"


def test_a_changed_binding_changes_the_digest(tmp_path: Path) -> None:
    """The property the install-time comparison rests on: drift is one string comparison."""
    plain = _lockstep(tmp_path)
    plain.bind(Review, AiReview())

    house = _lockstep(tmp_path)
    house.bind(Review, AiReview(layers=PromptLayers(guardrails=(("acme/only-ours", "x"),))))

    assert receipt_for(plain, root=tmp_path)["digest"] != receipt_for(house, root=tmp_path)["digest"]


def test_policy_layers_keep_their_source(tmp_path: Path) -> None:
    """Who asked for what, not only what was merged: a contribution stamped `plugin:acme` is how a
    reader sees that an organisation's floor applied at all."""
    step = _lockstep(tmp_path)
    step.contribute(Policy(name="acme-baseline", source="plugin:acme", scan_input="block", max_turns=8))
    receipt = receipt_for(step, root=tmp_path)

    assert {"name": "acme-baseline", "source": "plugin:acme"} in receipt["policy"]["layers"]
    assert receipt["policy"]["resolved"]["scan_input"] == "block"
    assert receipt["policy"]["resolved"]["max_turns"] == 8


def test_cli_describe_prints_json_that_round_trips(tmp_path: Path, monkeypatch) -> None:
    """`--json` is the artifact an index would store; the digest must survive a parse."""
    monkeypatch.chdir(tmp_path)
    for name in [k for k in __import__("os").environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)

    result = CliRunner().invoke(main, ["pack", "describe", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["receipt"] == 1
    assert payload["digest"] == digest(payload)


def test_cli_describe_reads_the_repositorys_own_module(tmp_path: Path, monkeypatch) -> None:
    """The subject is the configuration, so the module has to be the thing described."""
    monkeypatch.chdir(tmp_path)
    for name in [k for k in __import__("os").environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    module = tmp_path / ".lockstep" / "lockstep.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.adapters import PytestTest\n"
        "from in_lockstep.core.types import Test\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.bind(Test, PytestTest())\n"
    )

    result = CliRunner().invoke(main, ["pack", "describe"])
    assert result.exit_code == 0, result.output
    assert "Test" in result.output and "PytestTest" in result.output
    assert "executes_code" in result.output, "what it may do, read off the bound object"


def test_test_binding_is_reported_without_an_ai_adapter(tmp_path: Path) -> None:
    """A repository binding only deterministic verbs still gets a receipt — and no prompts block,
    because it composes none. An empty section would imply the question was asked and answered."""
    from in_lockstep.adapters import PytestTest

    step = _lockstep(tmp_path)
    step.bind(Test, PytestTest())
    receipt = receipt_for(step, root=tmp_path)
    assert receipt["prompts"] == []
    assert "prompts" not in "\n".join(render(receipt))
