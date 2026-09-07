"""The documentation's code runs, or at least parses.

The first code block a visitor pastes is where adoption is decided, and it spent months raising
NameError — the snippet bound `Test` and `Validate` without importing them, three separate
readers hit it inside their first ten minutes, and nothing in CI could notice because nothing
executed what the docs showed. `test_example_wayfinder.py` set the precedent of running what we
ship; this applies it to what we say.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _python_blocks(path: Path) -> list[str]:
    return re.findall(r"```python\n(.*?)```", path.read_text(), re.DOTALL)


def test_the_readme_front_door_actually_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_the_cookbook_snippets_execute_not_merely_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cookbook promises its `lockstep.py` snippets are executed by the suite. This is that.

    One shared namespace, in order: recipe 1 defines `lockstep` and later recipes bind into it,
    the way a reader pasting them into one file would. The chdir keeps `Lockstep.detect()` off
    this repository."""
    monkeypatch.chdir(tmp_path)
    # Recipe 9's lens points at a body file, because a prompt body is a file. A reader following
    # the cookbook writes it; so does this test, which is the difference between asserting the
    # snippet parses and asserting the thing it builds can be rendered.
    body = tmp_path / "prompts" / "license.md"
    body.parent.mkdir(parents=True)
    body.write_text("Review this diff ONLY for license and copyright problems.\n")

    blocks = _python_blocks(ROOT / "docs" / "cookbook.md")
    assert len(blocks) >= 5, "the cookbook lost its snippets"
    namespace: dict[str, Any] = {}
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


def _defined_prompts(namespace: dict[str, Any]) -> list[tuple[str, type]]:
    """Every `Prompt` subclass a snippet defined, and none it merely imported."""
    from in_lockstep.ai.prompt import Prompt

    return [
        (name, value)
        for name, value in namespace.items()
        if isinstance(value, type)
        and issubclass(value, Prompt)
        and not value.__module__.startswith("in_lockstep")
    ]


