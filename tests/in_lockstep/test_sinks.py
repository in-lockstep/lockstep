"""GATE-REDACT-1 — every writer that leaves the process goes through redaction.

The plan called this the essential gate and it was never written, which is how
`docs/controls-crosswalk.md` came to claim redaction was "default-deny over every writer" while
the sinks were in fact enumerated by hand. An enumerated list had already missed five of them —
stdout and stderr, OTel span attributes, checkpoint files, the trampoline artifact, and
notification bodies — so a list is exactly what must not be trusted here.

This walks the AST of every shipped module and fails on a raw write. The rule is inverted from
the obvious one: rather than listing sinks and checking each redacts, it lists the *primitives*
that reach outside a process and requires every use to be inside `privileged/sink.py` or named
below with a reason. Adding an unwrapped writer fails the build. Adding one deliberately is a diff
a reviewer sees, which is the property an enumerated sink list never had.

`print` and `click.echo` are deliberately absent from the primitives. Standard output is covered
by wrapping the stream at CLI entry, so those calls are safe by construction and policing sixty of
them would be noise that trains people to add exemptions.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "in_lockstep"

# The module that IS the wrapper. Everything here is expected to write rawly; that is its job.
SINK_MODULE = "privileged/sink.py"

# Calls that put bytes somewhere another process can read them.
RAW_WRITE_METHODS = {"write", "write_text", "write_bytes", "writelines"}
RAW_WRITE_FUNCTIONS = {"open", "fdopen"}

# Exemptions, each with the reason it is not a redaction sink. A bare path would be an enumerated
# list again; the reason is what makes an addition reviewable rather than habitual.
#
# Keyed on the enclosing FUNCTION, not just the module. Keying on `(module, method)` meant one
# exemption licensed every call site in that file: `init` writing two module-level scaffold
# constants excused a later `write_text` in the same module that serialized model-authored
# content, and nothing here would have said so. An exemption is a decision about one call site,
# and the key should be able to express that.
EXEMPT: dict[tuple[str, str, str], str] = {
    ("cli.py", "init_cmd", "write_text"): (
        "`init` writes the lifecycle scaffold, a module-level string constant. There is no run, "
        "no credential, and nothing in scope that could carry one."
    ),
    ("cli.py", "_write_trampoline", "write_text"): (
        "Writes a CI trampoline: a module-level string template with the framework version "
        "substituted in. No run, no credential, nothing in scope that could carry one."
    ),
    ("cli.py", "_scaffold_implement", "write_text"): (
        "`init --implement` writes the merged lifecycle module — a module-level string constant "
        "appended to the existing scaffold. Same reasoning: no run, no credential, nothing in "
        "scope that could carry one."
    ),
    ("platform/artifacts.py", "write_changeset", "write_text"): (
        "The ChangeSet artifact. Its `contents` are the change itself and must survive verbatim: "
        "masking a source file that happens to match a credential shape would corrupt the file "
        "the framework was asked to write, and would protect nothing — `apply` writes those same "
        "bytes to disk at the other end. The metadata around them IS model prose, so the function "
        "redacts summary and ticket explicitly before serializing. Same reasoning as scm/base.py."
    ),
    ("cli.py", "_write_changeset", "write_text"): (
        "`apply-inline` writing a ChangeSet to the working tree — the local half of what "
        "scm/base.py `apply` does in CI, for the identical reason: the content is the change, and "
        "masking it would corrupt the file the framework was asked to write.\n\n"
        "This call site was already here and already unredacted. It went unnoticed because the "
        "old `(module, method)` key let `init`'s scaffold exemption license every `write_text` in "
        "cli.py, which is what the function-level key was introduced to stop."
    ),
    ("loader.py", "load", "write"): (
        "Materialises lockstep.py from the TRUSTED ref to a temp file so it can be imported. "
        "Redacting it would corrupt the configuration being loaded — this is an input, not a sink."
    ),
    ("platform/scm/base.py", "apply", "write_text"): (
        "Applies a ChangeSet to the working tree. The content is the change itself; masking it "
        "would silently corrupt the file the framework was asked to write. ChangeGuard is the "
        "control on this path, not Redact."
    ),
}


def _shipped_modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


MODULES = _shipped_modules()


def _write_name(call: ast.Call) -> str | None:
    """What raw writer this call is, or None."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in RAW_WRITE_METHODS:
        return func.attr
    if isinstance(func, ast.Attribute) and func.attr in RAW_WRITE_FUNCTIONS and _writes(call):
        return func.attr
    if isinstance(func, ast.Name) and func.id in RAW_WRITE_FUNCTIONS and _writes(call):
        return func.id
    return None


def _raw_writes(tree: ast.AST) -> list[tuple[str, str, int]]:
    """Every raw write, as (enclosing function, writer, line).

    The scope is carried down rather than looked up afterwards, because `ast` nodes do not know
    their parents and a second pass to reconstruct them is how this kind of scan grows a bug.
    """
    found: list[tuple[str, str, int]] = []

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and (name := _write_name(child)):
                found.append((scope, name, child.lineno))
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            walk(child, inner)

    walk(tree, "<module>")
    return found


