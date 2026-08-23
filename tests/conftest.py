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
