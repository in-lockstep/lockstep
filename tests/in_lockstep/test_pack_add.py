"""`add`: what a repository accepted, recorded where review can see it.

The command does three things and refuses a fourth, and the refusal is the design. It re-derives
the receipt from the code that is installed, compares it with what was accepted before, and
records the result — but it never writes `.lockstep/lockstep.py`, because that file's value is
that a person typed every line of it, and it never installs anything, because putting a stranger's
code on your machine belongs in your dependency diff rather than in a framework.

The line drawn between a refusal and a note is agency. A pack that gained a prompt has changed; a
pack that gained `reaches_network` may now do something this repository never agreed to.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep import doctor
from in_lockstep.cli import main
from in_lockstep.packs import GROUP, Pack
from in_lockstep.receipt import compare, digest, read_record, receipt_for_pack

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "acme-review-prompts" / "acme_review_prompts"

STRATEGY = """
from typing import ClassVar
from in_lockstep.adapters.ai import ImplementStrategy
from in_lockstep.core.verbs import Capability
from in_lockstep.adapters.ai.strategy import AGENCY


class AcmeTDD(ImplementStrategy):
    id: ClassVar[str] = "acme-tdd-pro/tdd"
    capabilities: ClassVar[frozenset] = AGENCY{extra}

    async def invoke(self, ctx, request):
        raise NotImplementedError
"""


class FakeEntry:
    def __init__(self, name: str, module: str, root: Path, version: str = "1.0.0") -> None:
        self.name = name
        self.value = module
        self.root = root
        self.dist = type("Dist", (), {"name": name, "version": version})()

    def load(self):  # pragma: no cover - nothing in these paths may import through the entry point
        raise AssertionError("add must not load a pack through its entry point")


@pytest.fixture(autouse=True)
def _clean_verbs():
    from in_lockstep.core.verbs import Verb

    yield
    Verb.forget_custom()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in [k for k in __import__("os").environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _install(monkeypatch: pytest.MonkeyPatch, *entries: FakeEntry) -> None:
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda *, group: list(entries) if group == GROUP else [],
    )


def _strategy_pack(
    monkeypatch: pytest.MonkeyPatch, root: Path, *, extra: str = "", version: str = "1.0.0"
) -> FakeEntry:
    """A pack on disk whose capabilities can be widened between calls.

    On `sys.path` because `receipt_for_pack` imports a pack that reports `imports: modules` — that
    import is the whole reason `offers` is a fact rather than a claim.
    """
    monkeypatch.syspath_prepend(str(root))
    module = root / "acme_tdd_pro"
    module.mkdir(exist_ok=True)
    (module / "pack.toml").write_text('[pack]\nkind = "strategy"\nsummary = "TDD, harder"\n')
    (module / "__init__.py").write_text(STRATEGY.format(extra=extra))
    sys.modules.pop("acme_tdd_pro", None)
    importlib.invalidate_caches()
    return FakeEntry("acme-tdd-pro", "acme_tdd_pro", module, version=version)


def _example(monkeypatch: pytest.MonkeyPatch) -> FakeEntry:
    return FakeEntry("acme-review-prompts", "acme_review_prompts", EXAMPLE)


# -- the record ------------------------------------------------------------------------


def test_add_records_what_was_accepted_and_prints_what_to_paste(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _example(monkeypatch))
    result = CliRunner().invoke(main, ["add", "acme-review-prompts"])
    assert result.exit_code == 0, result.output

    record = repo / ".lockstep" / "packs" / "acme-review-prompts.json"
    assert record.is_file(), "the record is the acknowledgement, so it has to exist"
    payload = json.loads(record.read_text())
    assert payload["subject"]["name"] == "acme-review-prompts"
    assert payload["digest"] == digest(payload), "recorded canonically, so a diff reads as a change"
    assert "commit it" in result.output


def test_gate_pack_4_add_never_writes_the_lifecycle_module(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-PACK-4. The decision this command is built around. That file can rebind any adapter, remove any
    middleware and grant any tool; a tool that types into it spends the property that makes it
    trustworthy — and there is deliberately no flag that turns this off."""
    module = repo / ".lockstep" / "lockstep.py"
    module.parent.mkdir(parents=True)
    module.write_text("# untouched\n")
    before = module.read_text()

    _install(monkeypatch, _example(monkeypatch))
    for argv in (["add", "acme-review-prompts"], ["add", "acme-review-prompts", "--accept"]):
        assert CliRunner().invoke(main, argv).exit_code == 0
    assert module.read_text() == before