def _writes(call: ast.Call) -> bool:
    """An `open` in a read mode is not a sink. Absent a literal mode, assume it is."""
    modes = [a for a in call.args[1:2] if isinstance(a, ast.Constant)]
    modes += [k.value for k in call.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
    if not modes:
        return len(call.args) > 1 or any(k.arg == "mode" for k in call.keywords)
    return any(isinstance(m.value, str) and any(c in m.value for c in "wax+") for m in modes)


def test_the_scan_finds_the_wrapper_itself() -> None:
    """A scanner matching nothing would make every assertion below vacuous."""
    wrapper = SRC / SINK_MODULE
    assert _raw_writes(ast.parse(wrapper.read_text())), "the primitives no longer match anything"


@pytest.mark.parametrize("path", MODULES, ids=[str(p.relative_to(SRC)) for p in MODULES])
def test_no_module_writes_outside_the_process_unwrapped(path: Path) -> None:
    relative = str(path.relative_to(SRC))
    if relative == SINK_MODULE:
        return

    for scope, name, line in _raw_writes(ast.parse(path.read_text())):
        assert (relative, scope, name) in EXEMPT, (
            f"{relative}:{line} calls {name}() directly inside {scope}(). Anything leaving this "
            f"process must go through in_lockstep.privileged.sink, which redacts — or be added to "
            f"EXEMPT in {Path(__file__).name} with the reason it is not a redaction sink."
        )


def test_every_exemption_still_applies() -> None:
    """An exemption whose call site is gone is a licence nobody revoked."""
    live = {
        (str(p.relative_to(SRC)), scope, name)
        for p in MODULES
        for scope, name, _ in _raw_writes(ast.parse(p.read_text()))
        if str(p.relative_to(SRC)) != SINK_MODULE
    }
    stale = set(EXEMPT) - live
    assert not stale, f"remove these exemptions; their call sites are gone: {sorted(stale)}"


def test_every_exemption_gives_a_reason() -> None:
    for key, reason in EXEMPT.items():
        assert len(reason) > 60, f"{key} needs a reason, not a label"


# -- and that the wrapper actually masks, not merely that everything routes through it --------


@pytest.fixture
def secret() -> str:
    from in_lockstep.privileged.redact import redact_registry

    value = "sk-ant-api03-NOTAREALKEYbutlongenough"
    redact_registry.add(value)
    yield value
    redact_registry.clear()


def test_stdout_is_masked_without_the_caller_knowing(secret: str, capsys) -> None:
    """The whole point of wrapping the stream: `print` did not have to be taught anything."""
    from in_lockstep.privileged.sink import redacted_streams

    with redacted_streams():
        print(f"provider said: {secret}")
    out = capsys.readouterr().out
    assert secret not in out
    assert "***" in out


def test_click_echo_is_masked_too(secret: str, capsys) -> None:
    import click

    from in_lockstep.privileged.sink import redacted_streams

    with redacted_streams():
        click.echo(f"failed with {secret}")
    assert secret not in capsys.readouterr().out


def test_a_written_file_is_masked(secret: str, tmp_path: Path) -> None:
    from in_lockstep.privileged import sink

    target = tmp_path / "nested" / "record.json"
    sink.write_json(target, {"error": f"401 from provider: {secret}"})
    assert secret not in target.read_text()
    assert target.exists(), "parent directories are created, like the writers it replaced"


def test_an_atomic_write_is_masked_and_leaves_no_partial(secret: str, tmp_path: Path) -> None:
    from in_lockstep.privileged import sink

    target = tmp_path / "step.json"
    sink.write_text_atomic(target, f"checkpoint carrying {secret}")
    assert secret not in target.read_text()
    assert list(tmp_path.glob("*.partial")) == []


def test_span_attributes_are_masked(secret: str) -> None:
    """A sink that never touches a file, and the one an enumerated list missed twice."""
    from in_lockstep.privileged import sink

    masked = sink.attributes({"error": f"got {secret}", "nested": {"body": secret}})
    assert secret not in str(masked)


def test_the_ledger_a_run_writes_carries_no_secret(secret: str, tmp_path: Path) -> None:
    """End to end through the store, not through the helper it happens to call."""
    import asyncio

    from in_lockstep.platform.ledger.store import InRepoLedger

    ledger = InRepoLedger(root=tmp_path)
    asyncio.run(ledger.append("run-1", {"error": f"provider rejected {secret}"}))
    assert secret not in ledger.path_for("run-1").read_text()


def test_a_cassette_recorded_from_a_live_call_carries_no_secret(secret: str, tmp_path: Path) -> None:
    from in_lockstep.ai.replay import Cassette

    tape = Cassette(path=tmp_path / "tape.json")
    tape.provider_calls["k"] = {"content": f"Authorization: Bearer {secret}"}
    tape.save()
    assert secret not in (tmp_path / "tape.json").read_text()


def test_an_unseeded_credential_shape_is_still_masked(tmp_path: Path) -> None:
    """Structural patterns are the backstop for a secret nothing registered."""
    from in_lockstep.privileged import sink

    target = tmp_path / "r.json"
    sink.write_json(target, {"h": "Authorization: Bearer ghp_aaaaaaaaaaaaaaaaaaaaaaaa"})
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaa" not in target.read_text()
