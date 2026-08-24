"""Test environment, for the whole workspace.

Everything here exists to make the suite hermetic. `make check` on a laptop and `make ci` on a
runner must exercise the same code paths, and until the first push to GitHub they did not: the
runner injects `GITHUB_OUTPUT`, `GITHUB_STEP_SUMMARY` and six more variables that the executors
read, so thirty-eight tests asserting on a command's stdout passed locally and failed in Actions —
the commands were writing to the runner's files instead, which is exactly what they are supposed to
do there.

A test that wants Actions' environment sets it deliberately. Inheriting it from whatever host the
suite happens to run on is how a green laptop becomes a red pipeline.
"""

from __future__ import annotations

import pytest

# Prefixes rather than a list of names. The executors read eight `GITHUB_*` variables today, and the
# next one added should be covered by having been added — not by somebody remembering this file.
INJECTED_PREFIXES = ("GITHUB_", "RUNNER_", "ACTIONS_")
INJECTED_NAMES = ("CI",)


@pytest.fixture(autouse=True)
def _no_ambient_ci_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as though nothing is hosting it."""
    import os

    for name in list(os.environ):
        if name.startswith(INJECTED_PREFIXES) or name in INJECTED_NAMES:
            monkeypatch.delenv(name, raising=False)
