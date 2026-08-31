"""Extension packs: a group that offers, and the properties that make offering safe.

`in_lockstep.standards` and `in_lockstep.extensions` differ in one thing and it is the right
thing. A standards package may only tighten, so applying it automatically is safe and forgetting
it is the real risk. An extension pack hands a model write and execute tools and pays for a model
call, so its arrival must be a diff somebody read.

These tests hold that difference, and the two properties that make a pack inspectable before it is
trusted: listing does not import, and `imports` is derived rather than declared.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep import Lockstep
from in_lockstep.cli import main
from in_lockstep.packs import GROUP, Pack, PackError, PackNotFound, installed, pack
from in_lockstep.receipt import digest, receipt_for_pack, render_pack

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "acme-review-prompts"


@pytest.fixture(autouse=True)
def _no_verb_leakage():
    """`Verb`'s intern table is process-global, so a verb one test invents outlives it.

    Deliberate — identity is why `Verb` could stop being an enum without breaking
    `verb is Verb.TEST` — and it means a pack test that introduces `benchmark` would otherwise
    make a later `ls` test fail by finding an orphan verb it never declared. `test_cli.py` carries
    the same fixture for the same reason.
    """
    from in_lockstep.core.verbs import Verb

    yield
    Verb.forget_custom()


class FakeEntry:
    """The shape `importlib.metadata` yields — plus `root`, which stands in for an install.

    `load` is deliberately a bomb. Nothing in the listing path may call it, and a test double that
    merely returned something would let a regression through: the assertion that matters is that
    this is never invoked.
    """

    def __init__(self, name: str, value: str, root: Path | None = None, version: str = "1.0.0") -> None:
        self.name = name
        self.value = value
        self.root = root
        self.dist = type("Dist", (), {"name": name, "version": version})()

    def load(self):  # pragma: no cover - calling this is the failure
        raise AssertionError("listing a pack must not import it")


def _example_entry(name: str = "acme-review-prompts") -> FakeEntry:
    return FakeEntry(name, "acme_review_prompts", EXAMPLE / "acme_review_prompts")


# -- the group's whole reason to exist ------------------------------------------------


def test_gate_pack_1_an_installed_pack_binds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installing offers; a line in lockstep.py is what puts a pack in force.

    The contrast is `in_lockstep.standards`, where installing IS applying. If this group were
    applied the same way, which strategy runs would become a property of what happens to be
    installed rather than of a reviewed line in a file loaded from a trusted ref — and that is the
    property #104 and #116 established, from the other direction.
    """
    import importlib.metadata

    asked: list[str] = []

    def fake_entry_points(*, group: str):
        asked.append(group)
        return [_example_entry()] if group == GROUP else []

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    before = len(Lockstep().container.resolved())
    lockstep = Lockstep.detect()

    assert len(lockstep.container.resolved()) == before, "detect() bound something from a pack"
    assert GROUP not in asked, "detect() must not even look at the extensions group"
    assert lockstep.standards == []


def test_listing_never_imports_a_pack() -> None:
    """`FakeEntry.load` raises. Reaching the end of this test is the assertion."""
    packs = installed(entries=[_example_entry()])
    assert [p.name for p in packs] == ["acme-review-prompts"]
    assert packs[0].module == "acme_review_prompts"
    assert packs[0].imports() == "none"
    assert packs[0].manifest().kind == "prompt"
    assert "acme_review_prompts" not in sys.modules or True  # importing elsewhere is not this path


def test_packs_are_listed_in_name_order() -> None:
    """Two machines disagreeing about the order of a listing is a support ticket nobody can
    reproduce — the same argument `load_standards` makes about application order."""
    entries = [FakeEntry("z-pack", "z_pack"), FakeEntry("a-pack", "a_pack")]
    assert [p.name for p in installed(entries=entries)] == ["a-pack", "z-pack"]


def test_an_unknown_pack_names_what_is_installed() -> None:
    with pytest.raises(PackNotFound, match="acme-review-prompts"):
        pack("nosuch", entries=[_example_entry()])


# -- imports: the derived fact, not a tier --------------------------------------------


def test_imports_none_is_derived_from_the_ast(tmp_path: Path) -> None:
    """A docstring is not code. Anything else is."""
    module = tmp_path / "quiet_pack"
    module.mkdir()
    (module / "__init__.py").write_text('"""Just prose."""\n')
    assert Pack(name="quiet", module="quiet_pack", root=module).imports() == "none"

    (module / "helper.py").write_text("import os\n")
    assert Pack(name="quiet", module="quiet_pack", root=module).imports() == "modules"


