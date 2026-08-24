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


# That day came. `actions-v0.1.0` and `exec-v0.1.0` are published, so **the examples pin real
# capabilities** and their doctor reports are expected to be clean — `target_ready` is the stronger
# assertion the weaker one was standing in for.
#
# The fixtures deliberately did *not* move. Something has to keep exercising what doctor does with a
# placeholder, and `ready_but_unpublished` is that: DOC015 is still reported, still an error, and
# still the only thing between an otherwise sound spec and a runner. A fixture that pinned real
# capabilities would leave that path untested the moment it mattered least and most.
UNPUBLISHED = "DOC015"


def ready_but_unpublished(report, *also_expected: str) -> None:
    """A spec that would run, except that its capabilities are placeholders. Fixtures only."""
    codes = {finding.code for finding in report.findings}
    assert UNPUBLISHED in codes, "placeholder pins should be reported, not passed over"
    assert codes == {UNPUBLISHED, *also_expected}
    assert all(finding.code == UNPUBLISHED for finding in report.errors)


def target_ready(report, *expected_warnings: str) -> None:
    """Nothing stands between this spec and a runner.

    Examples are held to this rather than to `ready_but_unpublished`: now that the capabilities
    exist, a placeholder pin in an example is a regression rather than the status quo, and asserting
    its *absence* is what stops one drifting back in unnoticed.
    """
    codes = {finding.code for finding in report.findings}
    assert UNPUBLISHED not in codes, "an example pinning a placeholder is a regression now"
    assert codes == set(expected_warnings)
    assert report.errors == []
