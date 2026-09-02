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

from in_lockstep import Lockstep, RunContext, Workshop
from in_lockstep.adapters import PytestTest, RuffValidate
from in_lockstep.adapters.ai import TDD, DiagnoseThenFix, Fix, Implement
from in_lockstep.adapters.pytest_adapter import Test
from in_lockstep.adapters.ruff_adapter import Validate
from in_lockstep.adapters.sandbox import Sandbox
from in_lockstep.adapters.worktree import verdict_over_staged
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.policy import Policy
from in_lockstep.core.spend import Budget
from in_lockstep.core.workflow import workflow
from in_lockstep.middleware import CostBudget, otel
from in_lockstep.middleware.approval import ApprovalGate
from in_lockstep.platform.artifacts import read_changeset, read_verdict, write_changeset
from in_lockstep.platform.conversation import ticket_for, with_review
from in_lockstep.platform.propose import escalate, open_reviewable
from in_lockstep.platform.report import fix_body, implement_body
from in_lockstep.platform.scm import GitHubScm, Scm
from in_lockstep.platform.tickets import GitHubIssues, TicketSource
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress

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
        #
        # This number alone does not raise anything. `InvokePolicy.under` takes the LOWEST of this
        # and the workshop's `max_turns`, because a contributed floor may only tighten — so a run
        # gets 100 turns only once the workshop below says 100 too. Raising this one on its own
        # is a line that reads like a change and is not one.
        max_turns=100,
        # No `max_tokens` here, and it is not an omission. `Policy` is the tighten-only stack an
        # ORG contributes to, and every field on it merges by `min` across contributions — which
        # is the right semantics for a ceiling on the whole run and the wrong semantics for the
        # per-request output cap, where the lowest of two upstreams would silently truncate the
        # answer rather than refuse. That cap is a property of the workshop, so it lives there.
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
# The numbers were sized against a single-turn reviewer: a security review of a median diff costs
# about $0.02, its pre-flight estimate about $0.07, and $0.25 left a threefold margin over the
# estimate. That is the right shape of ceiling for a verb that makes one call, and the wrong one
# for a verb that explores — a TDD implement runs two model phases of many turns each, re-sending
# its accumulated messages every turn, so cost is quadratic in turns rather than linear. #139 was
# refused at the ceiling rather than at anything it did wrong.
#
# So: $100 and half an hour, which is a ceiling for a session and not for a call. It still fires
# on a loop going wrong; it no longer fires on the work.
#
# DOLLARS BIND FIRST, and the turn cap below is nowhere near reachable. This is measured, not
# guessed: run 33564844360 spent $23.07 over nine turns in 286 seconds before turn 10's projection
# crossed its ceiling. Cost per turn RISES as the loop goes — the accumulated message list is
# re-sent every turn, so spend is quadratic in turns rather than linear — which puts $100 at
# roughly turn 25 and roughly twenty minutes, short of both the 1800-second wall and the 100 turns
# the workshop grants.
#
# So `max_turns=100` below is a runaway backstop and not a promise of 100 turns. If a session
# needs to explore further than it currently gets, THIS is the number that buys it; raising the
# turn cap alone buys nothing, because it is not what stops the run.
#
# `turns` and `tokens` are NOT set, and that is the deliberate half of this line.
#
# `Spend` is run-scoped and accumulates across every invocation in the run, so a `turns` ceiling
# equal to the per-invocation cap lets the red phase spend all of it and leaves the green phase
# nothing — a run that stops halfway and reports a budget refusal for work it was never given
# room to do. And `tokens` here is the run total, not the per-request output cap that shares the
# name on `Workshop`: 20000 would be under one turn's output, so it would refuse every run
# immediately while reading like generous headroom.
#
# `usd` is the ceiling that bounds all of it, and it is the one checked against a projection
# before each turn.
lockstep.budget = Budget(usd=100.00, wall_seconds=1800)

# -- the ports those workflows receive ----------------------------------------------
#
# Bound here rather than constructed inside the workflows: a workflow names what it needs in its
# signature (`tickets: TicketSource`) and the dispatcher fills it from these bindings — so
# `in-lockstep ls` can print what will actually run, and a test can substitute either one by
# passing its own or rebinding, without touching the process.
lockstep.bind(TicketSource, GitHubIssues())
lockstep.bind(Scm, GitHubScm())