def test_gate_docs_1_every_prompt_extending_md_defines_can_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The render pass, over the page that teaches the first thing an adopter tries. The page
    showed `Body.from_file("prompts/our-review.md")` for months, which resolved inside the
    framework's own package and failed on first run; the type gate passed it because the shape
    was fine (#311). Each block that defines a prompt is executed in a scratch repository holding
    the body file the block names, and the prompt it defines is composed."""
    import re

    from in_lockstep.prompts.review import review_layers

    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "our-review.md").write_text("Review this diff for what the team cares about.\n")
    rendered = 0
    for index, block in enumerate(_python_blocks(ROOT / "docs" / "extending.md")):
        if not re.search(r"^class \w+\((\w+Prompt|\w+Lens)\):", block, re.M):
            continue
        if "in_lockstep.packs" in block:
            # A body a pack ships needs the pack installed; that block is covered by the pack
            # tests over a built fixture, and this pass says so rather than installing one.
            continue
        namespace: dict[str, Any] = {}
        exec(compile(block, f"extending.md[{index}]", "exec"), namespace)
        for name, cls in _defined_prompts(namespace):
            assert cls().system(review_layers()).strip(), f"extending.md prompt {name} composed to nothing"
            rendered += 1
    assert rendered >= 2, "extending.md lost its house-prompt snippets"


def test_the_extending_md_strategy_example_is_accepted_by_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`use` refuses a strategy that names no `request`; the example must not be one."""
    import re

    from in_lockstep import Lockstep

    monkeypatch.chdir(tmp_path)
    blocks = [
        b
        for b in _python_blocks(ROOT / "docs" / "extending.md")
        if re.search(r"^class \w+\(\w+Strategy\):", b, re.M)
    ]
    assert blocks, "extending.md lost its strategy example"
    namespace: dict[str, Any] = {"lockstep": Lockstep.detect()}
    exec(compile(blocks[0], "extending.md[strategy]", "exec"), namespace)
    strategy = next(
        v for v in namespace.values() if isinstance(v, type) and getattr(v, "id", "") == "implement/careful"
    )
    bound = namespace["lockstep"].use(strategy)
    assert type(bound) is strategy


# -- GATE-DOCS-1: what the docs show type-checks under what an adopter runs ---------------------
#
# `docs/extending.md` taught a workflow signature `mypy --strict` rejects, on the page that exists
# to make extending discoverable, a week after #232 shipped `py.typed` so adopters could run that
# very checker. Parsing and executing catch a name that does not exist; only the checker catches a
# shape the framework rejects, and an adopter meets it on the first snippet they copy.

DOCS_TYPED = (
    ROOT / "README.md",
    ROOT / "docs" / "getting-started.md",
    ROOT / "docs" / "extending.md",
    ROOT / "docs" / "trampoline.md",
    ROOT / "docs" / "cookbook.md",
)

#: What a page's snippets may assume without importing it, and nothing else: the lifecycle the
#: module built, and the run context a workflow is handed. Stated on the page that elides them.
#: Deliberately NOT the framework's public names -- a prelude that imported `Verb` would let a
#: snippet omit the import a reader needs, which is the README front-door defect one gate over.
_PRELUDE = """from __future__ import annotations

from in_lockstep import Lockstep
from in_lockstep.core.context import RunContext

lockstep = Lockstep()
ctx: RunContext


async def _page() -> None:
"""


#: The checker exactly as an adopter runs it.
_MYPY = (sys.executable, "-m", "mypy", "--strict", "--no-error-summary")


def _page_module(doc: Path) -> str:
    """One module per page: its blocks in order, as a reader pasting them into one file would
    have them, indented under one async function so the docs' top-level `await` shorthand is
    legal and a name a later block relies on from an earlier one resolves."""
    body: list[str] = []
    for index, block in enumerate(_python_blocks(doc)):
        body.append(f"    # -- block {index} --")
        for line in block.rstrip("\n").splitlines():
            if line.startswith("from __future__"):
                continue
            body.append(("    " + line) if line.strip() else "")
    return _PRELUDE + "\n".join(body or ["    pass"]) + "\n"


def test_gate_docs_1_every_documented_snippet_type_checks_under_strict_mypy(tmp_path: Path) -> None:
    """GATE-DOCS-1. The same `mypy --strict` an adopter runs, over every page, with exactly the
    two names the page says it assumes. Written to a temporary directory rather than the tree,
    so `test_checked_tree.py`'s ratchet over checked paths is not asked to cover generated files.
    """
    import subprocess

    modules = []
    for doc in DOCS_TYPED:
        module = tmp_path / (doc.stem.replace("-", "_") + ".py")
        module.write_text(_page_module(doc))
        modules.append(module.name)
    result = subprocess.run(
        [
            *_MYPY,
            "--cache-dir",
            str(tmp_path / ".mypy"),
            *modules,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        "a documented snippet does not type-check under `mypy --strict`, which is the checker "
        f"`py.typed` was shipped for adopters to run:\n{result.stdout}{result.stderr}"
    )


def test_the_typecheck_gate_would_notice_a_bare_outcome(tmp_path: Path) -> None:
    """The positive control, and the exact shape #252 filed: a bare `Outcome` on a documented
    workflow. The gate passes over the real pages, so without this a checker that had silently
    stopped finding anything would go on passing."""
    import subprocess

    bad = tmp_path / "bad.py"
    bad.write_text(
        _PRELUDE
        + "    from in_lockstep import Outcome, TicketSource, workflow\n"
        + "    from in_lockstep.adapters.ai import Implement\n\n"
        + '    @workflow(id="implement/from-label")\n'
        + "    async def implement_from_label(ctx: RunContext, label: str, tickets: TicketSource)"
        + " -> Outcome:\n"
        + '        ready = await tickets.search(f"label:{label}", limit=1)\n'
        + "        return await ctx.do(Implement(ticket=ready[0]))\n"
    )
    result = subprocess.run(
        [
            *_MYPY,
            "--cache-dir",
            str(tmp_path / ".mypy"),
            "bad.py",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode != 0
    assert "type-arg" in result.stdout, result.stdout


def test_the_typecheck_gate_would_notice_a_missing_import(tmp_path: Path) -> None:
    """The other positive control, and the one that keeps the prelude honest: a snippet using a
    framework name it never imported must fail, so the prelude can never quietly grow the import
    a reader needs. The README front door once raised NameError for months on exactly this."""
    import subprocess

    bad = tmp_path / "bad.py"
    bad.write_text(_PRELUDE + '    lockstep.models.route(Verb.TRIAGE, "local:qwen3-8b")\n')
    result = subprocess.run(
        [*_MYPY, "--cache-dir", str(tmp_path / ".mypy"), "bad.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode != 0
    assert 'Name "Verb" is not defined' in result.stdout, result.stdout


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
        # Three halves, because the claim has three: the vocabulary a repository declares in,
        # the arithmetic over the ledger, and the command that prints it. Any one alone would
        # be a row that survives the other two being deleted.
        # Four halves now: the vocabulary, the census, the command, and the process that drafts,
        # measures and stages -- which is the half the row claims since #163.
        "Improvement loop": (
            _importable("in_lockstep.core.improve", "Improvable")
            and _importable("in_lockstep.metrics", "recurring")
            and _importable("in_lockstep.workflows.improve", "improve_measure")
            and "improve" in commands
        ),
        # The split and the spread, plus the command: a row that survived `keyed_by_actor` being
        # deleted would be claiming a comparison nothing computes.
        "Consistency across askers": (
            _importable("in_lockstep.metrics", "keyed_by_actor")
            and _importable("in_lockstep.metrics", "Spread")
            and "report" in commands
        ),
        "Backport": "backport" in SHIPPED_VERBS and "backport" in commands,
        "RFE": "rfe" in SHIPPED_VERBS and "rfe" in commands,
        "Triage": "triage" in SHIPPED_VERBS and "triage" in commands,
        # All four deterministic verbs from what the tree declares, so the claim is pinned to the
        # two adapters issue 162 added as well as to the function that binds them.
        "Detected lifecycle": (
            _importable("in_lockstep.adapters", "detected_bindings")
            and _importable("in_lockstep.adapters", "CommandBuild")
            and _importable("in_lockstep.adapters", "CommandRun")
        ),
        # Both halves: the verb and its adapter, and the command the scaffolded work jobs run
        # before anything else.
        "Provisioned environment": (
            "provision" in SHIPPED_VERBS
            and "provision" in commands
            and _importable("in_lockstep.adapters", "CommandProvision")
        ),
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
        # Both halves again: the harvester, and the recorder keeping the request it needs. A
        # harvester over cassettes that store only a hash would be a claim that raises on every
        # recording a repository actually has.
        "Harvesting history into cases": (
            _importable("in_lockstep.evaluation.harvest", "harvest")
            and "_request_record" in _source("in_lockstep.ai.replay")
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


def _source(module: str) -> str:
    """A module's source, for a claim whose proof is a behaviour rather than a name."""
    import importlib
    import inspect

    return inspect.getsource(importlib.import_module(module))


def _importable(module: str, attr: str) -> bool:
    import importlib

    try:
        return hasattr(importlib.import_module(module), attr)
    except ImportError:
        return False


def test_the_quickstart_outputs_match_the_tool_that_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """getting-started shows literal command output. The stable lines of those captures are
    asserted against the real commands, so the page cannot describe a previous version.

    Subset matching, deliberately: hashes, paths and timings differ per machine; section
    headers, command vocabulary and fixed sentences do not."""
    import subprocess

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
    for stable in ("DOC101", "DOC120", "DOC130"):
        assert stable in doctor_out and stable in doc, f"doctor code drifted: {stable!r}"
    # DOC121 asserts a branch has no protection rule. A tree with no remote has no default branch
    # to protect and nothing that could have been read, so claiming it here was a finding about
    # the reader's terminal rather than about their repository (#249).
    assert "DOC121" not in doctor_out, "a repository with no remote cannot be found unprotected"


def test_no_documented_snippet_claims_a_workflow_id_the_framework_ships() -> None:
    """A snippet that hand-writes a shipped id raises `DuplicateWorkflow` at module load.

    `docs/extending.md` told an adopter to write `@workflow(id="implement/from-ticket")` in their
    own module. That was correct until #261 made the shipped processes framework code: the
    framework now claims that id in `implement.register()`, `@workflow` refuses a repeated id from
    a different `def`, and anyone who ran `init --implement` and then followed the page had a
    `lockstep.py` that would not import at all.

    Parsing was not enough to catch it — the snippet is valid Python, and it is valid in
    isolation. It is only wrong in the presence of the registration `init` writes, which is the
    configuration every reader of that page is in. So this asserts the relationship rather than
    the syntax.

    Deliberately not a check that the id is *bound*: an adopter's own `implement/from-label` is
    the documented, encouraged case, and a test that required every documented id to be one of
    ours would refuse the very extensibility O8 is about.
    """
    from in_lockstep.workflows import fix, implement

    shipped = set()
    for module in (implement, fix):
        source = inspect.getsource(module.register)
        shipped |= set(re.findall(r'workflow\(id="([^"]+)"\)', source))
    assert shipped, "no shipped workflow ids were found; this test would pass over nothing"

    claimed_by_docs = []
    for doc in sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]:
        for block in _python_blocks(doc):
            for wid in re.findall(r'@workflow\(id="([^"]+)"\)', block):
                if wid in shipped:
                    claimed_by_docs.append(f"{doc.name}: @workflow(id={wid!r})")

    assert not claimed_by_docs, (
        f"{claimed_by_docs} claim ids the framework registers ({sorted(shipped)}). A reader who "
        f"scaffolded with `init` and then copied this gets DuplicateWorkflow at load. Register "
        f"the shipped process (`implement.register()`) or choose an id of your own."
    )


# -- docs/needs.md: the conclusion has to agree with the table it sits under ---------------------
#
# The N2 row was updated when the loop first ran against a real model, and the section beneath it,
# "the one that matters most", went on saying nothing had executed, sixteen lines apart (#238).
# The section is the part a reader skims to, so it is the part that must not decay: whichever need
# it opens on has to be one the table still calls open.

_NEED_ROW = re.compile(r"^\| \*\*(N\d+)\*\* \| [^|]+ \| [^|]+ \| ([^|]+) \|", re.M)


def _needs() -> tuple[dict[str, str], str]:
    """The table's status cell per need, and the text of the closing section."""
    text = (ROOT / "docs" / "needs.md").read_text()
    rows = {m.group(1): m.group(2).strip().strip("*").lower() for m in _NEED_ROW.finditer(text)}
    _, _, closing = text.partition("## The one that matters most")
    return rows, closing


def test_the_need_that_matters_most_is_one_the_table_still_calls_open() -> None:
    rows, closing = _needs()
    assert len(rows) >= 14 and closing, "docs/needs.md lost its table or its closing section"
    named = re.search(r"\bN\d+\b", closing)
    assert named, "the closing section names no need"
    status = rows[named.group()]
    assert status.startswith("open"), (
        f"'The one that matters most' opens on {named.group()}, whose row says {status.split('.')[0]!r}. The "
        f"row moved and the conclusion under it did not (#238): rewrite the section against the row, "
        f"and open it on a need the table still calls open."
    )
