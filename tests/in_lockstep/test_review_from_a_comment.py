"""Which lens a `/review` comment asked for.

The body is untrusted, and unlike `--resume` this one chooses which prompt composes. That is why
the answer is resolved against a closed set the repository itself declared: a comment picks among
lenses that already exist, and cannot introduce one. `design/strategy-selection.md` draws the line
in the same place — "It is not selection that is unsafe. It is selection across a capability line"
— and every review lens shares one posture, no tools at all.

The refusal matters as much as the resolution. An aspect that reaches a run id earns a ledger
record, and `blocked` sits inside `failure_rate`'s denominator, so a stream of typos from anyone who
can comment would deflate the repository's failure rate (#203).
"""

from __future__ import annotations

import pytest

from in_lockstep.platform.chatops import AspectRefused, aspect_from

KNOWN = ("security", "intent", "performance", "tests")


def test_a_comment_naming_a_lens_asks_for_that_lens() -> None:
    assert aspect_from("/review security", known=KNOWN) == "security"


def test_the_words_after_the_lens_are_not_part_of_it() -> None:
    """A person writes to a person as well as to a tool. `/review security please` is one lens and
    a courtesy, not a lens named "security please"."""
    assert aspect_from("/review security please, this one worries me", known=KNOWN) == "security"


def test_only_the_first_line_decides() -> None:
    """A comment that asks for a review and then explains why must not be read past the ask, or
    prose below the request can change the request."""
    assert aspect_from("/review tests\n\nBecause security here is fine already.", known=KNOWN) == "tests"


def test_a_bare_review_is_refused_and_says_what_exists() -> None:
    """Grounded to a named aspect rather than defaulting. A bare `/review` could mean "every lens",
    which is four paid calls nobody asked for by name, or "the usual one", which is a default this
    framework does not get to choose on somebody's behalf. Refusing lists the options, so the
    refusal is also the documentation."""
    with pytest.raises(AspectRefused) as refused:
        aspect_from("/review", known=KNOWN)
    assert "security" in str(refused.value)
    assert "intent" in str(refused.value)


def test_a_lens_nobody_declared_is_refused_by_name() -> None:
    with pytest.raises(AspectRefused, match="sekurity"):
        aspect_from("/review sekurity", known=KNOWN)


def test_the_known_set_is_the_repositorys_own_not_the_shipped_one() -> None:
    """A repository that replaced its lens map gets exactly its map. The shipped four are examples,
    not the set — which is the whole point of `AiReview(lenses=...)`."""
    assert aspect_from("/review a11y", known=("a11y",)) == "a11y"
    with pytest.raises(AspectRefused):
        aspect_from("/review security", known=("a11y",))


def test_an_aspect_that_would_escape_the_comment_marker_is_refused() -> None:
    """`marker()` builds `<!-- in-lockstep:review:{aspect} -->`, so an aspect carrying `-->` would
    close the comment early and render the rest as visible content in the pull request. Nothing
    special-cases that string: it is refused because it is not a lens, which is the refusal that
    keeps working when somebody invents a new way to be clever."""
    with pytest.raises(AspectRefused):
        aspect_from("/review --> <script>", known=KNOWN)


def test_a_path_is_not_a_lens() -> None:
    with pytest.raises(AspectRefused):
        aspect_from("/review ../../etc/passwd", known=KNOWN)


def test_the_lens_name_is_matched_without_regard_to_case() -> None:
    """Forgiving where forgiving is free: the set is closed, so case-folding cannot admit anything
    that was not already there."""
    assert aspect_from("/review SECURITY", known=KNOWN) == "security"


def test_a_comment_that_is_not_a_review_request_is_refused_rather_than_guessed() -> None:
    """The trampoline matches the prefix, so this is defence against a trigger that fires wrongly,
    not against a person. Guessing an aspect out of arbitrary prose is how a tool spends money on a
    comment nobody meant as a command."""
    with pytest.raises(AspectRefused):
        aspect_from("we should review security here", known=KNOWN)


def test_no_known_lenses_at_all_refuses_rather_than_accepting_anything() -> None:
    """An adapter bound with an empty lens map. Accepting a name against an empty set would be the
    one case where the closed set stopped being closed."""
    with pytest.raises(AspectRefused):
        aspect_from("/review security", known=())