# -- models -------------------------------------------------------------------------
#
# Routes are all an AI adapter needs: an adapter bound with no explicit invoker resolves its
# model from these lines (snapshotted onto the run context), and egress from the binding above.
lockstep.models.route("review", "anthropic:claude-sonnet-4-6")
lockstep.models.route("implement", "anthropic:claude-opus-4-6")
lockstep.models.route("triage", "local:qwen3-8b")
# Sonnet, not the Opus that implements. A fix is a bounded task — reproduce one reported bug, make
# the reproducer pass — and it is the verb the loop retries, so the cheaper model is the one that
# should be running three times rather than once.
lockstep.models.route("fix", "anthropic:claude-sonnet-4-6")

# -- the workshop -------------------------------------------------------------------
#
# What every AI strategy below is completed from, declared once. It used to be typed once per verb,
# and the two paragraphs were identical apart from the class name — which meant two chances to drop
# the `WorktreeRunner` wrap or the `InvokePolicy.under(...)`, neither of which is optional in
# spirit and neither of which shows up in `ls` when it is missing.
#
# `run_script` executes in this container with `--network=none` and refuses rather than falling
# back to the host — the per-command egress constraint that makes allowing the tool at all
# defensible under the `UnsandboxedEgress` binding above. `use()` wraps it in a `WorktreeRunner`,
# so what the container bind-mounts read-write is a throwaway worktree of HEAD rather than the live
# tree: without the wrap, a model's command could write `.git/hooks` or this very file past
# ChangeGuard.
# The two ceilings on the loop itself live here, and this is the only place raising them has an
# effect: `use()` completes every strategy below from this object, and `InvokePolicy.under` takes
# `min(workshop.max_turns, policy.max_turns)`. The policy floor above is the *upper bound on what
# this may ask for*, never the grant.
#
# 100 turns rather than the shipped 30. 30 was sized against a reviewer, and an implementing
# session spends turns reading before it has done anything at all; #139 spent $21 and never got
# past exploring. Both phases of TDD get 100 each — the cap is per invocation, and the run total
# is bounded by `lockstep.budget` above, which is the ceiling that should be doing that job.
#
# 20000 output tokens rather than 8192, because a diff plus its reasoning does not fit in 8192 and
# a truncated turn is charged in full and then retried. Raising it also raises the pre-flight
# estimate, which bounds output by this number rather than by an expected value — that is the
# intended coupling, not a side effect: asking for more room means the projection reserves more.
lockstep.workshop = Workshop(
    commands=Sandbox(image="docker.io/library/python:3.12-slim", require_container=True),
    max_turns=100,
    max_tokens=20000,
)


# -- the implementing verb ----------------------------------------------------------
#
# Bound here, not by the CLI. `in-lockstep implement` binds a default when a repository has said
# nothing; this repository has said something, and what it says is one line of `in-lockstep ls`:
# `Implement -> TDD`. The strategy IS the adapter — `TDD` rather than the cheaper `Oneshot`
# because red then green, with the Test verb bound above confirming both, is a choice this
# repository makes deliberately at the cost of a second model phase. The model comes from the
# `models.route("implement", ...)` line above and egress from the bound `EgressPolicy`, so no
# invoker is threaded here.
#
# `run_script` executes in a container with `--network=none` and refuses rather than falling back
# to this host — which is the per-command egress constraint that makes allowing the tool at all
# defensible under the `UnsandboxedEgress` binding above. Wrapped in `WorktreeRunner`, so what the
# container bind-mounts read-write is a throwaway worktree of HEAD, not the live tree: without the
# wrap, a model's command could write `.git/hooks` or this very file past ChangeGuard.
tdd = lockstep.use(TDD)


# -- the fixing verb ----------------------------------------------------------------
#
# `Fix -> DiagnoseThenFix` in `in-lockstep ls`. Same sandbox and the same policy as the
# implementing verb, because it holds the same tools: it writes a reproducer, runs it to watch it
# fail, writes the fix, and runs it again to watch it pass. Two phases rather than one, and both
# inside a throwaway worktree in a `--network=none` container.
#
# What it does NOT share with implement is the model — see the `fix` route above — and what it
# does not share with either is a way to reach the repository: like every writing verb here it
# stages into a ChangeSet, and the privileged half opens the change.
fix = lockstep.use(DiagnoseThenFix)


