"""Entry-point standards: goal 9's distribution mechanism, finally real.

The container docstring promised "explicit binds, then plugins, then shipped defaults" while no
`entry_points` call existed under `src/`. These tests hold the promise end to end — and hold the
constraint that matters more than the feature: a plugin tightens and informs, it cannot
masquerade as the repository or weaken what the repository said.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from in_lockstep import Lockstep, Policy
from in_lockstep.core.container import Tier
from in_lockstep.core.standards import GROUP, Standards, StandardsError, load_standards

ROOT = Path(__file__).resolve().parents[2]


class FakeEntry:
    """The shape `importlib.metadata` yields: a name and a loadable."""

    def __init__(self, name: str, hook) -> None:
        self.name = name
        self._hook = hook

    def load(self):
        if isinstance(self._hook, Exception):
            raise self._hook
        return self._hook


class Iface:
    pass


def test_gate_plugin_1_a_plugin_binds_at_plugin_tier_and_the_repo_still_wins() -> None:
    lockstep = Lockstep()

    def org(std: Standards) -> None:
        std.bind(Iface, object())

    load_standards(lockstep, entries=[FakeEntry("acme", org)])
    binding = next(b for b in lockstep.container.resolved() if b.iface is Iface)
    assert binding.tier is Tier.PLUGIN

    mine = object()
    lockstep.bind(Iface, mine)  # the repository's own line, AFTER the plugin ran
    assert lockstep.container.resolve(Iface) is mine


def test_the_repo_wins_even_when_the_plugin_runs_second() -> None:
    """Tier decides, not order — `detect()` runs plugins first, but nothing depends on it."""
    lockstep = Lockstep()
    mine = object()
    lockstep.bind(Iface, mine)
    load_standards(lockstep, entries=[FakeEntry("acme", lambda std: std.bind(Iface, object()))])
    assert lockstep.container.resolve(Iface) is mine


def test_a_plugin_cannot_bind_explicit() -> None:
    """The facade offers no tier parameter; this asserts nobody adds one back."""
    import inspect

    assert "tier" not in inspect.signature(Standards.bind).parameters


def test_contributions_are_stamped_with_their_plugin_source() -> None:
    lockstep = Lockstep()
    load_standards(
        lockstep,
        entries=[FakeEntry("acme", lambda std: std.contribute(Policy(name="floor", max_turns=16)))],
    )
    layer = next(p for p in lockstep.policy.layers if p.name == "floor")
    assert layer.source == "plugin:acme"


def test_a_plugin_ceiling_merges_tighten_only_with_the_repos() -> None:
    """A plugin can lower a ceiling; neither it nor the repo can raise the other's."""
    lockstep = Lockstep()
    load_standards(
        lockstep,
        entries=[FakeEntry("acme", lambda std: std.contribute(Policy(name="floor", max_turns=16)))],
    )
    lockstep.contribute(Policy(name="repo", source="test", max_turns=40))
    assert lockstep.policy.resolve().max_turns == 16


def test_application_order_is_name_order_not_discovery_order() -> None:
    applied: list[str] = []
    entries = [
        FakeEntry("10-team", lambda std: applied.append("team")),
        FakeEntry("00-org", lambda std: applied.append("org")),
    ]
    load_standards(Lockstep(), entries=entries)
    assert applied == ["org", "team"], "sorted by name, so two machines agree who ran first"


def test_a_failing_plugin_is_loud_and_named() -> None:
    """Running without standards somebody installed is the silently-dropped control itself."""
    with pytest.raises(StandardsError, match="broken"):
        load_standards(Lockstep(), entries=[FakeEntry("broken", RuntimeError("no module"))])


def test_detect_discovers_the_entry_point_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """The integration seam: `detect()` asks importlib for exactly the documented group."""
    import importlib.metadata

    seen: dict[str, str] = {}

    def fake_entry_points(*, group: str):
        seen["group"] = group
        return [FakeEntry("acme", lambda std: std.contribute(Policy(name="floor", max_turns=16)))]

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    lockstep = Lockstep.detect()
    assert seen["group"] == GROUP == "in_lockstep.standards"
    assert lockstep.standards == ["acme"]
    assert any(p.source == "plugin:acme" for p in lockstep.policy.layers)


def test_a_bare_constructor_loads_no_plugins() -> None:
    """A hand-built instance in a test must not inherit whatever this machine has installed."""
    assert Lockstep().standards == []


def test_the_worked_example_actually_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    """`examples/acme-standards` is the shipped org layer; it must work, not merely read well."""
    import sys

    monkeypatch.syspath_prepend(str(ROOT / "examples" / "acme-standards"))
    sys.modules.pop("acme_lockstep", None)
    import acme_lockstep

    lockstep = Lockstep()
    load_standards(lockstep, entries=[FakeEntry("acme", acme_lockstep.apply)])
    resolved = lockstep.policy.resolve()
    assert resolved.scan_input == "block"
    assert resolved.max_turns == 16
    assert "run_script" in resolved.deny_tools
    assert lockstep.standards == ["acme"]


def test_ls_prints_the_standards_section(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Config as code needs a reader, and an ambient contribution needs one twice over."""
    import importlib.metadata

    from click.testing import CliRunner

    from in_lockstep.cli import main

    for var in [v for v in os.environ if v.startswith("GITHUB_")]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda *, group: [
            FakeEntry("acme", lambda std: std.contribute(Policy(name="acme-floor", max_turns=16)))
        ],
    )
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "standards" in result.output
    assert "acme" in result.output
    assert "plugin:acme" in result.output, "the layer says where it came from"


def test_ls_says_none_installed_rather_than_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from click.testing import CliRunner

    from in_lockstep.cli import main

    for var in [v for v in os.environ if v.startswith("GITHUB_")]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "(none installed)" in result.output
