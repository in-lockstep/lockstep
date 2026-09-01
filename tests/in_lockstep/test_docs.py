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


def test_the_cookbook_snippets_execute_not_merely_parse(tmp_path, monkeypatch) -> None:
    """The cookbook promises its `lockstep.py` snippets are executed by the suite. This is that.

    One shared namespace, in order: recipe 1 defines `lockstep` and later recipes bind into it,
    the way a reader pasting them into one file would. The chdir keeps `Lockstep.detect()` off
    this repository."""
    monkeypatch.chdir(tmp_path)
    # Recipe 9's lens points at a body file, because a prompt body is a file. A reader following
    # the cookbook writes it; so does this test, which is the difference between asserting the
    # snippet parses and asserting the thing it builds can be rendered.
    body = tmp_path / ".lockstep" / "prompts" / "license.md"
    body.parent.mkdir(parents=True)
    body.write_text("Review this diff ONLY for license and copyright problems.\n")

    blocks = _python_blocks(ROOT / "docs" / "cookbook.md")
    assert len(blocks) >= 5, "the cookbook lost its snippets"
    namespace: dict = {}
    for index, block in enumerate(blocks):
        exec(compile(block, f"cookbook.md[{index}]", "exec"), namespace)

    # Executing a snippet proves the class DEFINES. It does not prove the thing it defined works,
    # and that gap shipped: recipe 9 set `body` to a string literal for months, which every prompt
    # accepts at definition and none can render — `body_text` calls `body.resolve()`, so the reader
    # got `AttributeError: 'str' object has no attribute 'resolve'` from inside the composer the
    # first time they ran `show-prompt`. A persona review hit it; this test did not, because it
    # stopped one step early. So it now renders every prompt the cookbook defines.
    from in_lockstep.ai.prompt import Prompt
    from in_lockstep.prompts.review import review_layers

    rendered = 0
    for name, value in namespace.items():
        # Only what the snippets DEFINED. A recipe importing `ReviewPrompt` puts the shipped
        # abstract base in this namespace too, and that one legitimately has no body.
        if (
            isinstance(value, type)
            and issubclass(value, Prompt)
            and not value.__module__.startswith("in_lockstep")
        ):
            composed = value().system(review_layers())
            assert composed.strip(), f"cookbook prompt {name} composed to nothing"
            rendered += 1
    assert rendered, "no cookbook snippet defines a prompt — has recipe 9 gone?"


# -- the README matrix, checked in both directions ------------------------------------------
#
# The matrix says what runs. gates.md already argued why a claim nobody re-checks decays: the
# safe-looking drift is a `planned` row whose feature quietly shipped, and the dangerous one is
# a `runs` row whose feature quietly broke. Both directions fail here.

_MATRIX_ROW = re.compile(r"^\| ([^|]+) \| (runs|partial|planned) \|")


def _matrix_rows() -> dict[str, str]:
    rows = {}
    for line in (ROOT / "README.md").read_text().splitlines():
        m = _MATRIX_ROW.match(line)
        if m:
            rows[m.group(1).strip()] = m.group(2)
    return rows


def test_the_matrix_exists_and_uses_only_the_three_statuses() -> None:
    rows = _matrix_rows()
    assert len(rows) >= 10, f"parsed only {len(rows)} matrix rows from the README"
    assert set(rows.values()) <= {"runs", "partial", "planned"}


