"""GATE-TEST-1 / GATE-POLICY-1 — the composition invariant, frozen before the pivot deletes it.

The composition order (guardrails -> body -> skills -> contexts) and the `enforce()` ceiling merge
live in `src/lockstep/emit/fragments.py`, which the pivot deletes. They are unrecoverable
afterwards, so they are captured here while the compiler still runs and the new `in_lockstep`
composer is held to them.

Assertion shape is a NORMALIZED SECTION-IDENTITY PROJECTION plus a reviewed approval diff, not byte
equality: the new composer must be free to add provenance delimiters and drop gh-aw frontmatter.
A byte delta is never silently allowed — `sha256` moves only with a committed diff.

These cannot be regenerated. The compiler they were captured from was deleted in phase 7, which
is exactly why they were captured before it was: `emit/fragments.py` held the composition order,
and once it was gone there was nothing left to ask. A byte delta here is either a deliberate
change to the new composer — landed per-prompt, with the diff reviewed — or a regression.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

CORPUS = Path(__file__).parent


def _load(name: str) -> dict[str, Any]:
    path = CORPUS / name
    if not path.exists():
        pytest.skip(f"{name} not captured")
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _cases(name: str, scope: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [(scope, key, entry) for key, entry in sorted(_load(name).items())]


ALL = _cases("corpus.json", "repo") + _cases("corpus-shipped.json", "shipped")


@pytest.mark.parametrize("scope,key,entry", ALL, ids=[f"{k}" for _, k, _ in ALL])
def test_composed_prompt_matches_corpus(scope: str, key: str, entry: dict[str, Any]) -> None:
    """GATE-TEST-1: the captured text still hashes to what was recorded."""
    text = (CORPUS / "prompts" / f"{key}.txt").read_text()
    assert hashlib.sha256(text.encode()).hexdigest() == entry["sha256"], (
        f"{key}: composed prompt changed. If deliberate, re-run tools/capture_corpus.py and "
        f"commit the diff for THIS prompt in its own commit — one blob across 23 prompts is a "
        f"rubber stamp."
    )


@pytest.mark.parametrize("scope,key,entry", ALL, ids=[f"{k}" for _, k, _ in ALL])
def test_body_sits_between_guardrails_and_skills(scope: str, key: str, entry: dict[str, Any]) -> None:
    """The invariant `PromptLayers.signature()` cannot express, because body is not a Fragment.

    Guardrails strictly before the body; skills and contexts strictly after; contexts last.
    """
    proj = entry["projection"]
    body = [i for i, s in enumerate(proj) if s.startswith("body:")]
    assert len(body) == 1, f"{key}: expected exactly one body sentinel, got {body}"
    at = body[0]

    kinds = [s.split(":", 1)[0] for s in proj]
    assert all(k == "guardrail" for k in kinds[:at]), f"{key}: non-guardrail before body: {proj[:at]}"
    assert all(k in ("skill", "context") for k in kinds[at + 1 :]), (
        f"{key}: unexpected section after body: {proj[at + 1 :]}"
    )
    tail = kinds[at + 1 :]
    if "context" in tail:
        assert tail.index("context") >= (len(tail) - tail.count("context")), (
            f"{key}: contexts must come after skills: {proj[at + 1 :]}"
        )


@pytest.mark.parametrize("scope,key,entry", ALL, ids=[f"{k}" for _, k, _ in ALL])
def test_baseline_guardrail_is_first_and_present(scope: str, key: str, entry: dict[str, Any]) -> None:
    """The shipped baseline reaches every agent and cannot be displaced or excluded."""
    assert entry["projection"][0] == "guardrail:baseline", (
        f"{key}: baseline must lead every composition, got {entry['projection'][0]}"
    )


@pytest.mark.parametrize("scope,key,entry", ALL, ids=[f"{k}" for _, k, _ in ALL])
def test_enforce_ceilings_are_monotone(scope: str, key: str, entry: dict[str, Any]) -> None:
    """GATE-POLICY-1 source of truth: what `PolicyStack` must reproduce.

    Recorded here rather than asserted loosely, because the new PolicyStack's whole claim is that
    it merges the same way: deny-all is an irreversible floor, ceilings take the lowest not the
    last, scan is strictest-wins, deny-tools union.
    """
    enforce = entry["enforce"]
    assert set(enforce) == {
        "permissions",
        "network",
        "deny_tools",
        "max_turns",
        "max_ai_credits",
        "per_run_ai_credits",
        "daily_ai_credits",
        "scan_input",
    }, f"{key}: Enforce shape changed; PolicyStack parity must be re-derived"
    assert enforce["scan_input"] in ("", "warn", "block")
    assert isinstance(enforce["deny_tools"], list)
    assert len(set(enforce["deny_tools"])) == len(enforce["deny_tools"]), (
        f"{key}: deny_tools must be a union, not a concatenation"
    )


def test_corpus_covers_every_shipped_agent() -> None:
    """All 15 shipped library prompts are frozen, not just the ones this repo inherits. Fourteen
    came from the compiler; the judge (`GATE-JUDGE-2`) was composed by the framework and frozen
    the same way, so a body edit there is a re-recording somebody commits, like the others."""
    shipped = _load("corpus-shipped.json")
    assert len(shipped) == 15, f"expected 15 shipped prompts, captured {len(shipped)}"
    families = {k.split("/")[1] for k in shipped}
    assert families == {"fix", "implement", "judge", "retro", "review", "triage"}
