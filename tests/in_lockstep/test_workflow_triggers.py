"""The chat-ops trigger, as a file that has to hold together.

A workflow is the one artifact in this repository that nothing type-checks, nothing imports and
nothing runs until it matters — and `implement.yml` is the file where a mistake means either an
unauthenticated entry point or an API key sharing a process with a write token. YAML review is not
a control; this is.

Three properties, and the second is the one worth the file:

  * the actor gate runs before anything that costs money or holds a credential;
  * no job holds a provider key AND write access — the split the whole design rests on;
  * every expression function used actually exists, because GitHub's expression language has no
    `trim()` and an invalid function is a silent `false`, which reads exactly like "nobody was
    authorized" and would have made the trigger simply never fire.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

#: Everything GitHub's expression language provides. Anything else in a `${{ }}` or an `if:` is a
#: typo that evaluates to false rather than an error.
EXPRESSION_FUNCTIONS = frozenset(
    {
        "contains",
        "startsWith",
        "endsWith",
        "format",
        "join",
        "toJSON",
        "fromJSON",
        "hashFiles",
        "success",
        "always",
        "cancelled",
        "failure",
    }
)

_CALL = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")


# `on` is a YAML 1.1 boolean, so a naive `safe_load` gives back `True` as the key. Loading with a
# loader that leaves it alone is the difference between reading the triggers and reading `True`.
class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor(
    "tag:yaml.org,2002:bool",
    lambda loader, node: (
        loader.construct_scalar(node)
        if loader.construct_scalar(node) in ("on", "off")
        else yaml.SafeLoader.construct_yaml_bool(loader, node)
    ),
)


def _load(name: str) -> dict:
    return yaml.load((WORKFLOWS / name).read_text(), Loader=_Loader)


ALL_WORKFLOWS = sorted(WORKFLOWS.glob("*.yml"))


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=[p.name for p in ALL_WORKFLOWS])
def test_every_expression_function_exists(path: Path) -> None:
    """`trim()` is not one, and an invalid function is `false` rather than an error.

    Which is the worst available failure: a trigger that never fires looks identical to a trigger
    nobody is authorized for, and both look like nothing happening.
    """
    text = path.read_text()
    used = {
        name
        for expression in re.findall(r"\$\{\{(.*?)\}\}", text, re.DOTALL)
        for name in _CALL.findall(expression)
    }
    # `if:` bodies are expressions without the braces. The lookahead ends the body at the next YAML
    # key, and that key is frequently hyphenated — `runs-on:`, `timeout-minutes:`. `\w+` does not
    # match a hyphen, so the body used to run on past those and swallow whatever followed, which
    # made any later prose containing `word (` read as a call to a function GitHub does not have.
    # Comments are dropped for the same reason: a `#` line inside the captured region is not an
    # expression, and treating it as one produces a failure about a trigger from a sentence.
    for block in re.findall(r"^\s*if:\s*(>-|\|)?\s*\n?((?:.*\n)*?)(?=^\s*[\w-]+:)", text, re.MULTILINE):
        body = "\n".join(line for line in block[1].splitlines() if not line.lstrip().startswith("#"))
        used |= set(_CALL.findall(body))
    unknown = used - EXPRESSION_FUNCTIONS
    assert not unknown, f"{path.name} calls {sorted(unknown)}, which GitHub does not provide"


def test_the_trigger_is_a_prefix_match_not_a_mention() -> None:
    """`contains` would fire on every comment explaining why not to run it."""
    condition = _load("implement.yml")["jobs"]["gate"]["if"]
    assert "startsWith(github.event.comment.body" in condition
    assert "contains(" not in condition


def test_a_pull_request_comment_fires_the_trigger_too() -> None:
    """It used to be filtered out, and the filter was the bug.

    `issue_comment` fires for pull-request comments as well, and a reviewer deciding another
    attempt is needed is standing on the pull request when they decide it. Sending them somewhere
    else to say so is how a tool teaches people it is awkward.
    """
    for name in ("implement.yml", "fix.yml"):
        condition = _load(name)["jobs"]["gate"]["if"]
        assert "issue.pull_request" not in condition, f"{name} still refuses a reviewer's comment"


def test_which_ticket_a_comment_is_about_is_not_decided_in_yaml() -> None:
    """The resolution is `ticket_for`, in Python, where it has tests.

    A pull-request comment carries the pull request's number, not the ticket's, so *something* has
    to resolve one to the other. Doing it in an `if:` or a `run:` would put lifecycle logic back
    in the file this repository keeps free of it — and it is the kind of logic that fails silently,
    because an expression that evaluates wrong reads exactly like "nobody asked".

    So the workflow passes the number through untouched and the workflow function resolves it.
    """
    for name in ("implement.yml", "fix.yml"):
        text = (WORKFLOWS / name).read_text()
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "issue.pull_request" not in body, f"{name} branches on the comment's location"
        assert "github.event.issue.number" in body, f"{name} no longer passes the number through"


def test_the_actor_gate_runs_before_anything_holding_a_credential() -> None:
    """An unauthorized comment must cost one job that installs nothing and calls nothing."""
    jobs = _load("implement.yml")["jobs"]
    gate = jobs["gate"]
    assert "in-lockstep gate" in yaml.dump(gate["steps"])
    assert gate["permissions"] == {"contents": "read"}
    assert "secrets." not in yaml.dump(gate), "the gate must not be reachable by a secret's absence"

    for name, job in jobs.items():
        if name == "gate":
            continue
        needs = job["needs"]
        assert "gate" in (needs if isinstance(needs, list) else [needs]), (
            f"{name} does not depend on the gate, so it runs for anyone who can type a comment"
        )


def test_no_job_holds_a_provider_key_and_write_access() -> None:
    """The split the whole two-job design exists for, asserted rather than reviewed.

    `id-token: write` is exempt, deliberately: it is not repository write access — it lets the
    job mint the runner's own identity token, which is HOW the provider credential comes to
    exist under workload identity federation. That grant belongs in exactly the job that calls
    the model; what must never sit beside a provider credential is the ability to write the
    repository, and that is what this asserts.
    """
    for path in ALL_WORKFLOWS:
        for name, job in (_load(path.name).get("jobs") or {}).items():
            body = yaml.dump(job)
            spends = "ANTHROPIC_API_KEY" in body
            permissions = job.get("permissions") or {}
            granted = sorted(k for k, v in permissions.items() if v == "write" and k != "id-token")
            assert not (spends and granted), (
                f"{path.name}:{name} holds a provider credential and {granted} write access in one process"
            )


def test_the_writing_job_does_not_install_a_provider_sdk() -> None:
    """`apply` must be constructible with no provider registry — a fact, not a convention."""
    propose = yaml.dump(_load("implement.yml")["jobs"]["propose"])
    assert "--extra anthropic" not in propose
    assert "ANTHROPIC" not in propose


def test_permissions_are_denied_at_the_top_and_granted_per_job() -> None:
    workflow = _load("implement.yml")
    assert workflow["permissions"] == {}, "a workflow-level grant reaches jobs that should not have it"
    for name, job in workflow["jobs"].items():
        assert "permissions" in job, f"{name} inherits rather than declaring what it needs"


def test_a_paid_run_is_not_cancelled_by_a_second_comment() -> None:
    """A cancelled model call may still be billed, and throws away what was paid for."""
    concurrency = _load("implement.yml")["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "issue.number" in concurrency["group"], "the group has to be per issue, not per repo"


#: The shapes a trigger's shell may take. An allowlist of STATEMENTS rather than a budget of
#: lines, which is a stronger rule and a more honest one.
#:
#: A line count was the first attempt and it measured the wrong thing: it went red when the
#: workflow started carrying its run record out as a bundle — one more invocation, no more logic —
#: and the tempting fix was to raise the number, which is how a ratchet becomes a formality. What
#: the rule is actually about is that a CI file invokes the framework and decides nothing, so it
#: matches on that directly. A `case`, an `if`, a composed commit message or a `gh` call fails
#: here whether or not the file got shorter.
ALLOWED_STATEMENTS = (
    re.compile(r"^uv sync( --extra [\w-]+)?$"),
    re.compile(r"^uv run in-lockstep [\w-]+( .*)?$"),
    # One output variable, so a later job can name the actor the gate verified.
    re.compile(r'^echo "\w+=\$\w+" >> "\$GITHUB_OUTPUT"$'),
)

#: Statements, not lines. Four jobs each doing its work and moving its evidence.
#:
#: Moved 12 -> 13 when the `report` job was added — the job that answers on the ticket when the
#: work half failed and `propose` was therefore skipped. Recorded rather than quietly bumped,
#: because a cap somebody raises whenever it bites is not a cap.
#:
#: Note what this number does and does not guard. `ALLOWED_STATEMENTS` above is the real gate: it
#: is what refuses an `if`, a `case`, a composed commit message or a `gh` call, and it did not move.
#: This is the secondary tripwire on growth, and every one of the thirteen is still a bare
#: invocation of the framework.
MAX_STATEMENTS = 13


def _statements(workflow: str) -> list[str]:
    """Shell statements, with backslash-continuations joined, comments and blanks dropped."""
    out: list[str] = []
    for job in _load(workflow)["jobs"].values():
        for step in job["steps"]:
            joined = re.sub(r"\\\s*\n\s*", " ", step.get("run", ""))
            for line in joined.splitlines():
                stripped = " ".join(line.split())
                if stripped and not stripped.startswith("#"):
                    out.append(stripped)
    return out


def test_the_trigger_carries_no_lifecycle_logic() -> None:
    """The claim `lockstep.yml`'s own header makes, enforced for the file most likely to break it.

    A workflow is the one artifact here that nothing type-checks, nothing imports and nothing runs
    until it matters. Process that lives in it is process with no tests.
    """
    statements = _statements("implement.yml")
    assert len(statements) <= MAX_STATEMENTS, f"{len(statements)} statements: {statements}"
    for statement in statements:
        assert any(p.match(statement) for p in ALLOWED_STATEMENTS), (
            f"`{statement}` is not an invocation of the framework. A CI file triggers a process; "
            f"it does not contain one. Put it in `.lockstep/lockstep.py` as a @workflow and call "
            f"`in-lockstep run <id>`."
        )


def test_the_trigger_does_not_reimplement_what_the_framework_does() -> None:
    """The specific things the first draft did in bash, each of which has a port behind it."""
    text = (WORKFLOWS / "implement.yml").read_text()
    body = "\n".join(
        line
        for line in text.splitlines()
        if line.strip()
        and not line.strip().lstrip("#").strip().startswith(("#",))
        and not line.strip().startswith("#")
    )
    for reimplemented, port in (
        ("git checkout -b", "Scm.open_change"),
        ("git commit", "Scm.open_change"),
        ("gh pr create", "Scm.open_change"),
        ("gh issue comment", "TicketSource.comment"),
    ):
        assert reimplemented not in body, (
            f"implement.yml runs `{reimplemented}` itself; {port} exists and is what a @workflow "
            f"in lockstep.py should call"
        )


def test_every_job_invokes_the_framework() -> None:
    """A job that never calls `in-lockstep` is a job doing something the framework does not know
    about, which is where process starts leaking back into YAML."""
    for name, job in _load("implement.yml")["jobs"].items():
        joined = " ".join(step.get("run", "") for step in job["steps"])
        assert "in-lockstep" in joined, f"{name} invokes the framework nowhere"


def test_no_workflow_asserts_an_egress_mode_this_repository_overrides() -> None:
    """A line that looks like a control and is not is worse than its absence.

    lockstep.py binds `UnsandboxedEgress`, and a bound policy is resolved before `detect()` reads
    the environment — so `IN_LOCKSTEP_EGRESS: enforced` in a workflow here does nothing while
    reading as though something were enforced.
    """
    module = (ROOT / ".lockstep" / "lockstep.py").read_text()
    binds_unsandboxed = "bind(EgressPolicy, UnsandboxedEgress())" in module or (
        "bind(EgressPolicy, egress)" in module and "egress = UnsandboxedEgress()" in module
    )
    if not binds_unsandboxed:
        pytest.skip("this repository no longer opts out, so the variable would mean something")
    for path in ALL_WORKFLOWS:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "IN_LOCKSTEP_EGRESS:" not in stripped, (
                f"{path.name} sets IN_LOCKSTEP_EGRESS while lockstep.py binds UnsandboxedEgress, "
                f"which resolves first. The variable is never read."
            )


def test_a_run_that_did_the_work_and_could_not_publish_it_still_answers() -> None:
    """`report` used to be `needs: [gate, <work>]` with a bare `failure()`, which covered the run
    that produced nothing and missed the run that produced something and could not publish it.

    Run 33578430422 is the second case: the model implemented #146, the suite went green, and
    `propose` was refused by the host over a title length. Because the work job had SUCCEEDED, this
    job was skipped and the issue heard nothing — the failure mode this job exists to remove,
    reappearing on the outcome where the loss is largest, because a change actually existed.
    """
    for path, work in (
        (ROOT / ".github/workflows/implement.yml", "implement"),
        (ROOT / ".github/workflows/fix.yml", "fix"),
    ):
        data = yaml.safe_load(path.read_text())
        report = data["jobs"]["report"]
        assert "propose" in report["needs"], f"{path.name}: report cannot see propose's outcome"
        condition = str(report["if"])
        assert f"needs.{work}.result == 'failure'" in condition
        assert "needs.propose.result == 'failure'" in condition


class _StrictLoader(_Loader):
    """A loader that refuses a duplicate key rather than keeping the last one."""


def _no_duplicate_keys(loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise AssertionError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor("tag:yaml.org,2002:map", _no_duplicate_keys)


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_repeats_a_key(path: Path) -> None:
    """YAML keeps the LAST of two identical keys and says nothing, and neither does `safe_load` —
    so a step with two `env:` blocks silently loses the first one's variables.

    Written after exactly that: a `review.yml` step carried `env: {ISSUE: ...}` and then
    `env: {GH_TOKEN: ...}`, and every other assertion in this file passed over a workflow whose
    job would have run without the number it was about. A file nothing type-checks needs the
    checks it can get.
    """
    yaml.load(path.read_text(), Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_a_comment_body_never_reaches_a_shell(path: Path) -> None:
    """`${{ github.event.comment.body }}` inside a `run:` is shell injection, written by anyone who
    can comment, into whichever job holds that step's credentials.

    It travels as an environment variable and the script quotes it. The distinction is invisible on
    a screen and total in effect, which is what makes it worth a test rather than a review.
    """
    for job, spec in (_load(path.name).get("jobs") or {}).items():
        for step in spec.get("steps") or []:
            assert "github.event.comment.body" not in (step.get("run") or ""), (
                f"{path.name}:{job} interpolates a comment body into a shell script; pass it "
                f"through `env:` and quote it"
            )
