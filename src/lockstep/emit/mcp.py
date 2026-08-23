"""MCP server lowering: servers.json becomes gh-aw `mcp-servers:` with a computed allow-list."""

from __future__ import annotations

import fnmatch
from typing import Any

from ..spec.model import Agent, Enforce, McpServer, Profile, Spec
from .profiles import ENV_REF, secret_ref, var_ref

# What the read-only enforcement preset strips, regardless of what a server advertises.
READ_ONLY_DENY = ("create_*", "update_*", "delete_*", "transition_*", "write_*", "add_*")


def allowed_tools(server: McpServer, enforce: Enforce) -> list[str]:
    """Server tools intersected with the guardrails' enforceable denials."""
    patterns = list(enforce.deny_tools)
    if enforce.permissions == "read-all":
        patterns.extend(READ_ONLY_DENY)
    return [tool for tool in server.tools if not any(fnmatch.fnmatch(tool, p) for p in patterns)]


def _resolve_env(value: str, profile: Profile) -> str:
    """`${JIRA_API_TOKEN}` in servers.json resolves against the profile's declared secrets/vars."""
    match = ENV_REF.match(value.strip())
    if not match:
        return value
    name = match.group(1)
    if name in profile.github.secrets:
        return secret_ref(name)
    if name in profile.github.vars:
        return var_ref(name)
    return value


def emit_server(server: McpServer, enforce: Enforce, profile: Profile) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if server.container:
        entry["container"] = server.container
    else:
        entry["command"] = server.command
        if server.args:
            entry["args"] = [_resolve_env(a, profile) for a in server.args]
    if server.env:
        entry["env"] = {k: _resolve_env(v, profile) for k, v in server.env.items()}
    entry["allowed"] = allowed_tools(server, enforce)
    return entry


def emit_mcp_servers(
    agent: Agent, spec: Spec, enforce: Enforce, profile: Profile
) -> dict[str, dict[str, Any]]:
    """An agent with `max_tool_turns: 0` gets no servers at all — a text-only agent needs no tools."""
    if agent.max_tool_turns == 0:
        return {}
    return {
        name: emit_server(spec.mcp_servers[name], enforce, profile)
        for name in agent.mcp
        if name in spec.mcp_servers
    }


def secrets_used(servers: dict[str, dict[str, Any]], profile: Profile) -> list[str]:
    """Named secrets an agent's MCP config consumes, for its explicit `secrets:` block."""
    blob = str(servers)
    return sorted({name for name in profile.github.secrets if secret_ref(name) in blob})
