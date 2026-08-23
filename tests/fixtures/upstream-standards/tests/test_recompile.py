"""The recompile step publishes a proposal, and commits nothing."""

from __future__ import annotations

from pathlib import Path

SCRIPT = (Path(__file__).parent.parent / "scripts" / "recompile.sh").read_text()


def test_it_refuses_to_run_without_somewhere_to_publish():
    assert "--output is required" in SCRIPT


def test_it_fetches_before_it_compiles():
    assert SCRIPT.index("lockstep fetch") < SCRIPT.index("lockstep compile")


def test_the_pins_travel_with_the_recompile():
    """A reviewer who cannot see which commit moved cannot review the change."""
    assert "pins.lock" in SCRIPT


def test_nothing_is_committed_here():
    assert "git commit" not in SCRIPT
    assert "git push" not in SCRIPT
