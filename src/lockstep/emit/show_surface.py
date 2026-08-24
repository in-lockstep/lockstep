"""`--show-surface`: every GitHub-target decision in one reviewable document.

Target config is deliberately spread across the definitions it belongs to, which makes
"show me the whole GitHub surface" a question the spec cannot answer by itself. This answers it.
"""

from __future__ import annotations

from pathlib import Path

from ..spec.load import load_spec
from ..spec.model import Spec
from .context import Pins
from .overlay import load_overlays


def _daily_budget_line(per_agent: int | None, agents: int) -> str:
    """The daily ceiling, and what it actually permits.

    gh-aw enforces this per agent workflow per day. Printing only the configured number would let a
    repository with seven agents read "5000 credits a day" off its own surface document and be
    wrong by a factor of seven — which is the precise failure this document exists to prevent.
    """
    if per_agent is None:
        return "- daily ceiling: (none) — a run budget bounds one execution, not a day of them"
    return (
        f"- daily ceiling: {per_agent} credits per agent per day"
        f" — up to {per_agent * agents} across {agents} agent(s)"
    )


def render(root: Path) -> str:
    spec: Spec = load_spec(root)
    pins = Pins.load(spec)
    overlays = load_overlays(spec)
    manifest = spec.manifest
    # A capability the output never names is not unpinned; it is unused. Saying UNPINNED reads as a
    # pipeline that is not ready when it is.
    used = spec.capabilities_used()

    if used.actions:
        actions_line = f"- capability actions: `{pins.actions_repo}@{pins.actions_tag}`" + (
            f" -> `{pins.actions_sha}`" if pins.actions_sha else "  **UNPINNED**"
        )
    else:
        actions_line = "- capability actions: `(unused)`"
    if used.executor:
        executor_line = f"- executor: `{pins.exec_package}=={pins.exec_version}`" + (
            f", image `{pins.exec_image}@{pins.exec_digest}`" if pins.exec_digest else "  **UNPINNED**"
        )
    else:
        executor_line = "- executor: `(unused)`"

    from .plan import engine_credentials

    credentials = engine_credentials(spec)

    lines = [
        f"# GitHub target surface — {manifest.name}",
        "",
        "## Pins",
        "",
        actions_line,
        executor_line,
        f"- gh-aw: `{pins.gh_aw_version or '(unset)'}`",
        *(f"- engine credential: `{secret}` (engine `{engine}`)" for engine, secret, _ in credentials),
        f"- budget: {manifest.per_run_ai_credits or '(none)'} credits per run",
        _daily_budget_line(manifest.per_agent_daily_ai_credits, len(spec.agents)),
        "",
        "## Commands",
        "",
    ]
    for name, command in sorted(spec.commands.items()):
        cmd_gh = command.github
        lines.append(f"### `{name}`")
        lines.append("")
        lines.append(f"- triggers: `{cmd_gh.triggers or 'workflow_dispatch only'}`")
        lines.append(f"- runs-on: `{cmd_gh.runs_on or manifest.target.default_runs_on}`")
        if cmd_gh.timeout_minutes:
            lines.append(f"- timeout-minutes: `{cmd_gh.timeout_minutes}`")
        if cmd_gh.max_iterations > 1:
            lines.append(f"- max-iterations: `{cmd_gh.max_iterations}`")
        local_only = [s.label for s in command.steps if not s.applies_to("github")]
        if local_only:
            lines.append(f"- steps not compiled (local-only): {', '.join(repr(s) for s in local_only)}")
        lines.append("")

    lines.extend(["## Agents", ""])
    for name, agent in sorted(spec.agents.items()):
        agent_gh = agent.github
        lines.append(f"### `{name}`")
        lines.append("")
        engine = agent_gh.engine or agent.provider or "(default)"
        lines.append(f"- engine: `{engine}` model `{agent_gh.model or agent.model}`")
        lines.append(f"- max-turns: `{agent.max_tool_turns}` · credits: `{agent_gh.max_ai_credits}`")
        lines.append(f"- network: `{agent_gh.network or ['defaults']}`")
        lines.append(f"- mcp: `{agent.mcp or '(none)'}`")
        if agent_gh.safe_outputs:
            lines.append(f"- safe-outputs: `{agent_gh.safe_outputs}`")
        lines.append("")

    lines.extend(["## Profiles", ""])
    for name, profile in sorted(spec.profiles.items()):
        profile_gh = profile.github
        lines.append(f"### `{name}`")
        lines.append("")
        lines.append(f"- environment: `{profile_gh.environment or '(repository level)'}`")
        lines.append(f"- secrets: `{profile_gh.secrets or '(none)'}`")
        lines.append(f"- vars: `{profile_gh.vars or '(none)'}`")
        lines.append(f"- deploy: mode `{profile_gh.deploy.mode}`")
        lines.append("")

    enforcing = [
        (n, g)
        for n, g in sorted(spec.guardrails.items())
        if g.enforce.permissions or g.enforce.network or g.enforce.deny_tools
    ]
    if enforcing:
        lines.extend(["## Enforced guardrails", ""])
        for name, guardrail in enforcing:
            enforce = guardrail.enforce
            lines.append(
                f"- `{name}`: permissions `{enforce.permissions or '-'}`, "
                f"network `{enforce.network or '-'}`, deny-tools `{enforce.deny_tools or '-'}`"
            )
        lines.append("")

    lines.extend(["## Overlays", ""])
    if not overlays:
        lines.append("(none)")
    for overlay in overlays:
        hunks = len(overlay.patches) + len(overlay.frontmatter) + len(overlay.prompt)
        lines.append(f"- `{overlay.rel}` -> `{overlay.target}` ({hunks} hunks)")
    lines.append("")
    return "\n".join(lines)
