"""The catalog, and the three things it is not.

It is not a service — an `index.toml` in a git repository, read at search and accept time and never
during a run. It is not a source of truth — its receipts are what an author's code did, re-derived
locally and refused when they disagree. And it is not an endorsement — its criteria say a pack can
be measured before it is trusted, which is a different sentence from "this code is good".

The tests that matter most here are the refusals: an index that grows a key it may not carry, a
source that is not https, and an entry pointing at a file outside the repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.cli import main
from in_lockstep.market import (
    CRITERIA,
    Entry,
    MarketError,
    Source,
    add_source,
    check_url,
    criteria_failures,
    parse_index,
    read_catalog,
    receipt_at,
    sources,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_INDEX = ROOT / "examples" / "lockstep-index" / "index.toml"

INDEX = """
[index]
criteria = true

[[pack]]
name         = "acme-review-prompts"
distribution = "acme-review-prompts"
kind         = "prompt"
summary      = "House review prose"
receipt      = "receipts/acme.json"
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in [k for k in __import__("os").environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _catalog(root: Path, text: str = INDEX, name: str = "acme") -> None:
    """A catalog on disk, registered by writing the sources file directly.

    `market add` refuses anything but https, deliberately, so a test that wants a local catalog
    writes the committed file itself — which is also how a repository would carry a catalog it
    vendored rather than fetched.
    """
    (root / "index.toml").write_text(text)
    lockstep = root / ".lockstep"
    lockstep.mkdir(exist_ok=True)
    (lockstep / "market.toml").write_text(f'[source."{name}"]\nurl = "index.toml"\n')


# -- what an index may say --------------------------------------------------------------


def test_an_entry_may_not_carry_a_key_that_configures() -> None:
    """The refusal `pack.toml` makes, for the same reason: a key silently accepted arrives, gets
    documented, and becomes load-bearing. An index describes; it does not configure."""
    text = INDEX + '\nbind = "Implement"\n'
    with pytest.raises(MarketError, match="bind"):
        parse_index(text, Source("acme", "https://example.test/index.toml"))


def test_an_entry_without_a_distribution_is_refused() -> None:
    with pytest.raises(MarketError, match="name and a distribution"):
        parse_index('[[pack]]\nname = "x"\n', Source("acme", "https://example.test/i.toml"))


def test_entries_come_back_in_name_order() -> None:
    text = '[[pack]]\nname = "z"\ndistribution = "z"\n\n[[pack]]\nname = "a"\ndistribution = "a"\n'
    catalog = parse_index(text, Source("acme", "https://example.test/i.toml"))
    assert [entry.name for entry in catalog.entries] == ["a", "z"]


def test_a_catalog_states_its_criteria_or_states_none() -> None:
    """A tap with no criteria is a legitimate thing to be: an internal pack is trusted by having
    been published inside the company, which is a different question and a better answer."""
    stated = parse_index(INDEX, Source("acme", "https://e.test/i.toml"))
    tap = parse_index('[[pack]]\nname = "x"\ndistribution = "x"\n', Source("t", "https://e.test/i"))
    assert stated.criteria is True
    assert tap.criteria is False


# -- where a catalog may come from -------------------------------------------------------


@pytest.mark.parametrize("url", ["http://example.test/i.toml", "file:///etc/passwd", "example.test"])
def test_only_https_registers(url: str) -> None:
    """A catalog says what code to install. Over plain http that description is whatever the
    network says it is, and the receipt comparison would be checking an attacker's document."""
    with pytest.raises(MarketError, match="https"):
        check_url(url)


def test_https_registers_and_the_file_is_committed(repo: Path) -> None:
    path = add_source(repo, "acme", "https://example.test/index.toml")
    assert path == repo / ".lockstep" / "market.toml"
    assert sources(repo) == [Source("acme", "https://example.test/index.toml")]
    assert "committed and" in path.read_text(), "the file says why it is in the repository"


def test_registering_the_same_name_replaces_its_url(repo: Path) -> None:
    add_source(repo, "acme", "https://example.test/one.toml")
    add_source(repo, "acme", "https://example.test/two.toml")
    assert sources(repo) == [Source("acme", "https://example.test/two.toml")]


def test_a_receipt_may_not_point_outside_the_repository(repo: Path) -> None:
    """An entry's `receipt` arrives from a catalog somebody else wrote, so it is untrusted input
    naming a file this process will open. Containment is cheaper to enforce than to reason about —
    the instinct `ChangeGuard` applies to a path a model proposes."""
    with pytest.raises(MarketError, match="leaves this repository"):
        receipt_at(repo, "../../../etc/passwd")


def test_a_missing_receipt_is_absent_rather_than_an_error(repo: Path) -> None:
    assert receipt_at(repo, "receipts/nothing.json") is None
    assert receipt_at(repo, "") is None


# -- criteria ------------------------------------------------------------------------------


def test_no_receipt_fails_the_first_criterion() -> None:
    assert criteria_failures(Entry(name="x", distribution="x"), None) == [CRITERIA[0]]


def test_a_prompt_pack_carrying_code_fails() -> None:
    """The rule the project's index applies to `kind = "prompt"`, and the reason `imports` is a
    derived fact on every pack rather than a tier."""
    entry = Entry(name="x", distribution="x", kind="prompt")
    receipt = {"imports": "modules", "corpus": {"cases": 1}, "cassettes": ["a"]}
    assert criteria_failures(entry, receipt) == [CRITERIA[1]]


def test_evidence_is_two_criteria_because_it_is_two_things() -> None:
    """A corpus says what to measure; a cassette says measuring costs nothing. A pack with cases
    and no recording can only be measured by somebody who pays for a model call."""
    entry = Entry(name="x", distribution="x", kind="strategy")
    assert criteria_failures(entry, {"imports": "modules"}) == [CRITERIA[2], CRITERIA[3]]
    assert criteria_failures(entry, {"imports": "modules", "corpus": {"cases": 2}, "cassettes": []}) == [
        CRITERIA[3]
    ]
    assert not criteria_failures(
        entry, {"imports": "modules", "corpus": {"cases": 2}, "cassettes": ["review"]}
    )


# -- the worked example ----------------------------------------------------------------------


def test_the_example_catalog_parses_and_states_criteria() -> None:
    catalog = parse_index(EXAMPLE_INDEX.read_text(), Source("project", str(EXAMPLE_INDEX)))
    assert catalog.criteria is True
    assert [entry.name for entry in catalog.entries] == ["acme-review-prompts"]
    assert catalog.entries[0].receipt == "receipts/acme-review-prompts-1.0.0.json"


def test_the_examples_receipt_is_a_real_derivation() -> None:
    """Committed beside the index and derived by `pack describe`, so it records what the pack's
    code did. A hand-written receipt would make the comparison at `add` time meaningless."""
    from in_lockstep.receipt import digest

    receipt = json.loads((EXAMPLE_INDEX.parent / "receipts" / "acme-review-prompts-1.0.0.json").read_text())
    assert receipt["digest"] == digest(receipt), "not derived, or edited afterwards"
    assert receipt["subject"]["name"] == "acme-review-prompts"
    assert receipt["imports"] == "none"


def test_the_example_catalog_fails_its_own_criteria_for_one_stated_reason() -> None:
    """On purpose, and documented in its README: the pack ships prose and cases, and nobody has
    recorded a replay, so it cannot be measured for nothing. A lint that always passes teaches
    nothing about what the check is for — and fabricating a cassette to make it green would be
    inventing the evidence this project exists to refuse to invent."""
    result = CliRunner().invoke(main, ["market", "lint", str(EXAMPLE_INDEX)])
    assert result.exit_code != 0
    assert "1 failing" in result.output
    assert CRITERIA[3] in result.output
    assert CRITERIA[2] not in result.output, "it does ship a corpus"


# -- the CLI ------------------------------------------------------------------------------------


def test_market_add_refuses_a_url_that_can_be_rewritten(repo: Path) -> None:
    result = CliRunner().invoke(main, ["market", "add", "acme", "http://example.test/i.toml"])
    assert result.exit_code != 0
    assert "https" in result.output
    assert not (repo / ".lockstep" / "market.toml").exists(), "a refusal registers nothing"


def test_market_ls_says_how_one_arrives(repo: Path) -> None:
    result = CliRunner().invoke(main, ["market", "ls"])
    assert result.exit_code == 0, result.output
    assert "no catalogs registered" in result.output
    assert "market add" in result.output


def test_search_groups_by_source_and_says_which_states_criteria(repo: Path) -> None:
    """The difference is the point: one catalog states entry criteria and a tap states none."""
    _catalog(repo)
    result = CliRunner().invoke(main, ["search", "review"])
    assert result.exit_code == 0, result.output
    assert "acme  (states entry criteria)" in result.output
    assert "acme-review-prompts" in result.output


def test_search_reports_a_name_two_catalogs_claim(repo: Path) -> None:
    """Guessing which one somebody meant is how the wrong code gets installed under the right
    name, so this reports and refuses to resolve."""
    _catalog(repo)
    (repo / "other.toml").write_text(INDEX)
    (repo / ".lockstep" / "market.toml").write_text(
        '[source."acme"]\nurl = "index.toml"\n\n[source."internal"]\nurl = "other.toml"\n'
    )
    result = CliRunner().invoke(main, ["search", ""])
    assert result.exit_code == 0, result.output
    assert "is listed by acme, internal" in result.output
    assert "nothing here picks for you" in result.output


def test_search_without_a_catalog_says_so(repo: Path) -> None:
    result = CliRunner().invoke(main, ["search", "tdd"])
    assert result.exit_code != 0
    assert "market add" in result.output


def test_a_catalog_that_cannot_be_read_does_not_stop_the_others(repo: Path) -> None:
    """One broken catalog hiding every other one is the failure `pack ls` already refuses."""
    _catalog(repo)
    (repo / "broken.toml").write_text("[[pack]]\nname = ")
    (repo / ".lockstep" / "market.toml").write_text(
        '[source."acme"]\nurl = "index.toml"\n\n[source."broken"]\nurl = "broken.toml"\n'
    )
    result = CliRunner().invoke(main, ["search", ""])
    assert result.exit_code == 0
    assert "acme-review-prompts" in result.output
    assert "broken" in result.output


def test_read_catalog_reads_a_path_relative_to_the_repository(repo: Path) -> None:
    _catalog(repo)
    catalog = read_catalog(sources(repo)[0], root=repo)
    assert [entry.name for entry in catalog.entries] == ["acme-review-prompts"]
