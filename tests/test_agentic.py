"""Agent lowering: engine mapping, budgets, prompt layering, and tool allow-lists."""

from __future__ import annotations

import pytest
import yaml

from lockstep.emit import compile_spec
from lockstep.emit.agentic import ENGINE_BY_PROVIDER, resolve_engine
from lockstep.emit.mcp import allowed_tools
from lockstep.errors import EmitError, UnmappedProvider
from lockstep.spec.model import Agent, Enforce, McpServer

AGENT_FILE = ".github/workflows/aw-story-extractor.md"


def frontmatter(text: str) -> dict:
    body = text.split("---", 2)[1]
    return yaml.safe_load("\n".join(line for line in body.splitlines() if not line.startswith("#")))


def test_agent_compiles_to_a_single_item_workflow_call(basic_spec_dir):
    front = frontmatter(compile_spec(basic_spec_dir).files[AGENT_FILE])
    call = (front.get("on") or front.get(True))["workflow_call"]
    assert call["inputs"]["output_path"]["required"] is True
    assert "item" in call["inputs"]


def test_agent_job_is_read_only(basic_spec_dir):
    granted = frontmatter(compile_spec(basic_spec_dir).files[AGENT_FILE])["permissions"]
    # An explicit read-only map rather than `read-all`: a calling job cannot grant the latter
    # without enumerating every scope GitHub has, so an agent asking for it cannot start.
    assert granted == {"actions": "read", "contents": "read"}


def test_turn_cap_and_credit_budget_are_carried_over(basic_spec_dir):
    front = frontmatter(compile_spec(basic_spec_dir).files[AGENT_FILE])
    assert front["max-turns"] == 8
    assert front["max-ai-credits"] == 40


def test_mcp_allow_list_is_narrowed_by_enforced_guardrails(basic_spec_dir):
    front = frontmatter(compile_spec(basic_spec_dir).files[AGENT_FILE])
    assert front["mcp-servers"]["jira"]["allowed"] == ["search_issues", "get_issue"]


def test_mcp_secrets_resolve_to_declared_references(basic_spec_dir):
    env = frontmatter(compile_spec(basic_spec_dir).files[AGENT_FILE])["mcp-servers"]["jira"]["env"]
    assert env["JIRA_PERSONAL_TOKEN"] == "${{ secrets.JIRA_API_TOKEN }}"
    assert env["JIRA_URL"] == "${{ vars.JIRA_BASE_URL }}"


def test_guardrails_are_inlined_before_the_agent_body(basic_spec_dir):
    text = compile_spec(basic_spec_dir).files[AGENT_FILE]
    body = text.split("---", 2)[2]
    assert body.index("You MUST return valid JSON") < body.index("You read a single Jira issue")


def test_skills_and_contexts_are_imported_in_order(basic_spec_dir):
    front = frontmatter(compile_spec(basic_spec_dir).files[AGENT_FILE])
    assert front["imports"] == [
        "shared/skill-test-common.md",
        "shared/context-my-app-patterns.md",
    ]


def test_every_prompt_layer_is_also_written_for_audit(basic_spec_dir):
    files = compile_spec(basic_spec_dir).files
    assert ".github/workflows/shared/guardrail-common.md" in files
    assert ".github/workflows/shared/skill-test-common.md" in files
    assert ".github/workflows/shared/context-my-app-patterns.md" in files


@pytest.mark.parametrize(("provider", "engine"), sorted(ENGINE_BY_PROVIDER.items()))
def test_provider_maps_to_engine(provider, engine):
    assert resolve_engine(Agent(name="a", provider=provider)) == engine


def test_unmapped_provider_is_refused_with_a_reason():
    with pytest.raises(UnmappedProvider) as excinfo:
        resolve_engine(Agent(name="a", provider="ollama"))
    assert "local backend" in excinfo.value.render()


def test_agent_without_a_credit_budget_is_refused(basic_root):
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("  max-ai-credits: 40\n", ""))
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "budget is not optional" in excinfo.value.render()


def test_unresolvable_model_reference_is_refused(basic_root):
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("model: claude-sonnet-4-6", "model: ${CLAUDE_MODEL}"))
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "cannot be resolved at compile time" in excinfo.value.render()


def test_text_only_agent_gets_no_tools(basic_root):
    agent = basic_root / "agents" / "story-extractor.md"
    agent.write_text(agent.read_text().replace("max_tool_turns: 8", "max_tool_turns: 0"))
    front = frontmatter(compile_spec(basic_root).files[AGENT_FILE])
    assert "mcp-servers" not in front


def _enforce_deny_all(root):
    guardrail = root / "guardrails" / "common.md"
    guardrail.write_text(
        guardrail.read_text().replace(
            "  permissions: read-all", "  permissions: read-all\n  network: deny-all"
        )
    )


def test_deny_all_network_compiles_to_an_empty_allow_list(basic_root):
    _enforce_deny_all(basic_root)
    (basic_root / "overlays" / "github" / "generate-tests.yml").unlink()
    front = frontmatter(compile_spec(basic_root).files[AGENT_FILE])
    assert front["network"]["allowed"] == []


def test_overlay_cannot_widen_an_enforced_network_floor(basic_root):
    """`enforce:` is a floor. A customization tier must not be able to reopen egress."""
    _enforce_deny_all(basic_root)
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "deny-all" in excinfo.value.render()


def test_overlay_cannot_grant_write_permissions(basic_root):
    overlay = basic_root / "overlays" / "github" / "generate-tests.yml"
    overlay.write_text(
        overlay.read_text().replace(
            "  - op: merge\n    at: network\n    value:\n      allowed: ['jira-mirror.acme.internal']",
            "  - op: merge\n    at: permissions\n    value: write-all",
        )
    )
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "read-only" in excinfo.value.render()


def test_allowed_tools_honours_explicit_denies():
    server = McpServer(name="s", tools=["get_issue", "delete_issue", "search"])
    assert allowed_tools(server, Enforce(deny_tools=["delete_*"])) == ["get_issue", "search"]


def test_agents_with_divergent_layer_sets_are_refused(basic_root):
    """Identity is agent x resolved layer set; silently merging two prompt sets would be a lie."""
    extra = basic_root / "guardrails" / "strict.md"
    extra.write_text("---\nname: strict\ndescription: extra\n---\n\nBe strict.\n")
    original = (
        "1. **Discover API surface** → script: scripts/discover-api.py\n"
        "   - args: --output={output_dir}/api-endpoints.json --api-url={api_url}"
    )
    replacement = "1. **Extract** → agent: story-extractor\n   - output: {output_dir}/x.json"
    command = basic_root / "commands" / "discover.md"
    command.write_text(
        command.read_text()
        .replace(original, replacement)
        .replace("guardrails: [common]", "guardrails: [common, strict]")
    )
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "different prompt layers" in excinfo.value.render()