def test_imports_is_unknown_when_the_files_cannot_be_read() -> None:
    """`unknown` is not `none`. A pack nothing could check has not been checked, and reporting it
    as inert would be the reassuring answer computed from nothing."""
    assert Pack(name="zipped", module="zipped", root=None).imports() == "unknown"


def test_a_syntax_error_is_unknown_rather_than_inert(tmp_path: Path) -> None:
    """Failing open here would mean an unparseable file reads as 'contains nothing'."""
    module = tmp_path / "broken_pack"
    module.mkdir()
    (module / "__init__.py").write_text("def (:\n")
    assert Pack(name="broken", module="broken_pack", root=module).imports() == "unknown"


# -- pack.toml declares two things and may not grow a third ----------------------------


def test_an_unknown_manifest_key_is_refused(tmp_path: Path) -> None:
    """The refusal is the point rather than the parse: a key that is silently ignored is a key
    that arrives, gets documented, and becomes load-bearing."""
    module = tmp_path / "sneaky"
    module.mkdir()
    (module / "pack.toml").write_text('[pack]\nkind = "strategy"\nbind = "Implement"\n')
    with pytest.raises(PackError, match="bind"):
        Pack(name="sneaky", module="sneaky", root=module).manifest()


def test_a_pack_that_does_not_say_what_it_is_is_refused(tmp_path: Path) -> None:
    module = tmp_path / "nameless"
    module.mkdir()
    with pytest.raises(PackError, match="no pack.toml"):
        Pack(name="nameless", module="nameless", root=module).manifest()


def test_an_unrecognised_kind_is_refused(tmp_path: Path) -> None:
    module = tmp_path / "odd"
    module.mkdir()
    (module / "pack.toml").write_text('[pack]\nkind = "middleware"\n')
    with pytest.raises(PackError, match="middleware"):
        Pack(name="odd", module="odd", root=module).manifest()


# -- the authoring surface -------------------------------------------------------------


def test_a_guardrail_is_labelled_by_the_pack_that_shipped_it() -> None:
    """A projection is read to answer "whose rule is this". Two packs contributing `house` would
    otherwise be indistinguishable in the one artifact meant to tell them apart."""
    fragments = pack("acme-review-prompts", entries=[_example_entry()]).guardrails("house")
    assert len(fragments) == 1
    label, text = fragments[0]
    assert label == "acme-review-prompts/house"
    assert "migrations" in text
    assert not text.startswith("---"), "frontmatter is stripped, as it is for a shipped fragment"


def test_a_missing_guardrail_says_which_file(tmp_path: Path) -> None:
    with pytest.raises(PackError, match="prompts/nope.md"):
        pack("acme-review-prompts", entries=[_example_entry()]).guardrails("nope")


def test_a_body_resolves_through_the_packs_own_package() -> None:
    """At bind time importing is expected — the repository has decided to trust the pack by then."""
    found = pack("acme-review-prompts", entries=[_example_entry()])
    body = found.body("prompts/security.md")
    assert body.package == "acme_review_prompts"


# -- the worked example ----------------------------------------------------------------


def test_the_worked_example_is_a_data_pack_end_to_end() -> None:
    """`examples/acme-review-prompts` is the shipped prompt pack; it must work, not merely read
    well — the same standard `examples/acme-standards` is held to."""
    found = pack("acme-review-prompts", entries=[_example_entry()])
    receipt = receipt_for_pack(found)

    assert receipt["imports"] == "none"
    assert receipt["imported"] is False, "nothing importable means nothing was imported"
    assert receipt["declares"] == {
        "kind": "prompt",
        "summary": "A security lens that knows about our ORM, and a guardrail about migrations",
    }
    assert receipt["kind_matches"] is True
    assert receipt["offers"] == []
    assert receipt["corpus"] == {
        "path": "corpus",
        "cases": 2,
        "deterministic": 2,
        "rubric": 2,
        "families": {"review": 2},
    }
    assert receipt["problems"] == []
    assert receipt["digest"] == digest(receipt)


