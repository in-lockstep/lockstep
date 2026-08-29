"""The lifecycle for this repository.

This file is the configuration. It is executed, not parsed — there is no schema, no manifest and
nothing generated from it. It replaces `.lockstep/pipeline.yaml` plus the seven directories that
used to sit beside it.

Keep it pure: it is imported to be inspected (`in-lockstep ls`) as well as run, so it may
construct objects and bind them, but it must not perform IO at import.

One consequence of configuration being code is worth stating where somebody will read it: this
file can rebind any adapter, remove any middleware, and grant any tool. That is why it is the
first entry in the protected-path deny list, and why it is loaded from a trusted ref rather than
from whichever branch is under review.
"""

from typing import Any

from in_lockstep import Lockstep
from in_lockstep.adapters import PytestTest, RuffValidate
from in_lockstep.adapters.ai.implement import AiImplement, Implement, ImplementSpec
from in_lockstep.adapters.pytest_adapter import Test
from in_lockstep.adapters.ruff_adapter import Validate
from in_lockstep.adapters.sandbox import Sandbox
from in_lockstep.ai.bootstrap import invoker_factory
from in_lockstep.ai.invoker import InvokePolicy
from in_lockstep.ai.pricing import default_table
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.policy import Policy
from in_lockstep.core.spend import Budget
from in_lockstep.core.workflow import workflow
from in_lockstep.middleware import CostBudget, otel
from in_lockstep.middleware.approval import ApprovalGate
from in_lockstep.platform.artifacts import read_changeset, write_changeset
from in_lockstep.platform.scm import GitHubScm, Scm
from in_lockstep.platform.tickets import GitHubIssues, TicketSource
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress
from in_lockstep.strategies import default_registry

lockstep = Lockstep.detect()

# -- deterministic verbs ------------------------------------------------------------
#
# Both run out of process. pytest executes conftest.py from this repository and ruff loads its
# configuration, so an in-process run would hand repository-authored Python the credentials this
# process holds.
lockstep.bind(Test, PytestTest(args=["-q", "--no-header"], sandbox=Sandbox()))
lockstep.bind(Validate, RuffValidate(sandbox=Sandbox()))

# -- policy -------------------------------------------------------------------------
#
# Contributions append and only ever tighten. There is no removal API: what this preserves is that
# taking a standard away is a visible diff, not that it is impossible.
# Both numbers below were sized when the only AI verb was a single-turn reviewer, and both have
# been resized now that a verb explores before it acts.
#
# `deny_tools` changed shape rather than merely loosening, and the reasoning is worth reading
# before the next person tightens it back.
#
#   * `write_file` and `delete_file` are no longer denied. That deny was a proxy for "no agent
#     writes in this repository", written when no verb had write tools, so it cost nothing and
#     bound nothing. What bounds a write now is real and is not a name in a list: `ChangeGuard`
#     stands at all three write paths with this file first in its Tier-1 set, a session STAGES
#     into a ChangeSet and applies nothing, `ApprovalGate` is a startup requirement for any
#     adapter that both spends and writes, and a person runs `apply`.
#
#   * `shell` is dropped because no tool of that name has ever existed here. A denylist of tool
#     names goes stale as the tool set grows — it kept denying a tool that was never real while
#     silently permitting `delete_file` and `run_script` the moment those shipped. That is the
#     argument for `ToolSet` being an allowlist, and this floor was the one place it was not
#     applied.
#
#   * `run_script` is NOT denied, and that took an argument rather than an omission. This
#     repository binds `UnsandboxedEgress` sixty lines below, and ticket text is
#     UNTRUSTED_EXTERNAL by construction — so a model-chosen command running on this host would
#     be exactly the outbound channel that opt-out's comment says it cannot afford.
#
#     What makes it allowable is that the command does not run on this host. `Sandbox` runs it
#     under docker or podman with `--network=none`, `--cap-drop=ALL` and no credential in the
#     environment, and `require_container` makes the absence of a runtime a REFUSAL rather than
#     a silent fall back to the host. That container is a per-command egress constraint, which
#     is the thing `UnsandboxedEgress` gave up — narrower than a firewall, and not nothing.
#
#     It is therefore a control that depends on a container runtime being present. `doctor` can
#     see one; if yours cannot, deny `run_script` here.
lockstep.contribute(
    Policy(
        name="framework-floor",
        source="in-lockstep",
        # A ceiling, not a target, and a runaway backstop rather than the budget — the budget is
        # below, and it is what actually stops a long session, because it is checked against a
        # projection before each turn. 12 was sized against a reviewer that takes exactly one
        # turn; an implementing session spends several before it has done anything wrong.
        max_turns=30,
        scan_input="warn",
        deny_tools=(),
    )
)