# -- the refs a comment event does not carry ------------------------------------------------


def test_a_change_requests_head_is_a_commit_not_a_branch_name() -> None:
    """A branch name resolves to whatever it points at when the job runs, so a review keyed on one
    can review something nobody asked about. The base stays a name because that is what it is."""
    import asyncio

    from in_lockstep.platform.scm import GitHubScm

    scm = GitHubScm(".")
    asked: list[tuple[str, ...]] = []

    def _json(*args: str) -> object:
        asked.append(args)
        return {"baseRefName": "main", "headRefOid": "9f1c2ab"}

    scm._gh_json = _json  # type: ignore[method-assign]
    assert asyncio.run(scm.change_refs(199)) == ("main", "9f1c2ab")
    assert "headRefOid" in asked[0][-1], asked


def test_a_number_that_is_not_a_change_request_reports_nothing_rather_than_guessing() -> None:
    """`gh pr view` on an issue number fails, and that failure is the answer. Guessing the default
    branch here would diff it against itself and report a clean bill of health for a change the run
    never read — an all-clear, which is worse than an error."""
    import asyncio

    from in_lockstep.platform.scm import GitHubScm

    scm = GitHubScm(".")

    def _boom(*_a: str) -> object:
        raise RuntimeError("no pull requests found for branch")

    scm._gh_json = _boom  # type: ignore[method-assign]
    assert asyncio.run(scm.change_refs(199)) is None


def test_a_change_request_missing_either_ref_is_not_half_an_answer() -> None:
    import asyncio

    from in_lockstep.platform.scm import GitHubScm

    scm = GitHubScm(".")
    scm._gh_json = lambda *a: {"baseRefName": "main", "headRefOid": ""}  # type: ignore[method-assign]
    assert asyncio.run(scm.change_refs(199)) is None


# -- the live path: a comment reaching the command ------------------------------------------


def test_a_comment_naming_no_lens_costs_nothing_and_writes_no_record(tmp_path, monkeypatch) -> None:
    """GATE-REVIEW-3. The ordering is the whole fix.

    The adapter's own unknown-aspect refusal arrives after `_run_id`, so an unrecognised aspect
    reaching the run earns a ledger record, and `blocked` sits inside `failure_rate`'s
    denominator. Resolved before `Auth()`, the registry and the bind, a typo costs nothing:
    no credential is read, and the store is untouched."""
    from click.testing import CliRunner

    from in_lockstep.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ledger = tmp_path / ".lockstep" / "ledger"
    ledger.mkdir(parents=True)

    result = CliRunner().invoke(main, ["review", "--ask", "/review sekurity", "--diff", "x"])
    assert result.exit_code != 0
    assert "sekurity" in result.output, result.output
    assert "security" in result.output, "the refusal has to say what does exist"
    assert list(ledger.glob("*.json")) == [], "a typo must not append a ledger record"


def test_a_comment_naming_a_shipped_lens_selects_it(tmp_path, monkeypatch) -> None:
    """The positive control. Without it every assertion above is satisfied by a command that
    refuses everything."""
    from click.testing import CliRunner

    from in_lockstep.cli import main

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["review", "--ask", "/review intent", "--offline", "--diff", "x"])
    # It gets past resolution — whatever the replay then does, the lens was accepted.
    assert "no lens named" not in result.output, result.output
    assert "needs a lens" not in result.output, result.output


def test_an_explicit_base_and_head_still_win_over_the_change_request(tmp_path, monkeypatch) -> None:
    """`--pr` already meant "the change request this review is about" — it is what `--comment` posts
    to — so reusing it must not change what a command that passes refs explicitly does. The host is
    never asked in that case, which is the assertion: a stub that would explode proves it."""
    from click.testing import CliRunner

    import in_lockstep.cli as cli
    from in_lockstep.cli import main

    monkeypatch.chdir(tmp_path)

    def _boom(_lockstep):
        raise AssertionError("the host was asked for refs that were given explicitly")

    monkeypatch.setattr(cli, "_bound_scm", _boom)
    result = CliRunner().invoke(
        main,
        ["review", "--pr", "199", "--base", "abc", "--head", "def", "--offline", "--diff", "x"],
    )
    assert "could not say what change request" not in result.output, result.output