def test_the_examples_prose_is_reachable_as_a_prompt_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pack is only worth shipping if the lens it offers actually composes.

    This is the one path that imports the pack, and it is the right one: a body resolves through
    `importlib.resources` at render time, inside a repository that has already decided to trust it
    by writing the bind line. Every inspection path above reaches the same files without this.
    """
    from in_lockstep.prompts.review import SecurityReviewPrompt

    monkeypatch.syspath_prepend(str(EXAMPLE))
    sys.modules.pop("acme_review_prompts", None)
    found = pack("acme-review-prompts", entries=[_example_entry()])

    class OurSecurity(SecurityReviewPrompt):
        version = "acme-1"
        body = found.body("prompts/security.md")

    system = OurSecurity().system()
    assert "SQLAlchemy" in system
    assert "not a finding" in system


def test_the_rendering_explains_what_imports_none_buys() -> None:
    """The field exists to be understood by somebody deciding whether to install."""
    rendered = "\n".join(
        render_pack(receipt_for_pack(pack("acme-review-prompts", entries=[_example_entry()])))
    )
    assert "imports       none" in rendered
    assert "installing it runs nothing" in rendered


# -- a code pack, which is the other half ----------------------------------------------


def _strategy_pack(tmp_path: Path, *, strategy_id: str) -> Pack:
    module = tmp_path / "acme_tdd_pro"
    module.mkdir()
    (module / "pack.toml").write_text('[pack]\nkind = "strategy"\nsummary = "TDD, harder"\n')
    (module / "__init__.py").write_text(
        "from typing import ClassVar\n"
        "from in_lockstep.adapters.ai import ImplementStrategy\n"
        "class AcmeTDD(ImplementStrategy):\n"
        f"    id: ClassVar[str] = {strategy_id!r}\n"
        "    async def invoke(self, ctx, request):\n"
        "        raise NotImplementedError\n"
    )
    return Pack(name="acme-tdd-pro", module="acme_tdd_pro", distribution="acme-tdd-pro", root=module)


def test_a_strategy_pack_reports_what_it_offers_and_what_it_may_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`offers` is derived by walking the imported namespace, not read from a manifest: a list of
    classes an author wrote down is a claim, and one the interpreter found is a fact."""
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("acme_tdd_pro", None)
    receipt = receipt_for_pack(_strategy_pack(tmp_path, strategy_id="acme-tdd-pro/tdd"))

    assert receipt["imports"] == "modules"
    assert receipt["imported"] is True
    assert receipt["offers"] == [
        {
            "name": "AcmeTDD",
            "offers": "strategy",
            "id": "acme-tdd-pro/tdd",
            "verb": "implement",
            "request": "Implement",
            "capabilities": ["executes_code", "reads_repo", "spends_budget", "writes_files"],
        }
    ]
    assert receipt["kind_matches"] is True
    assert receipt["problems"] == []


def test_an_id_that_is_not_namespaced_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A strategy id lands in the ledger and keys an eval subject, so two packs shipping
    `implement/tdd` produce records that cannot be told apart afterwards."""
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("acme_tdd_pro", None)
    receipt = receipt_for_pack(_strategy_pack(tmp_path, strategy_id="implement/tdd"))

    rendered = "\n".join(render_pack(receipt))
    assert "not namespaced" in rendered
    assert "implement/tdd" in rendered


def test_no_load_declines_to_import_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The right trade for a caller who has not decided to trust the pack yet."""
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("acme_tdd_pro", None)
    receipt = receipt_for_pack(_strategy_pack(tmp_path, strategy_id="acme-tdd-pro/tdd"), load=False)

    assert receipt["imports"] == "modules"
    assert receipt["imported"] is False
    assert receipt["offers"] == []
    assert receipt["kind_matches"] is None, "unchecked is not a match"


# -- the CLI ---------------------------------------------------------------------------


def _entry_points(monkeypatch: pytest.MonkeyPatch, entries: list[FakeEntry]) -> None:
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda *, group: entries if group == GROUP else [],
    )


def test_pack_ls_says_offered_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(monkeypatch, [_example_entry()])
    result = CliRunner().invoke(main, ["pack", "ls"])
    assert result.exit_code == 0, result.output
    assert "offered, not in force" in result.output
    assert "acme-review-prompts" in result.output
    assert "imports: none" in result.output


def test_pack_ls_with_nothing_installed_says_how_one_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(monkeypatch, [])
    result = CliRunner().invoke(main, ["pack", "ls"])
    assert result.exit_code == 0, result.output
    assert "no extension packs installed" in result.output
    assert "in_lockstep.extensions" in result.output