# -- middleware ---------------------------------------------------------------------
#
# Redaction, egress policy and the kill switch are NOT here. They are privileged: they run outside
# this chain, so `--no-middleware` cannot reach them.
lockstep.middleware += [
    otel(),
    # No ceiling of its own, deliberately. `Budget.merge` takes the LOWEST of every declared
    # ceiling — tighten-only, like the policy stack — so the `usd=2.00` this used to carry kept
    # capping every run at $2 no matter what `lockstep.budget` or `run --budget` said: three
    # /implement attempts raised the other two numbers and were refused at 2.0000 anyway. One
    # number, declared once (`lockstep.budget` above, or the flag), is the fix; a bare CostBudget
    # still does its real job — the post-action reconciliation that stops a run whose actual
    # spend drifted past the estimate.
    CostBudget(),
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
# Two workflows rather than one, for that same reason. `implement/from-ticket` runs unprivileged
# with the provider key and emits an artifact; `implement/propose` runs privileged with a write
# token and no provider key. They cannot be one function, because they must not be one process.

#: Where the unprivileged half leaves its answer for the privileged half to pick up.
CHANGESET = "changeset"


@workflow(id="implement/from-ticket")
async def implement_from_ticket(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, actor: str = ""
) -> Outcome:
    """Read the ticket and the review of the last attempt, implement it, leave it staged.

    `tickets` and `scm` arrive from the bindings above — the signature names the ports, the
    dispatcher fills them. Writes nothing. The change set travels to the job that holds a write
    token, and crosses the guard again when it gets there.

    `with_review` is what makes a second `/implement` a reply rather than a retry: it gathers what
    people said on the open pull request this workflow opened last time — including the notes
    pinned to a line, which are the most specific thing a reviewer ever writes — and hands them
    over on the ticket, untrusted like the ticket body.
    """
    # `--approved-by` in CLI terms: a named human asked for this specific run, and the actor gate
    # verified them before this job started. Recorded, because a grant nobody can be traced to is
    # not much of a grant.
    #
    # `via=tdd` says at the execution site what serves this request — the same adapter the module
    # binds above, named here so the reader of this line knows Implement means red-then-green
    # without scrolling to the binding.
    key, where = await ticket_for(ticket, scm)
    print(where)
    source, note = await with_review(await tickets.get(key), scm)
    print(note)
    outcome = await ctx.do(Implement(ticket=source), via=tdd)

    # `SUCCEEDED` and not merely "there are changes", which is what this used to check — and the
    # difference is a real run that cost $21 and would have opened a pull request containing a
    # test that tested nothing. A test-first strategy that refuses in its red phase still returns
    # the test it staged, so `changeset.changes` is truthy on precisely the outcome that must not
    # travel. The fixing verb's own half has always guarded on the status; this now matches it.
    report = outcome.value
    if outcome.status is Status.SUCCEEDED and report is not None and report.changeset.changes:
        # The suite, run against a throwaway worktree of HEAD plus the staged change, before any
        # of it travels. The verdict rides the artifact so the privileged half can decide what to
        # open — a reviewer should learn whether the change passed from the pull request, not by
        # waiting for CI on a branch a model wrote.
        verdict = await verdict_over_staged(ctx, lockstep.repo.root, report.changeset)
        written = write_changeset(CHANGESET, report.changeset, verdict=verdict)
        print(f"staged    {len(report.changeset.changes)} change(s) -> {written}")
    return outcome


@workflow(id="implement/propose")
async def implement_propose(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, artifact: str = CHANGESET
) -> Outcome:
    """Open a change from a staged artifact, and say on the ticket what happened.

    Runs in the job that holds a write token and no provider credential. Everything it reads came
    from another job, so none of it is trusted: `Scm.open_change` runs `ChangeGuard` over the set
    before it writes a byte, and refuses any branch outside the run-scoped prefix.
    """
    # The same resolution the unprivileged half did, run again rather than threaded between
    # jobs: both halves are handed the number the comment was left on, and a fact both can
    # derive is not one to carry across an artifact boundary where it would arrive untrusted.
    ticket, where = await ticket_for(ticket, scm)
    print(where)
    changeset = read_changeset(artifact)
    verdict = read_verdict(artifact)

    if not changeset.changes:
        # Still a comment. A trigger that answers only on success leaves somebody watching a
        # thread that never got a reply, and "it found nothing to change" is an answer.
        await tickets.comment(await tickets.get(ticket), "`/implement` staged no change.")
        return Outcome(status=Status.FAILED, reason="implement.no_changes")

    if verdict is not None and verdict.red:
        # `red`, not `not green`: an errored suite — the runner never started — is not evidence
        # that this change is broken, and escalating on it files a bug report about code nobody
        # tested and then spends the loop's attempts on it. A change whose tests actually RAN and
        # failed does not become a pull request; it becomes the next `ai-generated` ticket, which
        # the label trigger routes to the fixing verb — and because `escalate` counts attempts off
        # the source ticket's labels, the loop stops at `lockstep.max_attempts` without any store
        # to keep count in.
        failure = f"Tests failed: {verdict.failed} of {verdict.total} against the staged change."
        opened = await escalate(
            tickets, await tickets.get(ticket), failure, max_attempts=lockstep.max_attempts
        )
        reason = "implement.tests_failed" if opened is not None else "implement.attempts_exhausted"
        if opened is not None:
            print(f"escalated {opened.key}")
        return Outcome(status=Status.FAILED, reason=reason, value=opened)

    # Draft unless the suite went green. An unverified change — no verdict at all, because nothing
    # was staged to run against or the Test verb refused — is not a failure, but it has not earned
    # a place in somebody's review queue either.
    ready = verdict is not None and verdict.green
    # Fetched before the change is opened, because the title comes from it now.
    issue = await tickets.get(ticket)
    change = await open_reviewable(
        scm,
        changeset,
        ready=ready,
        # The ticket's own title, not the model's `summary`. A summary is free prose: run
        # 33578430422 put a thousand characters of the model's running commentary here, and the
        # host refused the pull request after the work was done and green. The issue title is a
        # person's one-line statement of the same thing, which is what a title wants.
        title=issue.title or changeset.summary or f"Implement {ticket}",
        body=implement_body(changeset, verdict),
        ticket=ticket,
        workflow="implement",
        run_id=ctx.run_id,
    )
    await tickets.comment(
        issue,
        f"`/implement` opened {change.url or change.branch} as "
        f"{'ready for review' if ready else 'a draft — its tests have not passed'}. "
        "Nobody has read it yet.",
    )
    print(f"change    {change.url or change.branch}")
    return Outcome(status=Status.SUCCEEDED, value=change)


# -- what a `/fix` comment, or an `ai-generated` label, actually does ----------------------------
#
# The same two-half shape as implement, and the same reason for it: the half that reads a bug
# report and runs a model holds the provider key, the half that opens a change holds the write
# token, and they are separate processes because a credential split cannot be expressed inside one.
#
# What is different is where the work comes from. `/fix` is a person asking. The `ai-generated`
# label is this loop asking itself: `implement/propose` above files such a ticket when a staged
# change fails its tests, and `.github/workflows/ai-generated.yml` routes it straight back here.
# The label is write-gated, so it is the authorization — which is why that trampoline has no gate
# job and the `/fix` comment one does.

#: Where the unprivileged half leaves the fix for the privileged half to open.
FIX_CHANGESET = "fix-changeset"


@workflow(id="implement/report")
async def implement_report(ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm) -> Outcome:
    """Say on the ticket that the run failed, when the half that would have said so never ran.

    `implement/propose` answers on every outcome it sees — a change opened, no change staged, tests
    failed. It only sees the outcomes that reach it, and a strategy refusing in its first phase
    never gets there: the work job exits non-zero, `needs:` skips propose, and the person who typed
    `/implement` is left watching a thread that never replies.

    Which is the one failure a chat-ops trigger cannot afford. The alternative to an answer is not
    "no answer" — it is somebody assuming it worked, because the last thing the tool said was that
    it had started.

    Reads the record the run already wrote rather than being handed a reason by the CI file: the
    reason, the cost and the findings are all in the ledger, and a workflow that took them as
    arguments would be a workflow whose YAML had to know what happened.
    """
    key, where = await ticket_for(ticket, scm)
    print(where)
    source = await tickets.get(key)
    record = _last_unsuccessful(key)

    if record is None:
        body = (
            "`/implement` failed before it recorded anything. Nothing was staged and nothing was "
            "opened; the job log is the only account of it."
        )
    else:
        reason = str(record.get("reason") or record.get("status") or "failed")
        cost = record.get("cost_usd")
        spent = f" ${float(cost):.2f} spent." if isinstance(cost, (int, float)) else ""
        findings = [
            f"- `{f.get('id')}`: {f.get('message')}"
            for f in (record.get("findings") or {}).get("items", [])[:5]
            if isinstance(f, dict)
        ]
        detail = ("\n\n" + "\n".join(findings)) if findings else ""
        body = (
            f"`/implement` did not produce a change — the run failed with `{reason}`.{spent} "
            f"Nothing was staged and no pull request was opened.{detail}"
        )

    await tickets.comment(source, body)
    print(f"commented {key}")
    # SUCCEEDED: this job's job was to say what happened, and it did. Failing here would put a
    # second red mark on a run whose failure is already recorded, and hide whether the answer
    # actually reached the ticket.
    return Outcome(status=Status.SUCCEEDED, reason=None)


def _last_unsuccessful(ticket: str) -> dict | None:
    """The newest recorded run for this ticket that did not succeed.

    Matched on the `ticket` the record carries rather than on the run id, because a run id is a
    string a person would have to parse and the field exists for exactly this.
    """
    from in_lockstep.platform.ledger import store_for

    store = store_for(lockstep.container)
    reader = getattr(store, "records", None)
    if reader is None:
        return None
    wanted = {ticket, ticket.lstrip("#"), "#" + ticket.lstrip("#")}
    mine = [
        r
        for r in reader()
        if str((r.get("args") or {}).get("ticket", r.get("ticket", ""))) in wanted
        and r.get("status") != "succeeded"
    ]
    mine.sort(key=lambda r: str(r.get("ts", "")))
    return mine[-1] if mine else None


@workflow(id="fix/from-ticket")
async def fix_from_ticket(ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm) -> Outcome:
    """Read the bug and the review of the last attempt, reproduce it, fix it, leave it staged.

    Writes nothing to the tree. A fix that did not go green stages nothing — a broken fix must not
    travel — and the propose half says so on the ticket rather than opening a pull request.

    `with_review` gathers what people said on the open pull request this workflow opened last time,
    so replying to a reviewer is running the verb again rather than explaining yourself twice.
    """
    key, where = await ticket_for(ticket, scm)
    print(where)
    source, note = await with_review(await tickets.get(key), scm)
    print(note)
    outcome = await ctx.do(Fix(ticket=source), via=fix)

    report = outcome.value
    if outcome.status is Status.SUCCEEDED and report is not None and not report.empty:
        # `report.changeset` is the reproducer and the fix merged. They are kept apart inside the
        # report so a reader can see which is which; what gets applied is both.
        #
        # Then the whole suite, against a throwaway worktree of HEAD plus that change. The
        # strategy has already proved the reproducer goes red and then green — but that is a fact
        # about the bug, not about the rest of the repository, and the two can disagree. The first
        # fix this loop ever produced passed its own reproducer and broke a test elsewhere; it was
        # proposed as ready for review on the strength of the half that passed.
        verdict = await verdict_over_staged(ctx, lockstep.repo.root, report.changeset)
        written = write_changeset(FIX_CHANGESET, report.changeset, verdict=verdict)
        print(f"staged    reproducer + fix -> {written}")
    return outcome


@workflow(id="fix/propose")
async def fix_propose(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, artifact: str = FIX_CHANGESET
) -> Outcome:
    """Open the verified fix from the staged artifact, and say on the ticket what happened.

    Runs in the job that holds a write token and no provider credential. What it reads came from
    another job, so none of it is trusted: `Scm.open_change` runs `ChangeGuard` over the set before
    it writes a byte, and refuses any branch outside the run-scoped prefix.
    """
    # The same resolution the unprivileged half did, run again rather than threaded between
    # jobs: both halves are handed the number the comment was left on, and a fact both can
    # derive is not one to carry across an artifact boundary where it would arrive untrusted.
    ticket, where = await ticket_for(ticket, scm)
    print(where)
    changeset = read_changeset(artifact)
    verdict = read_verdict(artifact)

    if not changeset.changes:
        # An empty artifact means the fix failed: `fix/from-ticket` stages only when its reproducer
        # went red and then green. Open the next `ai-generated` ticket for another attempt rather
        # than leaving the bug with nothing said — bounded by the same cap implement escalates on.
        failure = "The automated fix did not reproduce the bug and turn it green."
        opened = await escalate(
            tickets, await tickets.get(ticket), failure, max_attempts=lockstep.max_attempts
        )
        reason = "fix.not_fixed" if opened is not None else "fix.attempts_exhausted"
        if opened is not None:
            print(f"escalated {opened.key}")
        return Outcome(status=Status.FAILED, reason=reason, value=opened)

    if verdict is not None and verdict.red:
        # A fix that made its own reproducer pass and broke something else is still a failure, and
        # it used to be the one failure this verb could not see: it opened ready for review on the
        # strength of the reproducer alone. Same escalation implement makes, for the same reason —
        # the suite ran and disagreed, so another attempt is the honest next move.
        failure = (
            f"The fix passed its reproducer but the suite went red: "
            f"{verdict.failed} of {verdict.total} failed."
        )
        opened = await escalate(
            tickets, await tickets.get(ticket), failure, max_attempts=lockstep.max_attempts
        )
        reason = "fix.suite_red" if opened is not None else "fix.attempts_exhausted"
        if opened is not None:
            print(f"escalated {opened.key}")
        return Outcome(status=Status.FAILED, reason=reason, value=opened)

    # Ready only when the whole suite agrees with the reproducer. Without a verdict — no Test verb
    # bound, or a runner that never started — this opens a draft: the reproducer passing is a fact
    # about the bug, and nobody has checked the rest of the repository.
    ready = verdict is not None and verdict.green
    change = await open_reviewable(
        scm,
        changeset,
        ready=ready,
        title=changeset.summary or f"Fix {ticket}",
        body=fix_body(changeset, verdict),
        ticket=ticket,
        workflow="fix",
        run_id=ctx.run_id,
    )
    await tickets.comment(
        issue,
        f"`/fix` opened {change.url or change.branch} as "
        f"{'ready for review' if ready else 'a draft — the suite has not confirmed it'}. "
        "Nobody has read it yet.",
    )
    print(f"change    {change.url or change.branch}")
    return Outcome(status=Status.SUCCEEDED, value=change)


@workflow(id="fix/report")
async def fix_report(ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm) -> Outcome:
    """Say on the ticket that the run failed, when the half that would have said so never ran.

    `fix/propose` answers on every outcome it sees — a change opened, no change staged, tests
    failed. It only sees the outcomes that reach it, and a strategy refusing in its first phase
    never gets there: the work job exits non-zero, `needs:` skips propose, and the person who typed
    `/fix` is left watching a thread that never replies.

    Which is the one failure a chat-ops trigger cannot afford. The alternative to an answer is not
    "no answer" — it is somebody assuming it worked, because the last thing the tool said was that
    it had started.

    Reads the record the run already wrote rather than being handed a reason by the CI file: the
    reason, the cost and the findings are all in the ledger, and a workflow that took them as
    arguments would be a workflow whose YAML had to know what happened.
    """
    key, where = await ticket_for(ticket, scm)
    print(where)
    source = await tickets.get(key)
    record = _last_unsuccessful(key)

    if record is None:
        body = (
            "`/fix` failed before it recorded anything. Nothing was staged and nothing was "
            "opened; the job log is the only account of it."
        )
    else:
        reason = str(record.get("reason") or record.get("status") or "failed")
        cost = record.get("cost_usd")
        spent = f" ${float(cost):.2f} spent." if isinstance(cost, (int, float)) else ""
        findings = [
            f"- `{f.get('id')}`: {f.get('message')}"
            for f in (record.get("findings") or {}).get("items", [])[:5]
            if isinstance(f, dict)
        ]
        detail = ("\n\n" + "\n".join(findings)) if findings else ""
        body = (
            f"`/fix` did not produce a change — the run failed with `{reason}`.{spent} "
            f"Nothing was staged and no pull request was opened.{detail}"
        )

    await tickets.comment(source, body)
    print(f"commented {key}")
    # SUCCEEDED: this job's job was to say what happened, and it did. Failing here would put a
    # second red mark on a run whose failure is already recorded, and hide whether the answer
    # actually reached the ticket.
    return Outcome(status=Status.SUCCEEDED, reason=None)


def _last_unsuccessful(ticket: str) -> dict | None:
    """The newest recorded run for this ticket that did not succeed.

    Matched on the `ticket` the record carries rather than on the run id, because a run id is a
    string a person would have to parse and the field exists for exactly this.
    """
    from in_lockstep.platform.ledger import store_for

    store = store_for(lockstep.container)
    reader = getattr(store, "records", None)
    if reader is None:
        return None
    wanted = {ticket, ticket.lstrip("#"), "#" + ticket.lstrip("#")}
    mine = [
        r
        for r in reader()
        if str((r.get("args") or {}).get("ticket", r.get("ticket", ""))) in wanted
        and r.get("status") != "succeeded"
    ]
    mine.sort(key=lambda r: str(r.get("ts", "")))
    return mine[-1] if mine else None
