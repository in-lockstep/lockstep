"""Error paths. A compiler is judged by what it refuses and how clearly it says why."""

from __future__ import annotations

import pytest

from lockstep.emit import compile_spec
from lockstep.emit.context import Pins
from lockstep.emit.mcp import emit_server
from lockstep.emit.profiles import env_block, named_secrets, prefix_for
from lockstep.errors import EmitError, MissingDefinition, SpecError
from lockstep.spec.load import load_manifest, load_spec
from lockstep.spec.model import Enforce, McpServer, Profile, ProfileGithub


def test_missing_manifest_names_the_fix(tmp_path):
    with pytest.raises(MissingDefinition) as excinfo:
        load_manifest(tmp_path)
    assert "pipeline.yaml" in excinfo.value.render()


def test_unsupported_spec_version_is_refused(basic_root):
    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(manifest.read_text().replace("spec: 1", "spec: 99"))
    with pytest.raises(SpecError) as excinfo:
        load_spec(basic_root)
    assert "spec: 1" in excinfo.value.render()


def test_manifest_listing_an_unknown_command_is_refused(basic_root):
    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(manifest.read_text() + "\ncommands:\n  nonexistent: {}\n")
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "nonexistent" in excinfo.value.render()


def test_missing_script_is_caught_at_compile_time(basic_root):
    (basic_root / "scripts" / "fetch-issues.py").unlink()
    with pytest.raises(MissingDefinition) as excinfo:
        load_spec(basic_root)
    assert "fetch-issues.py" in excinfo.value.render()


def test_unknown_nested_command_is_caught(basic_root):
    command = basic_root / "commands" / "generate-tests.md"
    command.write_text(command.read_text().replace("command: discover", "command: ghost"))
    with pytest.raises(MissingDefinition) as excinfo:
        load_spec(basic_root)
    assert "ghost" in excinfo.value.render()


@pytest.mark.parametrize(
    ("field", "value"),
    [("guardrails", "[nope]"), ("skills", "[nope]"), ("mcp", "[nope]")],
)
def test_unknown_agent_references_are_caught(basic_root, field, value):
    agent = basic_root / "agents" / "story-extractor.md"
    text = agent.read_text()
    import re

    agent.write_text(re.sub(rf"^{field}:.*$", f"{field}: {value}", text, count=1, flags=re.MULTILINE))
    with pytest.raises(MissingDefinition) as excinfo:
        load_spec(basic_root)
    assert "nope" in excinfo.value.render()


def test_unknown_profile_context_is_caught(basic_root):
    profile = basic_root / "profiles" / "my-app.md"
    profile.write_text(profile.read_text().replace("contexts: [my-app-patterns]", "contexts: [nope]"))
    with pytest.raises(MissingDefinition) as excinfo:
        load_spec(basic_root)
    assert "nope" in excinfo.value.render()


def test_non_mapping_frontmatter_is_refused(basic_root):
    (basic_root / "contexts" / "my-app-patterns.md").write_text("---\n- a list\n---\n\nbody\n")
    with pytest.raises(SpecError) as excinfo:
        load_spec(basic_root)
    assert "YAML mapping" in excinfo.value.render()


def test_profile_value_referencing_an_undeclared_secret_is_refused(basic_root):
    profile = basic_root / "profiles" / "my-app.md"
    profile.write_text(profile.read_text().replace("auth_method=jwt", "token=${UNDECLARED_TOKEN}"))
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    rendered = excinfo.value.render()
    assert "UNDECLARED_TOKEN" in rendered
    assert "will not guess where a credential lives" in rendered


def test_unpinned_capability_is_refused(basic_root):
    (basic_root / ".pipeline" / "pins.lock").unlink()
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "lockstep pin" in excinfo.value.render()


def test_unpinned_executor_image_is_refused(basic_root):
    pins = basic_root / ".pipeline" / "pins.lock"
    pins.write_text(pins.read_text().replace('"digest": "sha256:', '"ignored": "sha256:'))
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "digest" in excinfo.value.render()


def test_unpinned_external_action_is_refused(basic_root):
    pins = basic_root / ".pipeline" / "pins.lock"
    pins.write_text(pins.read_text().replace("actions/checkout", "actions/nothing"))
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "actions/checkout" in excinfo.value.render()


def test_a_spec_with_no_profiles_is_refused(basic_root):
    (basic_root / "profiles" / "my-app.md").unlink()
    manifest = basic_root / "pipeline.yaml"
    manifest.write_text(manifest.read_text().replace("    profiles: [my-app]", ""))
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "no profiles to compile" in excinfo.value.render()


def test_stdio_mcp_servers_lower_to_command_and_args():
    profile = Profile(name="p", github=ProfileGithub(secrets=["TOKEN"], vars=["URL"]))
    server = McpServer(
        name="git",
        command="uvx",
        args=["mcp-server-git", "--url", "${URL}", "--token", "${TOKEN}"],
        tools=["git_log", "git_diff"],
    )
    entry = emit_server(server, Enforce(), profile)
    assert entry["command"] == "uvx"
    assert entry["args"] == ["mcp-server-git", "--url", "${{ vars.URL }}", "--token", "${{ secrets.TOKEN }}"]
    assert entry["allowed"] == ["git_log", "git_diff"]


def test_unknown_env_reference_in_a_server_is_left_untouched():
    entry = emit_server(
        McpServer(name="s", command="x", env={"K": "${UNKNOWN}"}, tools=[]),
        Enforce(),
        Profile(name="p"),
    )
    assert entry["env"] == {"K": "${UNKNOWN}"}


def test_profile_prefix_follows_the_runtime_convention():
    assert prefix_for(Profile(name="ao-local")) == "AO"
    assert prefix_for(Profile(name="staging")) == "STAGING"


def test_named_secrets_reports_only_what_is_consumed():
    profile = Profile(
        name="p",
        github=ProfileGithub(secrets=["USED", "UNUSED"], vars=[]),
        values={"a": "${USED}"},
    )
    assert named_secrets(profile, env_block(profile)) == ["USED"]


def test_pins_without_a_lockfile_carry_manifest_tags(basic_spec_dir):
    spec = load_spec(basic_spec_dir)
    pins = Pins.load(basic_spec_dir.parent / "does-not-exist", spec)
    assert pins.actions_tag == "v1.6.2"
    assert pins.resolved is False