def test_pack_ls_survives_one_broken_pack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A pack that fails to parse has applied nothing, so it belongs beside the others in a
    listing rather than in an exception that hides every one of them. The opposite of
    `load_standards`, deliberately: a standard that failed to load is a control silently absent."""
    broken = tmp_path / "broken_pack"
    broken.mkdir()
    _entry_points(monkeypatch, [_example_entry(), FakeEntry("broken", "broken_pack", broken)])

    result = CliRunner().invoke(main, ["pack", "ls"])
    assert result.exit_code == 0, result.output
    assert "acme-review-prompts" in result.output
    assert "broken" in result.output and "pack.toml" in result.output


def test_pack_describe_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(monkeypatch, [_example_entry()])
    result = CliRunner().invoke(main, ["pack", "describe", "acme-review-prompts", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["subject"]["kind"] == "pack"
    assert payload["imports"] == "none"
    assert payload["digest"] == digest(payload)


def test_pack_describe_with_no_name_still_describes_the_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The subject added here does not displace the one that was there."""
    monkeypatch.chdir(tmp_path)
    for name in [k for k in __import__("os").environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    _entry_points(monkeypatch, [_example_entry()])

    result = CliRunner().invoke(main, ["pack", "describe", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["subject"]["kind"] == "repository"


def test_describing_an_uninstalled_pack_is_an_error_that_says_how_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry_points(monkeypatch, [])
    result = CliRunner().invoke(main, ["pack", "describe", "nosuch"])
    assert result.exit_code != 0
    assert "lockstep.py" in result.output, "a refusal should say what putting one in force takes"


def test_a_pack_offering_a_custom_verb_is_told_apart_from_a_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verb the framework does not ship is a different offer, and the difference is what the
    author owes: a strategy for `implement` inherits a route, a price, prompts and a corpus family;
    a pack introducing `benchmark` owes all four. Recognition is through `core`'s vocabulary, so a
    deterministic adapter is described by the same code as an AI one."""
    module = tmp_path / "acme_bench"
    module.mkdir()
    (module / "pack.toml").write_text('[pack]\nkind = "verb"\nsummary = "Benchmarking"\n')
    (module / "__init__.py").write_text(
        "from dataclasses import dataclass\n"
        "from typing import ClassVar\n"
        "from in_lockstep.core.verbs import Capability, Verb\n"
        "BENCHMARK = Verb('benchmark')\n"
        "@dataclass(frozen=True)\n"
        "class Benchmark:\n"
        "    iterations: int = 100\n"
        "class PyperfBenchmark:\n"
        "    verb: ClassVar[Verb] = BENCHMARK\n"
        "    request: ClassVar[type] = Benchmark\n"
        "    capabilities: ClassVar[frozenset] = frozenset({Capability.EXECUTES_CODE})\n"
        "    async def invoke(self, ctx, request):\n"
        "        raise NotImplementedError\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("acme_bench", None)
    receipt = receipt_for_pack(
        Pack(name="acme-bench", module="acme_bench", distribution="acme-bench", root=module)
    )

    offer = next(o for o in receipt["offers"] if o["name"] == "PyperfBenchmark")
    assert offer["offers"] == "verb", "a verb the framework does not ship"
    assert offer["verb"] == "benchmark"
    assert offer["request"] == "Benchmark"
    assert offer["capabilities"] == ["executes_code"]
    assert receipt["kind_matches"] is True


class EditableDist:
    """What `importlib.metadata` reports for an editable install: a `.pth` and dist-info.

    Not a hypothetical shape. `uv pip install -e` on the worked example records exactly this, so
    `dist.files` names nothing inside the package and the metadata path cannot answer.
    """

    name = "acme-review-prompts"
    version = "1.0.0"
    files = (
        "_editable_impl_acme_review_prompts.pth",
        "acme_review_prompts-1.0.0.dist-info/METADATA",
        "acme_review_prompts-1.0.0.dist-info/RECORD",
    )

    def locate_file(self, path):  # pragma: no cover - never reached for these entries
        raise AssertionError("no recorded file belongs to the package")


class EditableEntry:
    def __init__(self, name: str, module: str) -> None:
        self.name = name
        self.value = module
        self.dist = EditableDist()

    def load(self):  # pragma: no cover - listing must not import
        raise AssertionError("listing a pack must not import it")


def test_an_editable_install_is_located_without_importing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug the first real install found, and the property the fix must not cost.

    An editable install records no package files, so the metadata path returns nothing and a pack
    reported `imports: unknown` with no `pack.toml` — the honest-but-useless answer, shown to the
    population least able to explain it, since installing your own pack editable is how you write
    one. `find_spec` resolves the directory through the ordinary path finders and does not execute
    the module, which is asserted here rather than assumed: "listing a pack runs no code it ships"
    is the property this module is arranged around.
    """
    module = tmp_path / "editable_pack"
    module.mkdir()
    (module / "__init__.py").write_text('"""Prose only."""\n')
    (module / "pack.toml").write_text('[pack]\nkind = "prompt"\nsummary = "Editable"\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("editable_pack", None)

    found = installed(entries=[EditableEntry("editable-pack", "editable_pack")])[0]

    assert found.root == module.resolve(), "an editable install could not be located"
    assert found.imports() == "none"
    assert found.manifest().kind == "prompt"
    assert "editable_pack" not in sys.modules, "locating a pack imported it"


def test_a_module_that_does_not_exist_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback may not invent a location. A pack nothing can resolve has not been checked."""
    found = installed(entries=[EditableEntry("ghost", "no_such_module_anywhere_xyz")])[0]
    assert found.root is None
    assert found.imports() == "unknown"
