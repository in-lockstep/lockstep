"""The step grammar is shared with the local runtime, so parsing is where fidelity is won or lost."""

from __future__ import annotations

import pytest

from lockstep.errors import BadStepSyntax, MissingDefinition, SpecError
from lockstep.spec.load import load_spec
from lockstep.spec.model import StepKind
from lockstep.spec.parse import parse_steps


def test_parses_every_step_kind():
    steps = parse_steps(
        """
1. **Run a script** → script: scripts/a.py
2. **Call an agent** → agent: my-agent
3. **Use a builtin** → builtin: test-runner
4. **Nest a command** → command: other
""",
        location="t.md",
    )
    assert [s.kind for s in steps] == [
        StepKind.SCRIPT,
        StepKind.AGENT,
        StepKind.BUILTIN,
        StepKind.COMMAND,
    ]
    assert [s.target for s in steps] == ["scripts/a.py", "my-agent", "test-runner", "other"]


def test_accepts_ascii_arrow():
    steps = parse_steps("1. **Go** -> script: scripts/a.py", location="t.md")
    assert steps[0].target == "scripts/a.py"


def test_parses_subkeys_and_hooks():
    steps = parse_steps(
        """
1. **Work** → script: scripts/a.py
   - args: --output={output_dir}/x.json
   - pre: echo before
   - post: echo after
   - on-failure: echo boom
""",
        location="t.md",
    )
    step = steps[0]
    assert step.args["args"] == "--output={output_dir}/x.json"
    assert (step.pre, step.post, step.on_failure) == ("echo before", "echo after", "echo boom")


def test_parses_foreach_and_parallel():
    steps = parse_steps(
        """
1. **Each** → agent: a
   - foreach: issue in {output_dir}/issues.json
   - foreach-key: id
   - parallel: 3
""",
        location="t.md",
    )
    assert steps[0].foreach is not None
    assert steps[0].foreach.var == "issue"
    assert steps[0].foreach.source == "{output_dir}/issues.json"
    assert steps[0].foreach.key_field == "id"
    assert steps[0].parallel == 3


def test_rejects_malformed_foreach():
    with pytest.raises(BadStepSyntax):
        parse_steps("1. **X** → agent: a\n   - foreach: nonsense\n", location="t.md")


@pytest.mark.parametrize(
    ("line", "flag", "negated"),
    [("(if --pdf)", "--pdf", False), ("(if not --skip-deploy)", "--skip-deploy", True)],
)
def test_parses_conditions(line, flag, negated):
    steps = parse_steps(f"1. **X** → script: scripts/a.py\n   {line}\n", location="t.md")
    assert steps[0].condition is not None
    assert steps[0].condition.flag == flag
    assert steps[0].condition.negated is negated


def test_derives_stable_ids_and_honours_explicit_ones():
    steps = parse_steps(
        """
1. **Fetch issues from Jira** → script: scripts/a.py
2. **Fetch issues from Jira** → script: scripts/b.py
3. **Anything** → script: scripts/c.py
   - id: pinned
""",
        location="t.md",
    )
    assert [s.id for s in steps] == ["fetch-issues-from-jira", "fetch-issues-from-jira-2", "pinned"]
    assert steps[2].explicit_id is True


def test_rejects_duplicate_explicit_ids():
    with pytest.raises(SpecError):
        parse_steps(
            "1. **A** → script: a.py\n   - id: same\n2. **B** → script: b.py\n   - id: same\n",
            location="t.md",
        )


def test_targets_gate_steps_per_backend():
    steps = parse_steps("1. **Deploy** → script: scripts/a.sh\n   - targets: [local]\n", location="t.md")
    assert steps[0].applies_to("local") is True
    assert steps[0].applies_to("github") is False


def test_loads_the_fixture_spec(basic_spec_dir):
    spec = load_spec(basic_spec_dir)
    assert set(spec.commands) == {"discover", "generate-tests"}
    assert spec.agents["story-extractor"].max_tool_turns == 8
    assert spec.guardrails["common"].enforce.permissions == "read-all"
    assert spec.skills["test/common"].name == "test/common"
    assert spec.profiles["my-app"].values["auth_method"] == "jwt"
    assert spec.mcp_servers["jira"].tools[0] == "search_issues"


def test_unknown_agent_reference_fails_at_compile_time(basic_root):
    command = basic_root / "commands" / "generate-tests.md"
    command.write_text(command.read_text().replace("agent: story-extractor", "agent: nope"))
    with pytest.raises(MissingDefinition) as excinfo:
        load_spec(basic_root)
    assert "nope" in str(excinfo.value)
