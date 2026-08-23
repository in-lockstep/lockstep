"""Reading a chat-ops command out of a comment.

A comment is untrusted input from anyone who can type in a text box, so what counts as an invocation
is worth pinning down precisely — especially the cases that must *not* count.
"""

from __future__ import annotations

from pipeline_exec.command import parse

NAMES = ["issue", "branch"]


def test_a_bare_command_matches():
    assert parse("/implement", "/implement").matched is True


def test_a_different_command_does_not_match():
    assert parse("/deploy APP-1", "/implement").matched is False


def test_the_leading_slash_is_optional_in_the_declaration():
    assert parse("/implement", "implement").matched is True


# --- what must not count as an invocation ----------------------------------


def test_a_command_mentioned_mid_sentence_does_not_fire():
    """Otherwise discussing the command would invoke it."""
    assert parse("you should run /implement here", "/implement").matched is False


def test_a_quoted_command_does_not_fire():
    """Quoting somebody else's comment must not re-run their command."""
    assert parse("> /implement APP-412\n\nI disagree.", "/implement").matched is False


def test_a_command_that_only_shares_a_prefix_does_not_fire():
    assert parse("/implementation-notes", "/implement").matched is False


def test_an_empty_body_does_not_fire():
    assert parse("", "/implement").matched is False


# --- reading the arguments -------------------------------------------------


def test_positionals_fill_the_declared_names_in_order():
    invocation = parse("/implement APP-412 feature/x", "/implement", names=NAMES)
    assert invocation.arguments == {"issue": "APP-412", "branch": "feature/x"}


def test_named_arguments_work_with_or_without_dashes():
    dashed = parse("/implement --issue=APP-1 --branch=b", "/implement", names=NAMES)
    plain = parse("/implement issue=APP-1 branch=b", "/implement", names=NAMES)
    assert dashed.arguments == plain.arguments == {"issue": "APP-1", "branch": "b"}


def test_named_arguments_beat_positionals():
    invocation = parse("/implement --issue=APP-9 APP-1", "/implement", names=NAMES)
    assert invocation.arguments["issue"] == "APP-9"


def test_dashes_in_names_become_underscores():
    """So an argument reads naturally in a comment and still names a workflow input."""
    invocation = parse("/implement --target-branch=main", "/implement")
    assert invocation.arguments == {"target_branch": "main"}


def test_quoted_values_survive():
    invocation = parse('/implement --note="two words"', "/implement")
    assert invocation.arguments["note"] == "two words"


def test_an_unbalanced_quote_does_not_crash():
    """A human typing, not an attack — degrade rather than fail the run."""
    assert parse('/implement --note="unclosed', "/implement").matched is True


# --- the human's actual request --------------------------------------------


def test_prose_after_the_command_is_captured_as_instruction():
    body = "/implement APP-412\n\nUse the existing retry helper rather than a new one."
    invocation = parse(body, "/implement", names=NAMES)
    assert invocation.arguments["issue"] == "APP-412"
    assert "existing retry helper" in invocation.instruction


def test_an_invocation_with_no_prose_has_no_instruction():
    assert parse("/implement APP-1", "/implement", names=NAMES).instruction == ""


def test_the_first_invocation_wins():
    body = "/implement APP-1\n/implement APP-2"
    assert parse(body, "/implement", names=NAMES).arguments["issue"] == "APP-1"


def test_a_command_after_prose_still_fires_if_it_opens_a_line():
    body = "Thanks for the review.\n\n/implement APP-7"
    assert parse(body, "/implement", names=NAMES).arguments["issue"] == "APP-7"