def test_a_pack_that_is_not_installed_says_how_one_arrives(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    result = CliRunner().invoke(main, ["add", "nosuch"])
    assert result.exit_code != 0
    assert "uv add" in result.output


# -- widening is the line ---------------------------------------------------------------


def test_gate_pack_3_widened_capabilities_are_refused_and_record_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-PACK-3. More agency is exactly the change that should cost a decision — and a refusal
    must not leave the repository having accepted what it declined."""
    _install(monkeypatch, _strategy_pack(monkeypatch, repo))
    assert CliRunner().invoke(main, ["add", "acme-tdd-pro"]).exit_code == 0

    _install(
        monkeypatch,
        _strategy_pack(monkeypatch, repo, extra=" | {Capability.REACHES_NETWORK}", version="2.0.0"),
    )
    refused = CliRunner().invoke(main, ["add", "acme-tdd-pro"])
    assert refused.exit_code != 0
    assert "+ reaches_network" in refused.output
    assert "--accept" in refused.output

    recorded = read_record(repo, "acme-tdd-pro")
    assert recorded is not None
    assert "reaches_network" not in json.dumps(recorded), "a refusal must not record the thing refused"


def test_accept_records_the_wider_set(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _strategy_pack(monkeypatch, repo))
    CliRunner().invoke(main, ["add", "acme-tdd-pro"])

    _install(
        monkeypatch,
        _strategy_pack(monkeypatch, repo, extra=" | {Capability.REACHES_NETWORK}", version="2.0.0"),
    )
    accepted = CliRunner().invoke(main, ["add", "acme-tdd-pro", "--accept"])
    assert accepted.exit_code == 0, accepted.output
    assert "accepted, because --accept said so" in accepted.output

    recorded = read_record(repo, "acme-tdd-pro")
    assert recorded is not None
    assert "reaches_network" in json.dumps(recorded)


def test_a_change_that_grants_nothing_new_is_recorded_without_a_flag(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version bump is a change and not a grant. Refusing over one would teach people to pass
    `--accept` by reflex, which is how the flag that guards agency stops meaning anything."""
    _install(monkeypatch, _strategy_pack(monkeypatch, repo))
    CliRunner().invoke(main, ["add", "acme-tdd-pro"])

    _install(monkeypatch, _strategy_pack(monkeypatch, repo, version="1.1.0"))
    result = CliRunner().invoke(main, ["add", "acme-tdd-pro"])
    assert result.exit_code == 0, result.output
    assert "changed  version 1.0.0 -> 1.1.0" in result.output


def test_compare_reports_a_change_it_cannot_name() -> None:
    """A digest that moved with every named comparison agreeing is still a change, and saying so
    vaguely beats saying nothing — the alternative is a receipt that quietly differs."""
    before = {"subject": {"version": "1"}, "imports": "none", "offers": [], "digest": "sha256:a"}
    after = {"subject": {"version": "1"}, "imports": "none", "offers": [], "digest": "sha256:b"}
    drift = compare(before, after)
    assert drift.widened == ()
    assert any("no named comparison covers" in change for change in drift.changes)


# -- the lines it prints ------------------------------------------------------------------


def test_a_strategy_pack_is_offered_use_not_bind(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`use()` completes the worktree wrap and the policy floor that a hand-written bind can drop
    — so the line printed for a strategy is the one that cannot silently omit them."""
    _install(monkeypatch, _strategy_pack(monkeypatch, repo))
    result = CliRunner().invoke(main, ["add", "acme-tdd-pro"])
    assert "from acme_tdd_pro import AcmeTDD" in result.output
    assert "implement = lockstep.use(AcmeTDD)" in result.output


def test_a_data_pack_is_offered_the_resource_shape(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where a body belongs depends on the lens it replaces, so this prints a shape rather than
    pretending to know which prompt somebody meant to override."""
    _install(monkeypatch, _example(monkeypatch))
    result = CliRunner().invoke(main, ["add", "acme-review-prompts"])
    assert "acme_review_prompts = pack('acme-review-prompts')" in result.output
    assert "plus()` appends" in result.output or "plus()" in result.output


def test_an_unpinned_pack_is_called_out(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A receipt describes the code installed now; a pin makes that the code installed next time."""
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = []\n')
    _install(monkeypatch, _example(monkeypatch))
    result = CliRunner().invoke(main, ["add", "acme-review-prompts"])
    assert "NOT PINNED" in result.output


# -- doctor --------------------------------------------------------------------------------


def _codes(report) -> dict[str, str]:
    return {check.code: check.severity.value for check in report.checks}


def test_doc170_fails_when_a_pack_may_do_more_than_was_accepted(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _strategy_pack(monkeypatch, repo))
    CliRunner().invoke(main, ["add", "acme-tdd-pro"])

    _install(
        monkeypatch,
        _strategy_pack(monkeypatch, repo, extra=" | {Capability.REACHES_NETWORK}", version="2.0.0"),
    )
    report = doctor.run(repo)
    assert _codes(report).get("DOC170") == "error"
    finding = next(c for c in report.checks if c.code == "DOC170")
    assert "reaches_network" in finding.message
    assert "--accept" in finding.hint


def test_doc170_is_only_a_note_for_a_pack_nobody_accepted(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing offers a pack; it does not apply one. An unaccepted pack that nothing binds is
    ordinary, and failing over it would make the group's whole premise read as a problem."""
    _install(monkeypatch, _example(monkeypatch))
    report = doctor.run(repo)
    assert _codes(report).get("DOC170") == "note"


def test_doc172_warns_about_an_unpinned_pack(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = []\n')
    _install(monkeypatch, _example(monkeypatch))
    assert _codes(doctor.run(repo)).get("DOC172") == "warning"


def test_doc171_warns_when_a_bound_prompt_drops_the_baseline(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATE-PACK-2, the doctor half.

    It reads the BOUND adapters rather than the installed packs, because what matters is the
    prompt a run would actually send — a pack nobody bound sends nothing."""
    module = repo / ".lockstep" / "lockstep.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.adapters.ai import AiReview, Review\n"
        "from in_lockstep.ai.prompt import PromptLayers\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.bind(Review, AiReview(layers=PromptLayers(guardrails=(('acme/only-ours', 'x'),))))\n"
    )
    _install(monkeypatch)
    report = doctor.run(repo)
    assert _codes(report).get("DOC171") == "warning"
    warned = [c for c in report.checks if c.code == "DOC171"]
    finding = warned[0]
    assert {c.message.split()[0] for c in warned} == {
        "review/intent",
        "review/performance",
        "review/security",
        "review/tests",
    }, "every lens the adapter composes is affected, so every one is named"
    assert "show-prompt" in finding.hint


def test_doctor_is_quiet_when_the_accepted_receipt_still_matches(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check has to be able to pass, or it is noise that teaches people to ignore it."""
    entry = _strategy_pack(monkeypatch, repo)
    (repo / "uv.lock").write_text('[[package]]\nname = "acme-tdd-pro"\nversion = "1.0.0"\n')
    _install(monkeypatch, entry)
    CliRunner().invoke(main, ["add", "acme-tdd-pro"])

    _install(monkeypatch, _strategy_pack(monkeypatch, repo))
    codes = _codes(doctor.run(repo))
    assert "DOC170" not in codes and "DOC172" not in codes


def test_the_recorded_receipt_is_what_doctor_re_derives_against(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The comparison is possible because the receipt is canonical and derived: recorded once,
    re-derived from the installed code, and the difference is a fact rather than a judgement."""
    entry = _strategy_pack(monkeypatch, repo)
    _install(monkeypatch, entry)
    CliRunner().invoke(main, ["add", "acme-tdd-pro"])

    recorded = read_record(repo, "acme-tdd-pro")
    derived = receipt_for_pack(Pack(name="acme-tdd-pro", module="acme_tdd_pro", root=entry.root))
    assert recorded is not None
    assert compare(recorded, derived).widened == ()