# -- egress -------------------------------------------------------------------------
#
# THIS IS AN OPT-OUT FROM A CONTROL, and it is here rather than in an options object because the
# design's answer to "you cannot stop a repository weakening its own controls" is that weakening
# one has to be a legible line in a diff. This is that line.
#
# What it switches off: a review reads a diff authored by whoever opened the change, so the
# context package carries UNTRUSTED_EXTERNAL content, and egress enforcement is mandatory for it.
# Development happens on laptops with open internet, where no enforcement exists and the probe
# would refuse an asserted one — correctly. The alternative is running every local review inside
# a constrained container, which is a real answer and not the one this repository has chosen.
#
# What it costs: an injection that succeeds has somewhere to send what it read. `injection.py`'s
# own docstring is blunt about this — a scanner "is not a substitute for tool deny-lists, egress
# rules. Those are what actually bound a successful injection." The compensating controls here are
# that the review ToolSet is empty, that the one shipped tool which could carry bytes outward —
# `run_script` — executes inside a container with `--network=none` and refuses rather than falling
# back to this host, and that `ChangeGuard` stands at all three write paths.
#
# The container IS an egress rule, for the one process that could use one. It is narrower than the
# firewall this binding gave up, which covered the whole run rather than one child process.
#
# The floor above used to deny `write_file` instead, which had it backwards: a staged write cannot
# transmit anything, and a process on a host with an open network can.
#
# CI TAKES THIS PATH TOO, and this comment used to claim otherwise. It said "CI does not take this
# path: the workflow sets IN_LOCKSTEP_EGRESS=enforced, and the probe there has to pass." That is
# false, and provably so: the CLI resolves a bound `EgressPolicy` before it falls back to
# `detect()`, so this binding wins and the environment variable is never read. The workflow's
# `IN_LOCKSTEP_EGRESS: enforced` has no effect at all while this line exists.
#
# It is left as it is rather than made conditional on CI, because the conditional version would
# simply fail: a GitHub runner can reach `example.com`, so the probe would refuse every run, and
# the repository would be choosing between a broken pipeline and a false comment. Egress here is
# unenforced everywhere, and that is now written down where somebody reading the binding will see
# it instead of a claim that reassures them.
egress = UnsandboxedEgress()
lockstep.bind(EgressPolicy, egress)

# -- budgets ------------------------------------------------------------------------
#
# Per-run only. The per-agent-per-day ceiling the old substrate enforced before a run started has
# no in-process equivalent — see docs/controls-crosswalk.md, entry 3. The replacement is a
# provider-side organisation limit, which `doctor` can ask about but cannot verify.
#
# $0.25 rather than $2.00, sized against a measurement instead of a guess. A security review of a
# median diff costs about $0.02 and the pre-flight estimate for one is about $0.07 — the estimate
# bounds output by `max_tokens` rather than by an expected value, so it is deliberately the larger
# number. That leaves roughly a threefold margin over the estimate and tenfold over the actual
# spend, which is what a ceiling wants: far enough above normal that it never fires on a good run,
# close enough that a loop going wrong is stopped in cents rather than dollars.
#
# $2.00 was a hundredfold headroom, which is not a ceiling so much as a formality.
lockstep.budget = Budget(usd=0.25, wall_seconds=900)

# -- the ports those workflows resolve ----------------------------------------------
#
# Bound here rather than constructed inside the workflows, so `in-lockstep ls` can print what will
# actually run and a test can substitute either one without touching the process.
lockstep.bind(TicketSource, GitHubIssues())
lockstep.bind(Scm, GitHubScm())

# -- models and strategies ----------------------------------------------------------
lockstep.models.route("review", "anthropic:claude-sonnet-4-6")
lockstep.models.route("implement", "anthropic:claude-opus-4-6")
lockstep.models.route("triage", "local:qwen3-8b")

strategies = default_registry()

# -- cost ---------------------------------------------------------------------------
#
# An unpriced model is refused before the call rather than priced at some default. A defaulted
# rate is wrong in both directions and produces a number that looks like evidence.
costs = default_table()

# -- the implementing verb ----------------------------------------------------------
#
# Bound here, not by the CLI. `in-lockstep implement` binds a default when a repository has said
# nothing; this repository has said something, and what it says is visible in `in-lockstep ls`.
#
# `run_script` executes in a container with `--network=none` and refuses rather than falling back
# to this host — which is the per-command egress constraint that makes allowing the tool at all
# defensible under the `UnsandboxedEgress` binding above.
lockstep.bind(
    Implement,
    AiImplement(
        invoker_factory(lockstep.models.routes.get("implement", ""), egress=egress),
        registry=strategies,
        repo_root=lockstep.repo.root,
        commands=Sandbox(image="docker.io/library/python:3.12-slim", require_container=True),
        policy=InvokePolicy.under(
            lockstep.policy.resolve(), max_turns=30, max_tokens=8192, deadline_seconds=1800
        ),
    ),
)


