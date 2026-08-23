"""What the comment asked for becomes the work list, so what counts as a valid request matters."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "select_aspects", Path(__file__).parent.parent / "scripts" / "select-aspects.py"
)
assert spec and spec.loader
select_aspects = importlib.util.module_from_spec(spec)
spec.loader.exec_module(select_aspects)

ASPECTS_DIR = Path(__file__).parent.parent / "aspects"
ASPECTS = select_aspects.load_aspects(ASPECTS_DIR)


# --- reading the definitions ------------------------------------------------


def test_every_aspect_file_becomes_an_aspect():
    assert set(ASPECTS) == {"security", "intent", "tests", "performance"}


def test_an_aspect_carries_the_brief_the_agent_will_follow():
    """The agent has no tools; the whole lens has to travel in the item."""
    security = ASPECTS["security"]
    assert security["title"] == "Security"
    assert security["summary"]
    assert "an attacker would do" in security["brief"]


def test_adding_an_aspect_is_adding_a_file(tmp_path):
    (tmp_path / "clarity.md").write_text(
        "---\nname: clarity\ntitle: Clarity\nsummary: Whether it reads well\n---\n\nLook for…\n",
        encoding="utf-8",
    )
    loaded = select_aspects.load_aspects(tmp_path)
    assert loaded["clarity"]["title"] == "Clarity"
    assert loaded["clarity"]["brief"] == "Look for…"


# --- resolving a request ----------------------------------------------------


def test_one_aspect_produces_one_item():
    assert [a["key"] for a in select_aspects.select(["security"], ASPECTS)] == ["security"]


def test_several_aspects_produce_several_items():
    """`/review security intent` is two reviews, not one review mentioning both."""
    selected = select_aspects.select(["security", "intent"], ASPECTS)
    assert [a["key"] for a in selected] == ["security", "intent"]


def test_the_order_asked_for_is_preserved():
    assert [a["key"] for a in select_aspects.select(["intent", "security"], ASPECTS)] == [
        "intent",
        "security",
    ]


def test_a_repeated_aspect_is_reviewed_once():
    assert len(select_aspects.select(["security", "security"], ASPECTS)) == 1


def test_asking_for_nothing_reviews_everything():
    """`/review` with no arguments is a reasonable thing to type."""
    assert len(select_aspects.select([], ASPECTS)) == len(ASPECTS)


def test_an_unknown_aspect_is_refused_and_says_what_is_available():
    """A model asked for a "banana review" will produce one, and it will look plausible."""
    with pytest.raises(KeyError) as excinfo:
        select_aspects.select(["banana"], ASPECTS)
    message = str(excinfo.value)
    assert "banana" in message
    assert "security" in message


def test_one_unknown_aspect_refuses_the_whole_request():
    """Silently reviewing two of three would look like success."""
    with pytest.raises(KeyError):
        select_aspects.select(["security", "banana"], ASPECTS)


# --- reading what the comment said ------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["security", "intent"]', ["security", "intent"]),
        ("security intent", ["security", "intent"]),
        ("security, intent", ["security", "intent"]),
        ("  security   ", ["security"]),
        ("SECURITY", ["security"]),
        ("", []),
        ("[]", []),
        ("[not json", ["[not", "json"]),
    ],
)
def test_a_request_is_read_however_it_was_written(raw, expected):
    assert select_aspects.parse_request(raw) == expected


def test_items_carry_the_key_the_matrix_fans_out_on():
    assert all("key" in aspect for aspect in select_aspects.select([], ASPECTS))


def test_the_written_work_list_is_valid_json(tmp_path):
    output = tmp_path / "aspects.json"
    selected = select_aspects.select(["tests"], ASPECTS)
    output.write_text(json.dumps(selected, indent=2), encoding="utf-8")
    assert json.loads(output.read_text())[0]["key"] == "tests"