def test_a_runs_row_names_something_that_ships() -> None:
    """Each `runs` claim is pinned to a symbol or command that exists right now."""
    from in_lockstep.cli import main
    from in_lockstep.core.verbs import SHIPPED_VERBS

    rows = _matrix_rows()
    commands = set(main.commands)
    # Row -> the fact that must hold for the claim to be true.
    proof = {
        "Code Review": "review" in SHIPPED_VERBS and "review" in commands,
        "Implement": "implement" in SHIPPED_VERBS and "implement" in commands,
        "Bug Fix": "fix" in SHIPPED_VERBS,
        "Backport": "backport" in SHIPPED_VERBS and "backport" in commands,
        "RFE": "rfe" in SHIPPED_VERBS and "rfe" in commands,
        "Triage": "triage" in SHIPPED_VERBS and "triage" in commands,
        "GitHub": "gate" in commands,
        "Keyless CI (federation)": _importable("in_lockstep.ai.bootstrap", "ANTHROPIC_FEDERATION_AUDIENCE"),
        "Org standards as a package": _importable("in_lockstep.core.standards", "load_standards"),
        "Spend controls": _importable("in_lockstep.core.spend", "DailySpendExceeded"),
        "Ledger + tamper-evidence": _importable("in_lockstep.platform.ledger", "GitLedger"),
        # A pack is offered by an entry point and put in force by `add` printing lines somebody
        # pastes, so the claim is pinned to both halves: the discovery module and the commands.
        "Extension packs": (
            _importable("in_lockstep.packs", "installed")
            and _importable("in_lockstep.trial", "run")
            and {"pack", "add"} <= commands
        ),
        "Pack catalog": _importable("in_lockstep.market", "read_catalog") and "search" in commands,
        # Both halves: the join that gathers the conversation, and a host adapter that can read
        # one. `with_review` alone would be a claim that degrades to "unavailable" everywhere.
        "Review conversation as context": (
            _importable("in_lockstep.platform.conversation", "with_review")
            and _importable("in_lockstep.platform.scm", "GitHubScm")
            and hasattr(__import__("in_lockstep.platform.scm", fromlist=["GitHubScm"]).GitHubScm, "remarks")
        ),
    }
    for row, status in rows.items():
        if status == "runs" and row in proof:
            assert proof[row], f"the matrix says {row!r} runs, but its proof no longer holds"
    missing = [r for r in proof if r not in rows]
    assert not missing, f"matrix rows renamed or removed: {missing}"


def test_a_planned_row_has_not_quietly_shipped() -> None:
    """The safe-looking drift: the feature lands and the matrix still says planned.

    Backport and RFE lived here as verb checks until they shipped; what remains is pinned to
    the most concrete fact available for each — best-effort by nature, since a feature can
    always land somewhere a guess did not name, but a tripwire on the likely path beats none."""
    rows = _matrix_rows()
    if rows.get("Flaky-test adapter") == "planned":
        from in_lockstep.core.verbs import SHIPPED_VERBS

        assert not _importable("in_lockstep.adapters.flaky", "FlakyTest"), (
            "a flaky-test adapter ships — flip the matrix row"
        )
        assert "flaky" not in SHIPPED_VERBS
    if rows.get("Shared ledger store") == "planned":
        from in_lockstep.platform.ledger import GitLedger, InRepoLedger

        assert InRepoLedger().scope == "local" and GitLedger().scope == "local", (
            "a SHARED-scope store ships — flip the matrix row"
        )


def _importable(module: str, attr: str) -> bool:
    import importlib

    try:
        return hasattr(importlib.import_module(module), attr)
    except ImportError:
        return False


def test_the_quickstart_outputs_match_the_tool_that_ships(tmp_path, monkeypatch) -> None:
    """getting-started shows literal command output. The stable lines of those captures are
    asserted against the real commands, so the page cannot describe a previous version.

    Subset matching, deliberately: hashes, paths and timings differ per machine; section
    headers, command vocabulary and fixed sentences do not."""
    import subprocess
    import sys

    doc = (ROOT / "docs" / "getting-started.md").read_text()
    monkeypatch.chdir(tmp_path)
    for var in [k for k in list(__import__("os").environ) if k.startswith("GITHUB_")]:
        monkeypatch.delenv(var, raising=False)

    def cli(*args: str) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "in_lockstep.cli", *args],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            timeout=120,
        )
        return result.stdout + result.stderr

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    init_out = cli("init")
    ls_out = cli("ls")
    doctor_out = cli("doctor")
    for stable in (
        "wrote .lockstep/lockstep.py",
        "wrote .github/workflows/lockstep.yml",
    ):
        assert stable in init_out and stable in doc
    for stable in (
        "bindings",
        "middleware  (privileged tier runs outside this chain and is not listed)",
        "standards  (in_lockstep.standards entry points; applied before this module's own lines)",
        "policy",
    ):
        assert stable in ls_out and stable in doc, f"ls line drifted: {stable!r}"
    for stable in ("DOC101", "DOC121", "DOC130"):
        assert stable in doctor_out and stable in doc, f"doctor code drifted: {stable!r}"