# -- middleware ---------------------------------------------------------------------
#
# Redaction, egress policy and the kill switch are NOT here. They are privileged: they run outside
# this chain, so `--no-middleware` cannot reach them.
lockstep.middleware += [
    otel(),
    CostBudget(usd=2.00),
    # An adapter that both spends money and writes files needs an approval path, or
    # `Lockstep.context` refuses to start the run. No predicate: the grant arrives on the run
    # context from `--approve` or `--approved-by`, which is what lets the SAME command serve a
    # developer at a terminal today and a verified `/implement` comment once this repository
    # moves to hosted triggers. A gate configured differently for those two would make the
    # transition a rewrite.
    ApprovalGate(),
]


# -- what a `/implement` comment actually does --------------------------------------------------
#
# THIS is the process, and it is here rather than in `.github/workflows/implement.yml` because
# that is the whole claim of this framework: the lifecycle is Python you can read, test and run,
# and a CI file is a trigger that invokes it. The first draft of that workflow had forty-five
# lines of shell in it — branching on whether anything was staged, composing a commit message,
# picking between three issue comments — which is lifecycle logic living in the one file nothing
# type-checks and nothing tests.
#
# What legitimately stays in YAML is the trigger, the job split, and which credential each job
# holds. Those belong to the CI system because no amount of Python makes one process hold two
# different token scopes, and keeping an API key out of the job that can write is the reason there
# are two jobs at all.
#
# Two workflows rather than one, for that same reason. `implement/from-issue` runs unprivileged
# with the provider key and emits an artifact; `implement/propose` runs privileged with a write
# token and no provider key. They cannot be one function, because they must not be one process.

#: Where the unprivileged half leaves its answer for the privileged half to pick up.
CHANGESET = "changeset"


@workflow(id="implement/from-issue")
async def implement_from_issue(ctx: Any, issue: str, actor: str = "") -> Outcome:
    """Read the issue, implement it, leave the change staged in an artifact.

    Writes nothing. The change set travels to the job that holds a write token, and crosses the
    guard again when it gets there.
    """
    tickets: TicketSource = ctx.container.resolve(TicketSource)
    ticket = await tickets.get(issue)

    # `--approved-by` in CLI terms: a named human asked for this specific run, and the actor gate
    # verified them before this job started. Recorded, because a grant nobody can be traced to is
    # not much of a grant.
    outcome = await ctx.do(Implement, ImplementSpec(ticket=ticket))

    report = outcome.value
    if report is not None and report.changeset.changes:
        written = write_changeset(CHANGESET, report.changeset)
        print(f"staged    {len(report.changeset.changes)} change(s) -> {written}")
    return outcome


@workflow(id="implement/propose")
async def implement_propose(ctx: Any, issue: str, artifact: str = CHANGESET) -> Outcome:
    """Open a change from a staged artifact, and say on the issue what happened.

    Runs in the job that holds a write token and no provider credential. Everything it reads came
    from another job, so none of it is trusted: `Scm.open_change` runs `ChangeGuard` over the set
    before it writes a byte, and refuses any branch outside the run-scoped prefix.
    """
    tickets: TicketSource = ctx.container.resolve(TicketSource)
    scm: Scm = ctx.container.resolve(Scm)
    changeset = read_changeset(artifact)

    if not changeset.changes:
        # Still a comment. A trigger that answers only on success leaves somebody watching a
        # thread that never got a reply, and "it found nothing to change" is an answer.
        await tickets.comment(await tickets.get(issue), "`/implement` staged no change.")
        return Outcome(status=Status.FAILED, reason="implement.no_changes")

    change = await scm.open_change(
        changeset,
        title=changeset.summary or f"Implement {issue}",
        body=(
            "Written by `implement/oneshot` and read by nobody. The issue body is untrusted input "
            "to a model that held write tools, so review this as you would a change from a "
            "stranger who had read your repository — the controls bound where it could write, "
            "not what it thought."
        ),
        ticket=issue,
        workflow="implement",
        run_id=ctx.run_id,
    )
    await tickets.comment(
        await tickets.get(issue),
        f"`/implement` opened {change.url or change.branch}. Nobody has read it yet.",
    )
    print(f"change    {change.url or change.branch}")
    return Outcome(status=Status.SUCCEEDED, value=change)
