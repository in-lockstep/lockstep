"""SECRETS.md claims to list every secret a pipeline needs, so it has to.

The one it omitted was the most sensitive: the credential the model engine authenticates with.
Nothing this compiler emits references it — it is read by the workflows `gh aw compile` produces —
which is exactly why it went unlisted, and exactly why it has to be listed. A secret nobody set is
found when a run fails to authenticate, having already spent the setup.
"""

from __future__ import annotations

import pytest

from lockstep.emit.agentic import ENGINE_BY_PROVIDER, ENGINE_SECRET
from lockstep.emit.plan import compile_spec, engine_credentials
from lockstep.spec.load import load_spec

DOC = "SECRETS.md"


def secrets_doc(root):
    return compile_spec(root).files[DOC]


# --- the engine credential --------------------------------------------------


def test_the_engine_credential_is_listed(basic_spec_dir):
    doc = secrets_doc(basic_spec_dir)
    assert "ANTHROPIC_API_KEY" in doc
    assert "Engine credentials" in doc


def test_it_says_who_reads_it(basic_spec_dir):
    """Otherwise somebody looks for it in the compiled workflows and does not find it."""
    assert "gh aw compile" in secrets_doc(basic_spec_dir)


def test_it_names_the_agents_that_need_it(basic_spec_dir):
    engine, secret, agents = engine_credentials(load_spec(basic_spec_dir))[0]
    assert engine == "claude"
    assert secret == "ANTHROPIC_API_KEY"
    assert agents, "the credential should name what uses it"
    for agent in agents:
        assert agent in secrets_doc(basic_spec_dir)


def test_the_command_to_set_it_is_repository_wide(basic_spec_dir):
    """It is not a profile secret: one key serves every environment."""
    doc = secrets_doc(basic_spec_dir)
    assert "gh secret set ANTHROPIC_API_KEY" in doc
    assert "gh secret set ANTHROPIC_API_KEY --env" not in doc


def test_a_pipeline_with_no_agents_has_no_engine_section(basic_root):
    """No model runs, so there is no key to set, so the section would be noise."""
    import shutil

    for agent in (basic_root / "agents").glob("*.md"):
        agent.unlink()
    for command in (basic_root / "commands").glob("*.md"):
        command.unlink()
    shutil.rmtree(basic_root / "overlays", ignore_errors=True)
    assert engine_credentials(load_spec(basic_root)) == []
    assert "Engine credentials" not in secrets_doc(basic_root)


# --- the mapping itself -----------------------------------------------------


@pytest.mark.parametrize("engine", sorted(set(ENGINE_BY_PROVIDER.values())))
def test_every_engine_the_compiler_can_emit_has_a_known_credential(engine):
    """A provider that maps to an engine whose credential is unknown is a silent omission."""
    assert engine in ENGINE_SECRET, engine


def test_the_credential_is_not_invented_per_profile(basic_spec_dir):
    """Engine credentials and profile secrets are different things and must not be conflated."""
    spec = load_spec(basic_spec_dir)
    declared = {name for profile in spec.profiles.values() for name in profile.github.secrets}
    for _engine, secret, _agents in engine_credentials(spec):
        assert secret not in declared, "an engine credential is not a profile secret"
