"""The documentation's code runs, or at least parses.

The first code block a visitor pastes is where adoption is decided, and it spent months raising
NameError — the snippet bound `Test` and `Validate` without importing them, three separate
readers hit it inside their first ten minutes, and nothing in CI could notice because nothing
executed what the docs showed. `test_example_wayfinder.py` set the precedent of running what we
ship; this applies it to what we say.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _python_blocks(path: Path) -> list[str]:
    return re.findall(r"```python\n(.*?)```", path.read_text(), re.DOTALL)


def test_the_readme_front_door_actually_runs(tmp_path, monkeypatch) -> None:
    """Executed, not linted: an import that resolves but binds the wrong thing passes a parse.

    The chdir keeps `Lockstep.detect()` hermetic — the snippet must work in a directory that is
    not this repository, because that is where every reader runs it.
    """
    monkeypatch.chdir(tmp_path)
    blocks = _python_blocks(ROOT / "README.md")
    assert blocks, "the README no longer shows a lifecycle snippet"
    for index, block in enumerate(blocks):
        exec(compile(block, f"README.md[{index}]", "exec"), {})


def test_every_documented_snippet_is_at_least_valid_python() -> None:
    """Most doc blocks elide context (`ctx`, a bound adapter) and cannot execute standalone —
    but a block that does not even parse is describing an API that does not exist.

    Top-level `await` is allowed: the docs show `await ctx.do(...)` outside a function as
    shorthand, the same way a REPL accepts it.
    """
    import ast

    for doc in (
        ROOT / "README.md",
        ROOT / "docs" / "getting-started.md",
        ROOT / "docs" / "extending.md",
        ROOT / "docs" / "trampoline.md",
    ):
        for index, block in enumerate(_python_blocks(doc)):
            compile(block, f"{doc.name}[{index}]", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
