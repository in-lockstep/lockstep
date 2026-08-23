"""Not re-reviewing an unchanged pull request, and revising rather than repeating.

These are the two behaviours that decide whether a review bot is useful or muted.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from review_ext.state import commits_since, marker_for, plan, previous_reviews, review_state

ASPECTS = [
    {"key": "security", "title": "Security", "brief": "…"},
    {"key": "intent", "title": "Intent", "brief": "…"},
]
COMMITS = [
    {"sha": "aaa11111", "commit": {"message": "Add endpoint\n\nbody"}},
    {"sha": "bbb22222", "commit": {"message": "Validate the path"}},
    {"sha": "ccc33333", "commit": {"message": "Add a test"}},
]


def review(aspect, sha, *, id=1, body_extra=""):
    return {"id": id, "body": marker_for(aspect, sha) + "\n## Review\n" + body_extra}


# --- finding what was said before -------------------------------------------


def test_the_bots_own_reviews_are_found_by_their_marker():
    found = previous_reviews([review("security", "aaa11111"), {"id": 9, "body": "a human comment"}])
    assert set(found) == {"security"}
    assert found["security"]["sha"] == "aaa11111"


def test_the_latest_review_for_an_aspect_wins():
    """A review revised several times leaves several entries; only the last describes now."""
    found = previous_reviews(
        [review("security", "aaa11111", id=1), review("security", "ccc33333", id=2)]
    )
    assert found["security"]["id"] == 2
    assert found["security"]["sha"] == "ccc33333"


def test_a_human_review_is_never_mistaken_for_the_bots():
    assert previous_reviews([{"id": 4, "body": "Looks good to me"}]) == {}


# --- deciding what still needs reviewing ------------------------------------


def test_an_unchanged_pull_request_is_not_reviewed_again():
    """A second review saying the same thing buries the human conversation."""
    pending, skipped = plan(ASPECTS, [review("security", "ccc33333")], "ccc33333", COMMITS)
    assert [item["key"] for item in pending] == ["intent"]
    assert [entry["key"] for entry in skipped] == ["security"]
    assert "has not moved" in skipped[0]["reason"]


def test_a_moved_pull_request_is_reviewed_again():
    pending, _ = plan(ASPECTS, [review("security", "aaa11111")], "ccc33333", COMMITS)
    assert [item["key"] for item in pending] == ["security", "intent"]


def test_an_aspect_never_reviewed_is_not_a_revision():
    pending, _ = plan(ASPECTS, [], "ccc33333", COMMITS)
    assert all(item["revision"] is False for item in pending)


def test_a_revision_carries_what_was_said_and_what_moved():
    """This is what a revision is actually about: what these commits changed about the conclusion."""
    pending, _ = plan(ASPECTS, [review("security", "aaa11111", id=7)], "ccc33333", COMMITS)
    security = next(item for item in pending if item["key"] == "security")
    assert security["revision"] is True
    assert security["previous_review_id"] == 7
    assert "## Review" in security["previous_review"]
    assert [c["sha"] for c in security["new_commits"]] == ["bbb22222", "ccc33333"]


def test_force_reviews_again_even_when_nothing_moved():
    pending, skipped = plan(
        ASPECTS, [review("security", "ccc33333")], "ccc33333", COMMITS, force=True
    )
    assert [item["key"] for item in pending] == ["security", "intent"]
    assert skipped == []


def test_reviewing_everything_when_nothing_has_ever_been_reviewed():
    pending, skipped = plan(ASPECTS, [], "ccc33333", COMMITS)
    assert len(pending) == 2
    assert skipped == []


# --- what moved -------------------------------------------------------------


def test_commits_since_lists_only_what_came_after():
    assert [c["sha"] for c in commits_since(COMMITS, "aaa11111")] == ["bbb22222", "ccc33333"]


def test_commits_since_the_head_is_empty():
    assert commits_since(COMMITS, "ccc33333") == []


def test_a_force_push_that_erased_the_reviewed_commit_reviews_everything():
    """Pretending nothing changed because the history was rewritten is the wrong answer."""
    assert len(commits_since(COMMITS, "deadbeef")) == len(COMMITS)


def test_only_the_first_line_of_a_commit_message_is_carried():
    assert commits_since(COMMITS, "deadbeef")[0]["message"] == "Add endpoint"


# --- the command ------------------------------------------------------------


@pytest.fixture
def fixtures(tmp_path):
    directory = tmp_path / "fixtures"
    directory.mkdir()
    (directory / "commits.json").write_text(json.dumps(COMMITS), encoding="utf-8")
    return directory


def run(aspects_file, output, fixtures, *extra):
    return CliRunner().invoke(
        review_state,
        [
            "--pr=7",
            "--repo=o/r",
            f"--aspects={aspects_file}",
            f"--output={output}",
            f"--from-dir={fixtures}",
            "--head=ccc33333",
            *extra,
        ],
    )


def test_the_command_writes_only_what_still_needs_reviewing(tmp_path, fixtures):
    (fixtures / "reviews.json").write_text(
        json.dumps([review("security", "ccc33333")]), encoding="utf-8"
    )
    aspects_file = tmp_path / "aspects.json"
    aspects_file.write_text(json.dumps(ASPECTS), encoding="utf-8")
    output = tmp_path / "pending.json"

    result = run(aspects_file, output, fixtures)
    assert result.exit_code == 0
    assert "pending=1" in result.output
    assert [item["key"] for item in json.loads(output.read_text())] == ["intent"]


def test_nothing_to_review_writes_an_empty_list(tmp_path, fixtures):
    """An empty work list means an empty matrix, and the agent never starts."""
    (fixtures / "reviews.json").write_text(
        json.dumps([review("security", "ccc33333", id=1), review("intent", "ccc33333", id=2)]),
        encoding="utf-8",
    )
    aspects_file = tmp_path / "aspects.json"
    aspects_file.write_text(json.dumps(ASPECTS), encoding="utf-8")
    output = tmp_path / "pending.json"

    result = run(aspects_file, output, fixtures)
    assert result.exit_code == 0
    assert "pending=0" in result.output
    assert json.loads(output.read_text()) == []


def test_every_pending_item_carries_the_commit_being_reviewed(tmp_path, fixtures):
    aspects_file = tmp_path / "aspects.json"
    aspects_file.write_text(json.dumps(ASPECTS), encoding="utf-8")
    output = tmp_path / "pending.json"
    run(aspects_file, output, fixtures)
    assert all(item["head_sha"] == "ccc33333" for item in json.loads(output.read_text()))
