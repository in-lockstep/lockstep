from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def basic_root(tmp_path: Path) -> Path:
    """A writable copy of the basic fixture, so tests may compile into it."""
    root = tmp_path / "basic"
    shutil.copytree(FIXTURES / "basic", root)
    return root


@pytest.fixture
def basic_spec_dir() -> Path:
    """The read-only fixture, for tests that only compile in memory."""
    return FIXTURES / "basic"


@pytest.fixture
def repo_root() -> Path:
    """The lockstep repository itself, for tests that compile the shipped examples."""
    return Path(__file__).parent.parent


# Every example and fixture here pins its capabilities to placeholders, because
# `in-lockstep/lockstep/actions` and its executor image have never been published anywhere. That is
# a real reason not to be target-ready, and doctor says so — DOC015. These helpers assert that
# everything *else* holds, so the day the capabilities are published the assertions get stronger
# rather than needing rewriting.
UNPUBLISHED = "DOC015"


def ready_but_unpublished(report, *also_expected: str) -> None:
    codes = {finding.code for finding in report.findings}
    assert UNPUBLISHED in codes, "placeholder pins should be reported, not passed over"
    assert codes == {UNPUBLISHED, *also_expected}
    assert all(finding.code == UNPUBLISHED for finding in report.errors)
