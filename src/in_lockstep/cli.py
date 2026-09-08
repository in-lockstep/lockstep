"""The `in-lockstep` command: the composition root.

Wide on purpose. This module's `ALLOWED` entry reaches 18 of the other 20 packages, where the next
widest reaches 8, and that is what a composition root is for — it is the one place that may name
every implementation, because something has to. The layering gate enforces direction and
acyclicity, and neither of those bounds width, so this is also the one place where logic can
accumulate with nothing noticing (#239).

**What belongs here: parse, compose, render, translate.** Read the flags. Build the `Lockstep` and
the adapters a verb needs. Turn an `Outcome` into the lines a person reads and the exit code a
trampoline branches on. Turn a domain exception into a message rather than a traceback.

**What does not: deciding what a run means.** Identity and keying, what counts as a case, what a
census counts, what a workflow dispatches to — those are decisions the framework makes, and a
decision made here is one that cannot be reached from a workflow, tested without a CliRunner, or
reused by an adopter who never types `in-lockstep`.

The line is not always obvious and some of the calls that put logic here were right locally:
`_case_key` concedes in its own comment that it duplicates a hash `evaluation` may not import,
because doing it twice beats two implementations that drift. Eighteen right local calls is the
symptom rather than the defect, which is why the rule is a bound with an argument attached and not
a refactor. `test_layering.py::test_the_composition_root_does_not_grow_logic` is where it bites.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import click
from click.core import ParameterSource

from . import __version__
from .ai.prompt import BodyNotFound, Composition, Inspectable
from .ai.replay import CASSETTE_DIR
from .core.context import DISABLE_ENV, RunContext
from .core.human import PARKED_LABEL, Resumption
from .core.outcome import Status
from .core.types import Locatable, Test, Validate
from .core.verbs import SHIPPED_VERBS, Verb, verb_of
from .core.workflow import get, inject_ports, injectable_parameters, registered, workflow
from .lockstep import Lockstep
from .middleware.otel import Recorder, otel
from .platform.report import MARKER as _MARKER
from .privileged import sink
from .privileged.redact import Redact, redact_registry

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 3
#: A run that ended at a human boundary (§13): not failed, not blocked, waiting. Distinct so a
#: trampoline's `if` can tell "somebody has to act" from "something is wrong", and the run
#: prints the command that resumes it on the way out.
EXIT_PARKED = 4


def _default_lockstep() -> tuple[Lockstep, Recorder | None]:
    """A repository's own module if it has one, else detected defaults.

    The module is loaded from a trusted ref, never from the ref under review.

    The recorder comes back as `None` when the module declared its own middleware. The CLI cannot
    see through a chain it did not build, and printing `spans 0` for a run that emitted spans into
    somebody else's exporter is a wrong number rather than a missing one — which is the same
    reason `Outcome` carries `decided` instead of reporting an unjudged suite as passing.
    """
    from .config_ref import UnresolvableConfigRef, UntrustedConfig
    from .loader import NoLifecycle, load, lockstep_from
    from .platform.ci import detect as detect_ci

    # `ci.detect` rather than `GITHUB_*` directly: it answers base-ref and am-I-reviewing for
    # GitLab as well, and reading GitHub's variables here meant the trusted-ref control silently
    # failed to engage on a GitLab merge-request pipeline. `doctor._config_provenance` is the
    # check that notices; this is the load it protects, and both read the same detection.
    ci_env = detect_ci()

    try:
        module, ref = load(
            ".",
            base=ci_env.base_ref if ci_env else "",
            reviewing=ci_env.reviewing if ci_env else False,
        )
        configured = lockstep_from(module)
        # Detected defaults are NOT applied here. A repository that ships a module has made its
        # choices, and silently binding a detected adapter for a verb it left unbound would be a
        # surprise, not a convenience — the drop-in case detection serves is the repository with
        # no module at all, handled in the fallback below.
        # Said out loud. This is the control that replaces gh-aw's workflow-file provenance, and
        # it spent a CI run silently not applying — a bare `main` does not resolve in an
        # `actions/checkout` working directory, so configuration fell through to detected
        # defaults and nothing reported it. A provenance control nobody can see the output of is
        # one nobody notices the absence of.
        click.echo(f"config    {ref.reason}", err=True)
        configured.config_source = ref.reason
        if configured.middleware:
            return configured, None
        recorder = Recorder()
        # Telemetry only. The CLI does not invent a ceiling: a `CostBudget` supplied here would be
        # a budget nobody chose, and it would make GATE-BUDGET-1 unsatisfiable — every run would
        # arrive bounded by a number the repository never decided on. `init` scaffolds one, and
        # `--budget` states one; both of those are somebody meaning it.
        configured.middleware = [otel(recorder)]
        return configured, recorder
    except NoLifecycle as e:
        # Legitimate: a repository with no lockstep.py runs on detected defaults, and the first
        # pull request of any adopter is exactly that. Distinct from the ref being unreadable,
        # which is a refusal — the branch below.
        click.echo(f"config    none ({e})", err=True)
    except (UnresolvableConfigRef, UntrustedConfig) as e:
        # Both were written to be read, and neither was caught, so both arrived as an uncaught
        # exception and the remedy inside them reached nobody. `UnresolvableConfigRef` names the
        # cause and gives the exact fix for either host; on a shallow `actions/checkout` of a pull
        # request it is the first thing every command hits.
        #
        # A refusal, and deliberately NOT a fallback to detected defaults. Configuration comes from
        # the base ref precisely so a change under review cannot supply the file that constrains
        # reviewing it, and running on defaults instead would drop every binding, ceiling and policy
        # the repository declared — on the one code path where that matters most. That is the
        # degradation the exception's own docstring was written about.
        raise click.ClickException(str(e)) from None

    lockstep = Lockstep.detect()
    lockstep.config_source = "none (detected defaults)"
    # Bound from what the tree actually is, not from the assumption it is Python. A Node repository
    # used to get pytest bound here regardless, which broke its first run; now it gets `npm test`
    # if that is what detection found, and nothing for a verb detection could not place.
    _apply_detected_defaults(lockstep)
    recorder = Recorder()
    lockstep.middleware = [otel(recorder)]
    return lockstep, recorder


def _apply_detected_defaults(lockstep: Lockstep) -> None:
    """Install the bindings a repository's detected parts imply, at `Tier.DEFAULT` so any explicit
    bind in the module overrides them. Reads `lockstep.repo.facts`, which `Lockstep.detect`
    populated."""
    from .adapters import detected_bindings
    from .core.container import Tier

    for iface, impl in detected_bindings(lockstep.repo.facts):
        lockstep.bind(iface, impl, tier=Tier.DEFAULT)


def _bound_cost_table(lockstep: Lockstep) -> Any:
    """The repository's own `CostTable`, if the module bound one; else None.

    `default_table`'s docstring has always said a repository overrides shipped rates like any
    other binding. This is the line that makes it true for the two commands that spend money —
    without it, a bound table resolved for nothing and every run priced against the default.
    """
    from .ai.pricing import CostTable

    return lockstep.container.resolve(CostTable) if lockstep.container.has(CostTable) else None


# One turn is what the review lens needs: no tool runner ships, so there is nothing a second turn
# could do with a tool result. It is named rather than inlined because it is an adapter's own
# requirement, and a policy ceiling is a different thing that composes with it — see
# `InvokePolicy.under`, which does the composing for every field rather than just this one.
_REVIEW_TURNS = 1

# The output cap, sized against what this lens actually returns rather than left at the transport
# default of 16384. A review answers with a findings list — path, line, summary, detail, severity
# — at roughly 120 tokens each, so this is headroom for about thirty findings on one diff.
#
# It is a cost decision as much as a correctness one, and the asymmetry is worth stating. The
# pre-flight estimate bounds output by this number and not by an expected value, deliberately, so
# a turn returning its full allowance cannot overshoot a ceiling checked against an average. At
# 16384 that made the estimate $0.2566 for a diff whose real cost is nearer $0.02 — so a budget
# had to be sized about six times larger than the run needed, which teaches people to set loose
# ceilings. At 4096 the estimate is $0.0722.
#
# Erring low is the more expensive mistake, not the cheaper one: a truncated answer is paid for in
# full and yields nothing, where an over-large cap only inflates an estimate. `review.truncated`
# names that failure when it happens instead of letting it read as malformed JSON.
_REVIEW_MAX_TOKENS = 4096

# How many findings a record keeps. A ledger record is meant to stay readable and diffable, and a
# run that produced two hundred findings has a problem the two hundredth will not explain.
_LEDGER_MAX_FINDINGS = 50
#: Per step, under `steps`. Smaller, because the top-level list already carries the run's.
_LEDGER_MAX_STEP_FINDINGS = 20


def _approval(approve: bool, approved_by: str) -> Any:
    """One grant, two provenances.

    `--approve` is a person at a terminal: their identity is the shell's, and `attended` is what
    makes it meaningful. `--approved-by` is unattended, so the name IS the grant and has to be
    supplied. The same two flags exist on every command that can need one, because a process
    invoked one way locally and another way from CI is a process that has to be rewritten to make
    that transition rather than re-triggered.
    """
    import getpass

    from .core.context import Approval

    named = approved_by.strip()
    if named:
        return Approval(by=named, attended=False)
    if approve:
        try:
            who = getpass.getuser()
        except OSError:  # pragma: no cover - no passwd entry, which some CI images lack
            who = "local"
        return Approval(by=who, attended=True)
    return Approval()


def _context(lockstep: Lockstep, run_id: str, approval: Any = None, *, recording: Any = None) -> Any:
    """Start a run, turning a startup refusal into a message rather than a traceback.

    `UndeclaredBudget` lives in `core` and cannot inherit from `ClickException` — `core` imports
    nothing outward. Translating at this boundary is the same shape as every other refusal here:
    the exception carries the reason, the CLI decides how a person should see it.
    """
    from .core.spend import DailySpendExceeded, UndeclaredBudget
    from .core.verbs import UngatedAgency

    try:
        return lockstep.context(run_id=run_id, approval=approval, recording=recording)
    except (UndeclaredBudget, UngatedAgency) as e:
        raise click.ClickException(str(e)) from None
    except DailySpendExceeded as e:
        # BLOCKED, not failed: a policy refusing is §4.3's own category, and the exit code is how
        # a trampoline's `if` can tell "over the daily window" from "broken".
        click.echo(f"blocked   {e.reason}: {e}", err=True)
        raise SystemExit(EXIT_BLOCKED) from None


def _declare_zero_ceiling(lockstep: Lockstep) -> None:
    """A run that cannot spend says so, as a ceiling of zero.

    GATE-BUDGET-1 refuses a lifecycle that binds a spender and declares no ceiling, and for a
    real model call that refusal is the point. But `--offline` and `--dry-run` bind providers
    that declare `transmits = False`: no byte reaches a network and no cent reaches a bill, which
    is why the egress and residency checks already lift their triggers for them. The budget gate
    had no such scoping, so the first command a stranger ran after installing was refused for
    spend that could not happen (#174: five clean installs, five refusals, zero findings).

    A ceiling rather than an exemption, because the difference is enforced. "Absent is not zero"
    runs the other way too: zero is the true amount this run can spend, so stating it is a fact
    and not a budget the CLI invented, and `Spend` holds the run to it. A flag that lifted the
    gate would be trusted; a zero ceiling refuses a provider that transmitted anyway at its first
    projected dollar. A ceiling the module or `--budget` declared is left alone, since a replay
    under $2.00 spends nothing under it.
    """
    from .core.spend import Budget

    if not lockstep.declared_ceiling().declared:
        lockstep.budget = Budget(usd=0.0)


def _run_id(base: str) -> str:
    """One id per invocation: the base names what ran, the suffix stamps this particular run.

    These ids used to be fixed — `review-security`, `selfcheck-local` — so a routine re-run wrote
    to the SAME ledger path. On the orphan branch that read as tampering (`GitLedger.verify` is
    right to flag it: the second run silently replaced the first), and the replaced record's cost
    vanished from the daily spend window, so repeated runs under-counted. The stamp is wall-clock
    UTC to the second plus two random bytes for runs inside the same second; `history --explain`
    matches on prefix, so the base a person remembers still finds the record.
    """
    import secrets
    from datetime import UTC, datetime

    return f"{base}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


def _shipped_fixture() -> dict[str, Any] | None:
    """The recorded cassette and the diff it was recorded against, from package data.

    Shipped so `--offline` needs nothing recorded first. It is a real recording of a real model
    reviewing a real merged pull request, not an authored stand-in — which matters, because a
    hand-written cassette would be a fixture that looks like evidence.
    """
    import json as _json
    from importlib import resources

    try:
        root = resources.files("in_lockstep.cassettes")
        cassette, diff, manifest, recorded = (
            root / "review-security.json",
            root / "example.diff",
            root / "fixture.json",
            root / "request.json",
        )
        if not all(f.is_file() for f in (cassette, diff, manifest, recorded)):
            return None
        meta = _json.loads(manifest.read_text())
        with resources.as_file(cassette) as path:
            # base and head as well as the diff: the composed prompt names the range it is
            # reviewing, so the cassette key covers all three and a replay has to reproduce them.
            return {
                "cassette": path,
                "diff": diff.read_text(),
                "base": meta["base"],
                "head": meta["head"],
                "aspect": meta["aspect"],
                "model": meta["model"],
                "label": meta["label"],
                # The request the response was recorded against, shipped verbatim. A cassette
                # keeps only a hash of its request, which is enough to look one up and not enough
                # to explain a miss — so a prompt edit turned this fixture from evidence into an
                # unexplained crash for the one user who cannot re-record.
                "request": _json.loads(recorded.read_text())["request"],
            }
    except (ModuleNotFoundError, FileNotFoundError, KeyError):  # pragma: no cover - packaging
        return None


def _say_drift(composed: Any, recorded: Any) -> None:
    """Say that the shipped recording was made against a different prompt than this run composed.

    The demo prints a real model's real findings, and a reader will take them for what this
    repository would get today. That is true only while the two prompts agree. Naming the parts
    that differ costs one line and is the whole difference between a recording and a stand-in —
    the drift is usually the framework's own prompts moving, but a repository that has added a
    guardrail sees it too, and for that reader the note is the only honest explanation available.
    """

    def messages(request: Any) -> Any:
        return [(m.role, m.content) for m in request.messages]

    differs = [
        part
        for part, mine, theirs in (
            ("the system prompt", composed.system, recorded.system),
            ("the messages", messages(composed), messages(recorded)),
            ("the tools", [t.name for t in composed.tools], [t.name for t in recorded.tools]),
            ("max_tokens", composed.max_tokens, recorded.max_tokens),
            ("temperature", composed.temperature, recorded.temperature),
        )
        if mine != theirs
    ]
    import textwrap

    click.echo(
        textwrap.fill(
            f"{' and '.join(differs)} moved since this fixture was recorded, so what follows is "
            f"the model's answer to the prompt as recorded — not to the one composed just now, "
            f"which `in-lockstep show-prompt review/security` prints. Re-recording is a real "
            f"model call, which is the thing a reader trying this offline does not have.",
            width=92,
            initial_indent="  note: ",
            subsequent_indent="        ",
        )
    )


def _load_changeset(artifact: str) -> Any:
    """Read a ChangeSet from a file or an artifact directory.

    Thin, deliberately: `platform.artifacts` owns the format, because the writer and the reader
    are in two different JOBS and agreeing by both being private functions in this module was
    agreement by proximity rather than by contract.
    """
    from .platform.artifacts import MalformedArtifact, read_changeset

    try:
        return read_changeset(artifact)
    except MalformedArtifact as e:
        raise click.ClickException(str(e)) from None


def _guard_or_exit(changeset: Any, *, root: Any = None) -> None:
    """The guard, and the refusal report. Shared by both apply paths on purpose.

    GATE-GUARD-1 names three enforcement points, and the way a set of enforcement points goes
    wrong is that one of them grows its own slightly different check. `--apply-inline` and
    `apply --from-artifact` make the same call here; the in-loop boundary is the third, inside
    `Workspace.record`, where a refusal has to be a tool result rather than an exit code.
    """
    from pathlib import Path as _Path

    from .core.changes import ChangeGuard

    base = _Path(root or ".")

    def previous(path: str) -> str | None:
        candidate = base / path
        try:
            return candidate.read_text() if candidate.is_file() else None
        except OSError:  # pragma: no cover - unreadable is indistinguishable from absent here
            return None

    refusals = ChangeGuard().check(changeset, read=previous)
    if not refusals:
        return
    click.echo("refused:")
    for refusal in refusals:
        click.echo(f"  {refusal.path}  (tier {refusal.tier}, rule {refusal.rule})")
    raise SystemExit(EXIT_BLOCKED)


def _run_registered(
    lockstep: Lockstep,
    recorder: Recorder | None,
    entry: Any,
    args: tuple[str, ...],
    no_middleware: bool,
    approval: Any = None,
    *,
    record: bool = False,
    asked: bool = False,
    cassette: str = "",
    blocked_ok: bool = False,
    parked_ok: bool = False,
    parent_run_id: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Dispatch a workflow the repository registered.

    `in-lockstep run <workflow>` is the first line of the README's command table and was refused
    for every id but one. `@workflow` registered into a registry nothing dispatched from, so a
    repository could declare a lifecycle the CLI would not run — which is most of what this
    framework claims to be, and the gap that gets papered over with shell in a CI file.
    """
    if no_middleware:
        lockstep.middleware = []

    parsed: dict[str, str] = {}
    for item in args:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise click.ClickException(f"--arg expects NAME=VALUE, got {item!r}")
        parsed[name] = value

    # `GITHUB_RUN_ID` stays in the base when CI provides one — it joins the record to the
    # workflow run — but it is not sufficient: a re-run attempt reuses it, and locally it was
    # a constant. `_run_id` is what makes the id unique.
    ci_run = os.environ.get("GITHUB_RUN_ID", "")
    base = entry.id.replace("/", "-") + (f"-{ci_run}" if ci_run else "")
    run_id = _run_id(base)
    # Built here rather than by the caller, because the run id is what names it and this is where
    # the run id exists. `implement` and `fix` arrive through this function and nowhere else.
    tape = _run_cassette(lockstep, run_id, cassette) if record else None
    ctx = _context(lockstep, run_id, approval, recording=tape)
    if parent_run_id:
        # A continuation: the child carries the parked run's id, so the ledger chains the two
        # and `history --explain` shows the lifecycle across the park (§13.4).
        ctx.parent_run_id = parent_run_id
    handed: dict[str, Any] = {**parsed, **(extra or {})}
    try:
        result = asyncio.run(entry.fn(ctx, **inject_ports(entry.fn, ctx, handed)))
    except TypeError as e:
        # A signature mismatch is the common mistake here, and the traceback for it points at
        # asyncio rather than at the workflow the user named.
        raise click.ClickException(
            f"{entry.id} did not accept those arguments: {e}. It takes "
            f"{', '.join(_parameters(entry.fn, ctx)) or '(no parameters)'}."
        ) from None
    except _SETUP_ERRORS as e:
        # The same translation `review` and `implement` already do. Without it the path this
        # framework recommends — put the process in `lockstep.py`, invoke it with `run` — gives a
        # forty-line traceback where a bespoke command gives one sentence, which is a reason not
        # to take the recommendation.
        raise click.ClickException(str(e)) from None

    click.echo(f"{entry.id}  {_describe(result, ctx)}")
    # Findings, not just the verdict. For a workflow whose whole product is a judgement, the
    # status line is the least interesting part of it.
    for finding in getattr(result, "findings", ())[:20]:
        where = f"{finding.path}:{finding.line} " if getattr(finding, "path", "") else ""
        click.echo(f"  {where}{finding.id}: {finding.message}")
    click.echo("")
    _echo_telemetry(recorder)
    click.echo(f"spend     ${ctx.spend.charged.usd:.4f}, {ctx.spend.charged.wall_seconds:.2f}s")
    if tape is not None:
        # Said, because a flag whose effect is a file in a directory nobody looks at is a flag
        # people stop passing. Zero is printed as zero and means it: the run made no model call.
        # Harvest only where something was kept: a tape with no calls has no case in it, and
        # `_harvest_in_process` would print a refusal about a recording nobody made.
        if _report_what_was_kept(tape, ctx, asked=asked):
            _harvest_in_process(tape, entry.id, lockstep)
    _write_workflow_ledger(lockstep, ctx, entry.id, result, parsed)
    if getattr(result, "status", None) is Status.PARKED:
        _mark_parked(lockstep, result)
    _exit_for(result, ctx, blocked_ok=blocked_ok, parked_ok=parked_ok)


def _ensure_review_bound(lockstep: Any) -> None:
    """Bind the shipped `AiReview` under the module's own ceilings when the module bound no
    `Review`, the way `review` does before it runs a lens. The model comes from the route and
    the tape from the run context, through the seam every adapter goes through."""
    from .adapters.ai.review import AiReview, Review
    from .ai.invoker import InvokePolicy

    if lockstep.container.has(Review):
        return
    lockstep.bind(
        Review,
        AiReview(
            repo_root=lockstep.repo.root,
            policy=InvokePolicy.under(
                lockstep.policy.resolve(),
                max_turns=_REVIEW_TURNS,
                max_tokens=_REVIEW_MAX_TOKENS,
                deadline_seconds=300,
            ),
        ),
    )


def _ensure_judge_bound(lockstep: Any) -> None:
    """Bind the shipped `AiJudge` when the module bound no `Judge`, as `_ensure_review_bound`
    does for `Review`: the route is the module's, and a repository that wrote one line
    (`models.route("judge", ...)`) should not need a second to say which adapter serves it."""
    from .adapters.ai.judge import AiJudge, Judge

    if lockstep.container.has(Judge):
        return
    lockstep.bind(Judge, AiJudge())


def _eval_judge(root: Path, budget: float | None) -> None:
    """`eval run --judge`: the `judge/corpus` workflow over `root`, as a run.

    Composing, not deciding: the module is loaded so the route, the budget and the tape are the
    repository's; the shipped judge and the shipped workflow are bound when the module bound
    neither; and `_run_registered` does what it does for every workflow, so the record, the exit
    code and the redaction are the same as `run judge/corpus`. What the judge sends, what is
    replayed and what a verdict settles live in `improver.py` and `workflows/judge.py`.
    """
    from .improver import CorpusImprover
    from .workflows import judge as judge_workflows

    lockstep, recorder = _default_lockstep()
    if budget is not None:
        from .core.spend import Budget

        lockstep.budget = Budget(usd=budget)
    _ensure_judge_bound(lockstep)
    if judge_workflows.CORPUS not in {r.id for r in registered()}:
        judge_workflows.register()
    _run_registered(
        lockstep,
        recorder,
        get(judge_workflows.CORPUS),
        (),
        False,
        None,
        record=_recording(record=None),
        extra={"improver": CorpusImprover(root)},
    )


def _ls_parked(lockstep: Any) -> None:
    """Parked inventory is first-class (§13.6): every barrier the shared store holds, with what
    each branch waits for and who resumed what."""
    import json

    from .core.human import HumanBoundary

    store: Any = _ledger(lockstep)
    if getattr(store, "scope", "local") != "shared":
        scope = getattr(store, "scope", "none")
        click.echo(
            f"parked    — (the ledger here is {type(store).__name__}, scope {scope!r}; nothing parks on it)"
        )
        return
    held = asyncio.run(store.states("barrier/"))
    if not held:
        click.echo("parked    0 run(s)")
        return
    for _key, text in sorted(held.items()):
        record = json.loads(text)
        state = "complete" if record.get("complete") else "waiting"
        click.echo(f"{record.get('run_id', '?'):<40} {state:<9} -> {record.get('resume', '?')}")
        for name, branch in (record.get("branches") or {}).items():
            if branch.get("kind") != "human":
                continue
            what = HumanBoundary.from_record(branch.get("boundary") or {}).describe()
            who = (
                f"  ({branch.get('actor')}: {branch.get('reason')})"
                if branch.get("state") == "resumed"
                else ""
            )
            click.echo(f"    {name or '(park)':<12} {branch.get('state'):<8} {what}{who}")


def _mark_parked(lockstep: Any, result: Any) -> None:
    """Put the park where the person will act (§13.1): the `lockstep:parked` label and the
    barrier's JSON on the pull request a `pr_review` boundary names. Best effort and said: a
    marker that could not be placed does not un-park the run, whose record is on the ledger."""
    parked = getattr(result, "value", None)
    if parked is None:
        return
    boundaries = getattr(parked, "boundaries", ())
    scm: Any = _bound_scm(lockstep)
    for _name, boundary in boundaries:
        if boundary.kind != "pr_review" or not hasattr(scm, "mark_parked"):
            continue
        try:
            asyncio.run(
                scm.mark_parked(int(boundary.target), parked.run_id, parked.resume, boundary.describe())
            )
            click.echo(f"marked    #{boundary.target} {PARKED_LABEL}")
        except (RuntimeError, OSError, ValueError) as e:
            click.echo(f"marked    could not mark #{boundary.target}: {str(e).strip()[:160]}", err=True)


def _ledger(lockstep: Any = None) -> Any:
    """Where a run record goes. The decision lives in `platform.ledger.store_for`, shared with
    the pre-run daily ceiling — two answers to "which ledger" is how a ceiling ends up summing
    records nothing appends to."""
    from .platform.ledger import store_for

    return store_for(lockstep.container if lockstep is not None else None)


def _provenance(lockstep: Any) -> dict[str, Any]:
    """What every record carries about the run's circumstances (schema 4).

    A record used to say what was spent and decided but not when, against which commit, or under
    which configuration — so joining a run to a release, or asking "was this the trusted config
    or somebody's working tree", meant archaeology. Absent facts stay absent: no base ref outside
    CI, no head in a directory that is not a repository. `ts` is wall-clock UTC, which is the one
    field here that is evidence about the world rather than about git.
    """
    from datetime import UTC, datetime

    from .platform.ci import detect as detect_ci

    ci_env = detect_ci()
    repo = getattr(lockstep, "repo", None)
    out: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(timespec="seconds")}
    if repo is not None and repo.head:
        out["head"] = repo.head
        if repo.branch:
            out["branch"] = repo.branch
        if repo.dirty:
            # Recorded only when true: a run against uncommitted changes is a run whose `head`
            # does not describe what the model actually saw, and the record must say so.
            out["dirty"] = True
    if ci_env is not None:
        if ci_env.base_ref:
            out["base"] = ci_env.base_ref
        if ci_env.actor:
            # The host-computed identity, beside whatever `--approved-by` claimed: the two
            # corroborate each other, and a mismatch is worth seeing in the record.
            out["ci_actor"] = ci_env.actor
        if ci_env.run_id:
            # The host's id for this run, which its artifacts carry too. It is the join that lets
            # `report --scm` say whether a bundled record has reached the branch without
            # downloading the bundle, and lets the sweep absorb each artifact once (#294).
            out["ci_run"] = ci_env.run_id
    identity = getattr(lockstep, "identity", None)
    if identity is not None and repo is not None:
        # Only when the module said so. `lockstep.identity = GitAuthor()` is a line a repository
        # writes deliberately and nothing detects (#289). Its own field, beside `ci_actor` (what
        # the host said) and `approval.by` (what a grant claimed), so a reader can tell the three
        # kinds of claim apart. Empty is absent, and absent is written nowhere.
        who = str(identity.claim(repo.root) or "")
        if who:
            out["identity"] = who
    source = str(getattr(lockstep, "config_source", "") or "")
    if source:
        out["config"] = source
    return out


def _record(ledger: Any, run_id: str, payload: dict[str, Any]) -> None:
    """Write it, and say plainly if it could not be written.

    A run that produced a real answer should not fail because git refused — but evidence going
    missing quietly is the exact failure this whole change is about, so the alternative to raising
    is saying so loudly, not swallowing it.
    """
    from .platform.ledger import HistoryError

    try:
        asyncio.run(ledger.append(run_id, payload))
    except (HistoryError, OSError) as e:
        click.echo(f"ledger    NOT WRITTEN: {e}")
        return
    click.echo(f"ledger    {ledger.location(run_id)}")


def _steps_with_subjects(lockstep: Any, steps: Sequence[Any]) -> list[dict[str, Any]]:
    """The run's step records, each review lens carrying the subject its own record would.

    Since PR-13 the required check is one `review/all-lenses` run whose four lenses are steps,
    and a step carried no `subject`, so every lens run since was invisible to `report --around`
    and to `--by subject` (the finding `GATE-LEDGER-2`'s row recorded). The subject is computed
    the way `_write_ledger` computes it for a bespoke `review` run: the bound composition for
    `review/<step>` and the model the route table names for that lens, or nothing when either
    cannot be known -- a subject guessed is worse than none.
    """
    from .ai.bootstrap import routed_model

    routes = dict(getattr(getattr(lockstep, "models", None), "routes", None) or {})
    out: list[dict[str, Any]] = []
    for step in steps:
        record = step.as_record(max_findings=_LEDGER_MAX_STEP_FINDINGS)
        if step.verb == "review" and step.step:
            model = routed_model(routes, "review", step.step)
            subject = _eval_subject(lockstep, kind="review", aspect=step.step, model_id=model)
            if subject is not None:
                record["subject"] = subject.key
                record["subject_label"] = subject.label()
        out.append(record)
    return out


def _write_workflow_ledger(
    lockstep: Any, ctx: Any, workflow_id: str, result: Any, args: dict[str, str]
) -> None:
    """Every dispatched run leaves a record, not only the ones with a bespoke command.

    Without this, moving a process out of `review`/`implement` and into a `@workflow` — which is
    what this framework asks you to do — silently costs the run its evidence. A record that exists
    for the built-in path and not the recommended one is an argument for not taking the
    recommendation.

    The `--arg` values are recorded because they are the provenance: which issue, which actor.
    They pass through the same redacting writer as everything else.
    """
    cost = getattr(result, "cost", None)
    status, reason, decided, own = _workflow_verdict(result, ctx)
    # The findings come from wherever the verdict came from: the workflow's own Outcome, or else
    # its top-level steps, so a red test step's `test.expectation_unmet` is on the record and
    # `report` can count what keeps going wrong. Top-level steps only, like the verdict: an
    # adapter that ran a test inside its own step already carries that test's findings upward,
    # and listing the inner step too would count one red suite twice.
    findings: tuple[Any, ...] = (
        tuple(getattr(result, "findings", ()) or ())
        if own
        else tuple(f for step in ctx.steps for f in step.outcome.findings)
    )
    _record(
        _ledger(lockstep),
        ctx.run_id,
        {
            "kind": "workflow",
            "workflow": workflow_id,
            **_provenance(lockstep),
            "args": dict(args),
            # The park this run continued from, so the two records chain (§13.4).
            **({"parent_run_id": ctx.parent_run_id} if getattr(ctx, "parent_run_id", None) else {}),
            # Who asked, and whether they watched. Absent when nobody did, which is the
            # ordinary case for a workflow needing no grant.
            **({"approval": ctx.approval.as_record()} if ctx.approval.granted else {}),
            "status": status,
            "reason": reason,
            "decided": decided,
            **_turns_taken(ctx),
            # What ran, in order, so `history --explain` can name the step that went red and a
            # reader of the raw record does not have to take the verdict on trust.
            "steps": _steps_with_subjects(lockstep, ctx.steps),
            # `total_tokens` is input plus output and EXCLUDES the cache, which is exactly the
            # number that stops making sense once caching is on. Run 33582850420 recorded 62,190
            # tokens for $13.84 — a thirty-three-fold drop against the run before it, and no way to
            # tell from the record whether that was a cheaper task or a working cache. The
            # breakdown is what answers it, and the ledger already had every field.
            "tokens": ctx.spend.charged.total_tokens,
            "input_tokens": ctx.spend.charged.input_tokens,
            "output_tokens": ctx.spend.charged.output_tokens,
            "cache_read_tokens": ctx.spend.charged.cache_read_tokens,
            "cache_write_tokens": ctx.spend.charged.cache_write_tokens,
            # Both are `None` when nothing was billable, and both stay `None` here rather than
            # being coerced — a run that spent nothing has no coverage to report, and a `1.0`
            # computed from an empty denominator is the reassuring number this ledger refuses.
            "billed_fraction": ctx.spend.charged.billed_fraction,
            "priced_fraction": ctx.spend.charged.priced_fraction,
            "cost_usd": round(ctx.spend.charged.usd, 6),
            "wall_seconds": round(ctx.spend.charged.wall_seconds, 3),
            **({"outcome_cost_usd": round(cost.usd, 6)} if cost is not None else {}),
            "findings": {
                "count": len(findings),
                "items": [f.as_record() for f in findings[:_LEDGER_MAX_FINDINGS]],
            },
        },
    )


def _setup_errors() -> tuple[type[BaseException], ...]:
    """Problems with the environment rather than with the run. Each has one obvious remedy."""
    from .ai.bootstrap import MissingCredential
    from .platform.artifacts import MalformedArtifact

    # `LookupError` covers a cassette that no longer matches the prompt it was recorded against.
    return (ImportError, LookupError, MissingCredential, MalformedArtifact)


_SETUP_ERRORS = _setup_errors()


def _parameters(fn: Any, ctx: Any = None) -> list[str]:
    """The arguments a caller supplies: everything but `ctx` and the container-injected ports."""
    import inspect

    injected = injectable_parameters(fn, ctx) if ctx is not None else set()
    return [p for p in inspect.signature(fn).parameters if p != "ctx" and p not in injected]


def _workflow_verdict(result: Any, ctx: Any) -> tuple[str, str | None, bool, bool]:
    """How a workflow run ended: the workflow's own `Outcome` when it returned one, else what its
    steps say. The record, the console line and the exit code all read this, so they cannot
    disagree: a run whose test step went red is `failed` in all three places or none. The last
    element says which of the two it was, so the record can take the findings from the same
    source it took the verdict from.

    The workflow's own verdict wins when it gave one, because a workflow may run a step it
    expects to fail (a reproducer before a fix) and say so by succeeding. A dict or `None` is not
    a verdict, and stamping it `"completed"` (a word the status set does not contain) is how a
    red run reached the report as not failed: the report had nothing to count it as. A result
    with a `status` that is not a `Status` (a `TestVerdict`, a string) is not a verdict either.
    """
    status = getattr(result, "status", None)
    if isinstance(status, Status):
        return status.value, getattr(result, "reason", None), bool(getattr(result, "decided", True)), True
    derived, reason, decided = ctx.verdict()
    return derived.value, reason, decided, False


def _describe(result: Any, ctx: Any) -> str:
    """A workflow returns whatever it likes; say something true about it either way."""
    status, reason, decided, _ = _workflow_verdict(result, ctx)
    return status + (f"  ({reason})" if reason else "") + ("" if decided else "  (decided nothing)")


def _exit_for(result: Any, ctx: Any, *, blocked_ok: bool = False, parked_ok: bool = False) -> None:
    status, reason, _, _ = _workflow_verdict(result, ctx)
    if status == Status.PARKED.value:
        click.echo(
            f"parked    {reason or 'at a human boundary'}; resume with "
            f"`in-lockstep resume --run {ctx.run_id} --as approved --by <you>`"
        )
        if parked_ok:
            # A proposing job whose run is meant to end waiting on a person: the park is the
            # outcome, said, and the job that opened the draft is not red for it.
            click.echo("exit      0: parked, and --parked-ok says that is the outcome this job wants")
            return
        raise SystemExit(EXIT_PARKED)
    if status == Status.BLOCKED.value:
        if blocked_ok:
            # A scheduled job whose run was refused before it spent -- nothing recurs yet, the
            # ceiling is reached, no proposal in the artifact -- is the control working, and the
            # record already says which. `improve.yml` used to make this translation in shell
            # (`test "$status" -eq 3 && exit 0`), a decision about what a run MEANS living in
            # YAML (#314); the flag keeps it here, where a test can reach it. Said, so a log
            # reader sees a refusal and not a green step that did nothing.
            click.echo(
                f"exit      0: blocked ({reason or 'no reason given'}), and --blocked-ok says that is fine"
            )
            return
        raise SystemExit(EXIT_BLOCKED)
    if status != Status.SUCCEEDED.value:
        raise SystemExit(EXIT_FAILED)


def _billing_note(cost: Any) -> str:
    """Why a run with real tokens shows no cost.

    `$0.0000` beside `5804 tokens` reads as a pricing failure, which is what `pricing.py` spends a
    module refusing to produce. Saying it on the same line is cheaper than letting somebody go and
    find out.
    """
    fraction = cost.billed_fraction
    if fraction is None or fraction >= 1.0:
        return ""
    if fraction == 0.0:
        return "  (replayed; nothing was billed)"
    return f"  ({fraction:.0%} of tokens billed, the rest replayed)"


def _echo_telemetry(recorder: Recorder | None) -> None:
    """What the CLI observed — or that it could not observe, which is a different statement."""
    if recorder is None:
        click.echo("spans     (lockstep.py declares its own middleware; the CLI is not in that chain)")
        return
    click.echo(f"spans     {len(recorder.spans)}")
    click.echo(f"metrics   {len(recorder.metrics)}")


@workflow(id="selfcheck")
async def selfcheck(ctx: RunContext, paths: tuple[str, ...]) -> dict[str, Any]:
    """Validate, then test. The smallest thing that proves the core actually dispatches.

    Skips a verb nothing is bound to rather than raising: detection binds only what it found, so a
    repository whose stack it could not place has no Test or Validate, and a `ResolutionError`
    traceback there would read as a broken tool rather than an unconfigured one.
    """
    bound = ctx.container.has
    validate = await ctx.do(Validate(paths=paths)) if bound(Validate) else None
    if validate is not None and validate.status is Status.BLOCKED:
        return {"validate": validate, "tests": None}
    tests = await ctx.do(Test(paths=paths)) if bound(Test) else None
    return {"validate": validate, "tests": tests}


@click.group()
@click.version_option(__version__, prog_name="in-lockstep")
def main() -> None:
    """Run your lifecycle."""
    # Before any command runs. CI logs are frequently public and are the sink most easily
    # forgotten, because printing does not feel like writing somewhere — so the stream is wrapped
    # rather than each of the sixty-odd `click.echo` calls below it. A call added later is covered
    # without anyone remembering that it had to be.
    sink.install_streams()
    # Env scraping is the fallback source, not the mechanism: `Auth` registers what it mints. But
    # a key already in the environment before a run starts is real, and this is where it is seen.
    redact_registry.seed_from_environment()


def _harvest_in_process(tape: Any, workflow_id: str, lockstep: Any) -> None:
    """Turn the tape into cases here, at the end of the run, rather than as a second CI statement.

    Two reasons, and the second is the one that decided it. `implement.yml` is at 13 of
    `MAX_STATEMENTS = 13`, and its own comment says a cap somebody raises whenever it bites is not
    a cap — so a harvest step would have cost a raise nobody had argued for. And a step that can be
    forgotten is a step that will be: the flag and what makes it worth passing belong together, or
    a repository ends up recording into a tape nothing reads, which is the defect #216's own review
    turned up one layer out.

    Never fatal. Harvest refuses rather than inventing -- a session whose answers state nothing a
    machine can settle is a real refusal -- and that is the wrong reason to fail work somebody is
    waiting on. The refusal is printed and the run keeps its own verdict.
    """
    from .evaluation.harvest import NothingToHarvest, harvest

    family = workflow_id.split("/")[0]
    into = Path(lockstep.repo.root) / ".lockstep" / "cases"
    try:
        found = harvest(tape.path, family=family)
    except NothingToHarvest as e:
        click.echo(f"cases     none: {e}")
        return
    for item in found:
        item.case.setdefault("harvested", {})["key"] = _case_key(item)
        sink.write_json(item.path_in(into), item.case)
    click.echo(f"cases     {len(found)} in {into}")


def _case_key(item: Any) -> str:
    """The hash of the request as the CASE carries it. See `_eval_harvest`, which does the same.

    Stamped by this layer because `evaluation` is a leaf that may not import the hash, and doing it
    twice is better than two implementations of one hash that drift apart in silence.
    """
    from .ai.replay import key_of, request_from

    stored = (item.case.get("input") or {}).get("request")
    return key_of(request_from(stored)) if isinstance(stored, dict) else ""


def _report_what_was_kept(tape: Any, ctx: Any, *, asked: bool) -> bool:
    """Say what the run kept, or say nothing. Returns whether it said anything.

    One function because there are two dispatch paths -- `_run_registered` for a workflow the
    repository declared, and the `selfcheck` branch beside it -- and the rule about when to speak
    was written into one of them. `selfcheck` went on printing `kept 0 inference(s)` for a run
    that dispatched Test and called no model at all, which is the exact drift this codebase
    argues against everywhere else: two writers of one decision, agreeing until one is edited.

    Silent when a run reached no provider, spent nothing, AND nobody asked. All three, and the
    third is the one a first attempt at this got wrong. Staying quiet by default is right: `kept 0`
    for `run selfcheck` reports on a thing that did not happen, and recording is what a run does
    now rather than something it was asked for. But a person who typed `--record` asked a question,
    and answering it with silence tells them neither where the tape went nor whether one exists --
    which is how a flag becomes a thing people stop passing, the property
    `test_a_recorded_run_says_where_it_put_the_tape` exists to hold.

    Spend is consulted rather than the call count alone, because `kept 0` after real spend is not
    the same sentence: it means the recorder was bypassed, and `_warn_if_calls_went_unrecorded`
    has to get to say so.
    """
    if not (asked or tape.calls() or getattr(ctx.spend.charged, "total_tokens", 0)):
        return False
    click.echo(f"kept      {tape.calls()} inference(s) in {tape.path}")
    _warn_if_calls_went_unrecorded(tape, ctx)
    return True


def _asked_to_record(click_ctx: Any) -> bool:
    """Whether the person typed `--record`, as against getting it by default.

    The difference decides whether silence is an answer. Recording is what a run does, so a run
    that kept nothing and was never asked says nothing about it; a person who typed the flag asked
    a question, and answering that with silence tells them neither where the tape went nor whether
    one exists.
    """
    try:
        return click_ctx.get_parameter_source("record") is not ParameterSource.DEFAULT
    except (AttributeError, KeyError):  # pragma: no cover - a hand-built context in a test
        return False


def _warn_if_calls_went_unrecorded(tape: Any, ctx: Any) -> None:
    """A run that spent tokens and kept nothing has a model call the recorder never saw.

    The seam covers every adapter that lets the framework build its invoker, which is the default
    and what `routed_invoker` does. An adapter constructed with its own `invoker_factory=` builds
    the provider itself, and nothing can reach inside a lambda to wrap it — so for that repository
    `--record` records nothing.

    Detected rather than predicted. The CLI does not know which adapters a workflow will dispatch,
    and guessing from the bindings would be wrong in both directions; the spend is a fact the run
    produced. So the discrepancy is measured after the fact and named, which is the difference
    between "kept 0" meaning "no model was called" and "kept 0" meaning "the recorder was bypassed"
    — two states one number cannot tell apart, and this repository does not let a number stand for
    both.
    """
    if tape.calls() or not getattr(ctx.spend.charged, "total_tokens", 0):
        return
    click.echo(
        "          nothing was kept, but this run spent tokens — so something called a model the "
        "recorder never saw. An adapter bound with its own `invoker_factory=` constructs its "
        "provider itself; wrap it there, or let the framework build the invoker."
    )


def _run_cassette(lockstep: Any, run_id: str, cassette: str) -> Any:
    """Where a workflow run's recording goes. OUTSIDE the repository, on a laptop as in CI.

    Not a CI convention promoted to a default. A write verb's tape holds every file the session
    opened and every command's output, and `read_file` refuses `.lockstep/cassettes/` precisely
    because a tape inside the tree is a file the NEXT run's model can open -- a concentration of
    everything the last session touched, arriving as context nobody chose to supply. Keeping it
    out of the tree is the control; the read refusal is the second lock on the same door.

    `RUNNER_TEMP` when the host names one, so O3's "the same process at a terminal and in CI" is
    the same PATH RULE too, rather than two rules that happen to agree today.

    Unlike `_cassette_default`, which review shares: a review is one turn with no tools, so its
    tape holds the diff somebody was already reviewing and there is nothing to read it back with.
    """
    import tempfile

    from .ai.replay import Cassette

    if cassette:
        chosen = Path(cassette).expanduser().resolve()
        root = Path(lockstep.repo.root).resolve()
        if chosen == root or root in chosen.parents:
            raise click.ClickException(
                f"--cassette {cassette} is inside the repository. A recording of a workflow run "
                f"holds every file the session read and every command's output, and a model's own "
                f"read_file can open anything in the tree — so a tape in here becomes the next "
                f"run's prompt. Put it under $TMPDIR."
            )
        return Cassette.load(chosen)
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in run_id).strip("-.") or "run"
    return Cassette.load(Path(base) / "in-lockstep" / f"{safe}.json")


def _cassette_default(lockstep: Any, verb: str) -> str:
    """Where a verb's recording goes when nobody passed `--cassette`.

    Joined to the repository root rather than left relative, which is the whole point of the
    helper. A bare `.lockstep/cassettes/review.json` resolves against the process working
    directory, so `in-lockstep review --record` run from a subdirectory wrote
    `<subdir>/.lockstep/cassettes/review.json` — a real recording, in a directory no `.gitignore`
    line matches, holding the whole composed prompt and the whole diff. The ignore lines are
    anchored (`.lockstep/cassettes/`, not `**/.lockstep/cassettes/`) and anchoring them is right;
    what was wrong was writing outside them.

    Only the default is joined. A path a person passed is theirs, and resolving it against a root
    they did not name would be the same surprise in the other direction.
    """
    return str(Path(lockstep.repo.root) / Path(CASSETTE_DIR) / f"{verb}.json")


def _recording(*, dry_run: bool = False, offline: bool = False, record: bool | None) -> bool:
    """Whether this run keeps what it pays for. The answer is yes unless somebody said otherwise.

    O4 says an inference nobody kept is an opportunity spent and discarded, and that recording is
    not an option a run turns on -- it is what a run does. It was a flag, and it was off, so
    `in-lockstep review` at a terminal called a model, spent money and kept nothing. Every
    trampoline this repository ships and every one `init` scaffolds passed the flag, which is why
    this survived: the framework's own runs all satisfied O4 and the gap was exactly the path a
    person meets first.

    Tri-state rather than a flipped boolean, and that is the whole subtlety. `--offline` and
    `--dry-run` each say answers come from somewhere that is not a provider, so a recording
    default that was simply `True` would contradict them on every offline run and refuse it. What
    `_one_provider` must keep refusing is a person who typed BOTH -- `--offline --record` is a
    contradiction and guessing is how a tool teaches people not to trust it -- and what it must
    not do is turn a default into a contradiction somebody never asked for. So `None` means
    nobody chose, and only an explicit `--record` can collide.
    """
    _one_provider(dry_run=dry_run, offline=offline, record=record is True)
    if record is not None:
        return record
    # Nobody chose. A run that reaches a provider keeps what it gets; one that was already told to
    # answer from somewhere else has nothing to keep.
    return not (dry_run or offline)


def _one_provider(*, dry_run: bool, offline: bool, record: bool) -> None:
    """Refuse two flags that each choose a provider, rather than ordering them.

    They used to be ordered, and the order was invisible: `dry_run`, then the shipped fixture, then
    `offline`, then a real provider wrapped in a recorder. So `--offline --record` recorded nothing
    and reported success — the person got a replay of the cassette they were trying to overwrite,
    a green run, and no cassette, and found out when `eval harvest` gave them nothing.

    A contradiction, not a precedence question. Each of these says where answers come from, and two
    answers is a question about what somebody meant. Guessing is how a tool teaches people not to
    trust it, and `ReplayProvider` already refuses a key miss loudly for the same reason.

    Called first in every command that takes them, so the refusal arrives before any credential
    work: a person who typed a contradiction should not be told about a missing key.
    """
    chosen = [
        name for name, on in (("--dry-run", dry_run), ("--offline", offline), ("--record", record)) if on
    ]
    if len(chosen) > 1:
        raise click.ClickException(
            f"{' and '.join(chosen)} each say where this run's answers come from, and it cannot be "
            f"both. Nothing here guesses which you meant: `--record` calls the provider and writes "
            f"a cassette, and the other two never reach one."
        )


@main.command(name="run")
@click.argument("target")
@click.option("--paths", multiple=True, help="Restrict to these paths.")
@click.option("--arg", "args", multiple=True, metavar="NAME=VALUE", help="Workflow parameter.")
@click.option("--no-middleware", is_flag=True, help="Bisect behaviour. Cannot disable the privileged tier.")
@click.option("--recover", "recover_id", default="", help="Resume an interrupted run by id.")
@click.option("--checkpoint/--no-checkpoint", default=False, help="Checkpoint completed steps.")
@click.option(
    "--budget",
    type=float,
    default=None,
    help="Hard ceiling, in USD. Without one, lockstep.py must declare a budget.",
)
@click.option("--approve", is_flag=True, help="You are the human watching this run.")
@click.option("--approved-by", default="", help="Who asked, when nobody is watching. Recorded.")
@click.option(
    "--record/--no-record",
    default=None,
    help="Keep this run's model calls. On by default; --no-record declines.",
)
@click.option(
    "--cassette",
    default="",
    type=click.Path(),
    help="Where to keep it. Must be outside the repository; defaults to $RUNNER_TEMP or $TMPDIR.",
)
@click.option(
    "--blocked-ok",
    is_flag=True,
    help="Exit 0 when the run is blocked: for a scheduled job whose refusal is the control working.",
)
@click.option(
    "--parked-ok",
    is_flag=True,
    help="Exit 0 when the run parked: for a job whose run is meant to end waiting on a person.",
)
def run_cmd(
    target: str,
    paths: tuple[str, ...],
    args: tuple[str, ...],
    no_middleware: bool,
    recover_id: str,
    checkpoint: bool,
    budget: float | None,
    approve: bool,
    approved_by: str,
    record: bool | None,
    cassette: str,
    blocked_ok: bool,
    parked_ok: bool,
) -> None:
    """Run a workflow, by the id it was registered under.

    This is the entry point external CI is meant to use. A process — gate, then implement, then
    propose, then say what happened — belongs in `lockstep.py` as a `@workflow`, where it is
    Python that can be read, tested and reasoned about. What belongs in a CI file is the trigger,
    the job split, and which credential each job holds, because those are the CI system's to grant.

    `--arg name=value`, repeatable, supplies workflow parameters. Values arrive as strings, which
    usefully bounds what a CLI-runnable workflow can take: one needing a list of tickets takes a
    label or a path, not the tickets.

    `--recover` restarts an interrupted run from its checkpoints. It covers machine failure only:
    a run never waits on a person, so resuming after a human is a different mechanism entirely.

    `--record` keeps every model call the run makes. This is the flag `implement` and `fix` never
    had: CI drives them through this command, and an adapter reached this way builds its own
    invoker, so there was no provider for a CLI flag to wrap. The run context carries the tape
    instead and `ai.bootstrap` wraps at the seam every adapter already goes through.
    """
    lockstep, recorder = _default_lockstep()
    # `run` declares neither `--offline` nor `--dry-run`, so nothing here can contradict the
    # default -- but it resolves through the same function as the five verbs that do, because two
    # places deciding what "nobody chose" means is how they come to disagree.
    #
    # Resolved into a new name rather than over the old one. `record` stays the raw tri-state,
    # which is the only remaining record of whether a person TYPED the flag, and that is a
    # different question from whether this run keeps anything: one decides behaviour, the other
    # decides whether the run owes them an answer about it.
    keeping = _recording(record=record)

    # A ceiling stated at the call site, exactly as `review` takes one. A budget is operational
    # rather than process — like a timeout — and one workflow in a module can be much more
    # expensive than another while `Budget` is run-scoped, so the module cannot express both.
    if budget is not None:
        from .core.spend import Budget

        lockstep.budget = Budget(usd=budget)

    # After loading, because loading `lockstep.py` is what registers the module's workflows. The
    # check used to run first, so the error listed only `selfcheck` no matter what a repository
    # had written — it was reporting an empty registry, not an unknown id.
    known = {r.id: r for r in registered()}
    if target not in known:
        raise click.ClickException(
            f"unknown workflow {target!r}. Registered: {', '.join(sorted(known)) or '(none)'}"
        )

    if target != "selfcheck":
        # The shipped Review, when the module bound none: `review` binds it a few lines into its
        # own body, and a workflow that does `ctx.do(Review(...))` -- `review/all-lenses` is the
        # required check here -- reached an empty container through this command instead.
        _ensure_review_bound(lockstep)
        _run_registered(
            lockstep,
            recorder,
            known[target],
            args,
            no_middleware,
            _approval(approve, approved_by),
            record=keeping,
            asked=record is True,
            cassette=cassette,
            blocked_ok=blocked_ok,
            parked_ok=parked_ok,
        )
        return
    if no_middleware:
        # Note what this does NOT switch off: the kill switch is checked before the chain, and
        # redaction, egress and residency are privileged rather than middleware.
        lockstep.middleware = []

    run_id = recover_id or _run_id("selfcheck")
    # The grant travels with the run, exactly as it does for a registered workflow above. It was
    # not passed here, and the flag this command declares therefore did nothing: `selfcheck`
    # dispatches Test, `PytestTest` declares EXECUTES_CODE, and a module that binds `ApprovalGate`
    # blocks on that. So a repository scaffolded by `init --implement` or `--fix` could not reach
    # a green selfcheck by any flag, and the message it printed named two that do not work here
    # (#189). `--no-middleware` was not the escape hatch either: it drops `CostBudget` with the
    # gate, and startup then refuses the undeclared budget instead.
    tape = _run_cassette(lockstep, run_id, cassette) if keeping else None
    ctx = _context(lockstep, run_id, _approval(approve, approved_by), recording=tape)
    if checkpoint or recover_id:
        from .platform.state import StateStore

        ctx.state = StateStore()
        ctx.recovering = bool(recover_id)
        if recover_id:
            done = ctx.state.completed(recover_id)
            click.echo(f"recovering {recover_id}: {len(done)} completed step(s)")
        else:
            # The id carries a per-invocation stamp and is no longer guessable, so a run that
            # checkpoints has to say what `--recover` should be given.
            click.echo(f"run       {run_id}  (resume an interrupted run with --recover {run_id})")
    result = asyncio.run(selfcheck(ctx, paths or (lockstep.repo.root,)))

    validate = result["validate"]
    tests = result["tests"]

    for label, outcome in (("validate", validate), ("test", tests)):
        if outcome is None:
            click.echo(f"{label:<9} not run")
            continue
        undecided = "" if outcome.decided else "  (decided nothing)"
        click.echo(
            f"{label:<9} {outcome.status.value}"
            + (f"  ({outcome.reason})" if outcome.reason else "")
            + undecided
        )
        for finding in outcome.findings[:5]:
            where = f"{finding.path}:{finding.line} " if finding.path else ""
            click.echo(f"          {where}{finding.id}: {finding.message}")

    click.echo("")
    _echo_telemetry(recorder)
    click.echo(f"spend     ${ctx.spend.charged.usd:.4f}, {ctx.spend.charged.wall_seconds:.2f}s")

    if tape is not None:
        _report_what_was_kept(tape, ctx, asked=record is True)
    # The rule `_write_workflow_ledger` states — every dispatched run leaves a record — applied
    # to the one dispatch that predates it. Selfcheck is the first command an adopter runs, and
    # its record carrying schema-4 provenance is how they see what a record even is.
    _write_workflow_ledger(lockstep, ctx, "selfcheck", result, {})
    # The same verdict the record just took, so the exit code cannot disagree with it. The old
    # logic here sent a blocked test step out as EXIT_FAILED while the record said blocked, and
    # CI read a control working as a failure.
    _exit_for(result, ctx, blocked_ok=blocked_ok)


@main.command(name="resume")
@click.option("--run", "run_id", required=True, help="The parked run.")
@click.option(
    "--as", "verdict", required=True, help="What the person did: approved, changes_requested, chosen:2, ..."
)
@click.option("--by", "actor", required=True, help="Who did it.")
@click.option("--branch", default="", help="The fan-out branch the event is for (a plain park has one).")
@click.option(
    "--event",
    "event_id",
    default="",
    help="The host's event id; defaults to a stamp, so two manual resumes are two events.",
)
@click.option("--text", default="", help="What they said, if anything.")
def resume_cmd(run_id: str, verdict: str, actor: str, branch: str, event_id: str, text: str) -> None:
    """Apply a person's event to a parked run, and start its continuation if that completes it.

    The local form of the resume trampoline (§13.3): `--event pr_review:41` from a webhook is the
    hosted one and is not built. Reads the barrier record from the SHARED store, applies the event
    through the tick -- deduped on the event id, swapped under the remote's ref lock, so a
    redelivered event or a second engineer's simultaneous resume applies once -- and, when this
    was the write that completed the barrier, runs the continuation the record names as a fresh
    run carrying `parent_run_id`. Refused by name on a LOCAL store: nothing parks there.
    """
    from .platform.barrier import BarrierError, tick

    lockstep, recorder = _default_lockstep()
    store: Any = _ledger(lockstep)
    if getattr(store, "scope", "local") != "shared":
        raise click.ClickException(
            f"the ledger here is {type(store).__name__} at scope {getattr(store, 'scope', 'none')!r}; "
            f"a parked run lives on a SHARED store (GitLedger(shared=True) bound as LedgerStore)"
        )
    event = Resumption(
        parent_run_id=run_id,
        branch=branch,
        event_id=event_id or _run_id("manual"),
        actor=actor,
        verdict=verdict,
        text=text,
    )

    # The tick decides whether this event completed the barrier and hands the resumption out;
    # the continuation runs after the tick's loop has closed, because a run is its own event loop
    # (`_run_registered` starts one) and a loop cannot be started inside another.
    handed: list[Resumption] = []

    async def launch(resumption: Resumption) -> None:
        handed.append(resumption)

    try:
        done = asyncio.run(tick(store, event=event, launch=launch))
    except BarrierError as e:
        raise click.ClickException(str(e)) from None
    if done.duplicate:
        click.echo(f"resume    event {event.event_id} was already applied to {run_id}; nothing to do")
        return
    if not done.launched:
        waiting = [n or "(park)" for n, b in done.record["branches"].items() if b.get("state") == "parked"]
        click.echo(f"resume    applied to {run_id}; still waiting on {', '.join(waiting)}")
        return
    (resumption,) = handed
    entry = _continuation_for(run_id, store)
    click.echo(f"resuming  {run_id} -> {entry.id}  (by {actor}, {verdict})")
    _clear_parked(lockstep, resumption)
    _run_registered(
        lockstep,
        recorder,
        entry,
        (),
        False,
        parent_run_id=run_id,
        extra=_resumption_kwargs(entry.fn, resumption),
    )


def _continuation_for(run_id: str, store: Any) -> Any:
    """The registered workflow the barrier names, or a refusal that names the id."""
    from .platform.barrier import read_barrier

    record = asyncio.run(read_barrier(store, run_id)) or {}
    wanted = str(record.get("resume", ""))
    entry = get(wanted)
    if entry is None:
        raise click.ClickException(
            f"run {run_id} parked naming continuation {wanted!r}, which nothing registers here "
            f"(known: {', '.join(sorted(r.id for r in registered())) or '(none)'}). A park record "
            f"references an id, never a function name, so register the id."
        )
    return entry


def _resumption_kwargs(fn: Any, resumption: Resumption) -> dict[str, Any]:
    """The continuation's `Resumption` parameter, found by annotation rather than by name."""
    import inspect
    import typing

    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 - an unresolvable annotation is the workflow's problem to report
        hints = {}
    for name in inspect.signature(fn).parameters:
        if hints.get(name) is Resumption:
            return {name: resumption}
    return {}


def _clear_parked(lockstep: Any, resumption: Resumption) -> None:
    """Take the marker off the pull request the resumed branch named, best effort."""
    scm: Any = _bound_scm(lockstep)
    if not hasattr(scm, "clear_parked"):
        return
    branch = resumption.join.get(resumption.branch) or {}
    boundary = branch.get("boundary") or {}
    if boundary.get("kind") != "pr_review":
        return
    try:
        asyncio.run(scm.clear_parked(int(boundary.get("target", 0)), resumption.parent_run_id))
    except (RuntimeError, OSError, ValueError) as e:
        click.echo(f"marked    could not clear #{boundary.get('target')}: {str(e).strip()[:160]}", err=True)


@main.command(name="ls")
@click.option("--parked", is_flag=True, help="List the runs waiting on a person, from the SHARED store.")
def ls_cmd(parked: bool) -> None:
    """Print the resolved container and policy stack.

    Config as code can hide the effective setup in a way a YAML file cannot — you can read a
    manifest, but you cannot read a container. This is the answer to "what will actually run".
    """
    lockstep, _ = _default_lockstep()
    if parked:
        _ls_parked(lockstep)
        return

    click.echo(f"repo      {lockstep.repo.root}")
    click.echo(
        f"head      {lockstep.repo.head[:12] or '(none)'}"
        f"  branch {lockstep.repo.branch or '(none)'}"
        f"{'  dirty' if lockstep.repo.dirty else ''}"
    )
    detected = lockstep.repo.facts.summary()
    if detected:
        click.echo(f"detected  {'; '.join(detected)}")
    # The other half of O1's rule. Printing only what was found made an unsupported stack and a
    # misconfigured one produce the same output, so a person could not tell which they had.
    declined = lockstep.repo.facts.declined()
    if declined:
        click.echo(f"declined  {declined}")
    if os.environ.get(DISABLE_ENV):
        click.echo(f"DISABLED  {DISABLE_ENV} is set; no adapter will execute")

    # Verbs are open, so a verb can exist that the `bindings` block below will never mention.
    # That is exactly the shape a typo takes: `Verb("reviwe")` is a perfectly legitimate verb
    # that nothing serves. Shipped verbs with no binding are ordinary and would only be noise
    # here — most are unbound in a default install — so only the anomaly is printed.
    served = {verb_of(b.impl) for b in lockstep.container.resolved() if not isinstance(b.impl, type)}
    orphans = [v for v in Verb.known() if v not in served and v.value not in SHIPPED_VERBS]
    if orphans:
        click.echo("")
        click.echo("verbs defined but unbound  (a typo makes one of these)")
        for verb in orphans:
            click.echo(f"  {verb.value}")

    click.echo("")
    click.echo("bindings")
    for binding in lockstep.container.resolved():
        name = f" [{binding.name}]" if binding.name else ""
        impl = binding.impl if isinstance(binding.impl, type) else type(binding.impl)
        label = f"{binding.iface.__name__}{name}"
        # The strategy IS the adapter, so "what will an Implement run actually do" is the impl
        # column itself: `Implement -> TDD` needs no further annotation.
        click.echo(
            f"  {label:<22} -> {impl.__name__:<16}({binding.scope.value}, {binding.tier.name.lower()})"
        )
        # Where a deterministic adapter found its tool, or that it did not. Printed here because
        # a wrong answer (#167: the tool's own interpreter instead of the repository's) is
        # something to see before a run, not to infer from a red suite afterwards.
        if not isinstance(binding.impl, type) and isinstance(binding.impl, Locatable):
            # Under the impl column, wherever the label's width put it.
            indent = 2 + max(22, len(label)) + len(" -> ")
            for resolution in binding.impl.locations(lockstep.repo.root):
                click.echo(f"{'':<{indent}}{resolution.render()}")

    # What each AI binding would actually compose. `bindings` above says which adapter serves a
    # verb; this says what that adapter will tell the model, which is the other half of the same
    # question and was answerable only by reading the module. The guardrail chain is printed
    # rather than the whole projection because that is the line worth checking at a glance: a
    # stack that does not open with the shipped baseline is a decision, and it should not take a
    # second command to notice it.
    composers = [
        b
        for b in lockstep.container.resolved()
        if not isinstance(b.impl, type) and isinstance(b.impl, Inspectable)
    ]
    if composers:
        # Reading the shipped bodies is file IO, so it happens here rather than above: a
        # repository that binds no AI adapter should pay nothing for a block it will not print.
        shipped = _shipped_compositions()
        click.echo("")
        click.echo("prompts  (what each AI binding composes; * = not the shipped prompt)")
        for binding in composers:
            composed = binding.impl.compositions()
            if not composed:
                continue
            names = " ".join(
                label + ("" if _is_shipped(shipped, label, composed[label]) else "*")
                for label in sorted(composed)
            )
            click.echo(f"  {type(binding.impl).__name__:<18} {names}")
            for chain, drift, scope in _guardrail_chains(composed):
                click.echo(f"  {'':<18} guardrails: {chain}{scope}{drift}")

    click.echo("")
    click.echo("middleware  (privileged tier runs outside this chain and is not listed)")
    for mw in lockstep.middleware:
        click.echo(f"  {type(mw).__name__}")

    click.echo("")
    click.echo("standards  (in_lockstep.standards entry points; applied before this module's own lines)")
    for label in getattr(lockstep, "standards", None) or ["(none installed)"]:
        click.echo(f"  {label}")

    click.echo("")
    click.echo("policy")
    layers = lockstep.policy.layers
    if not layers:
        click.echo("  (nothing contributed)")
    else:
        for layer in layers:
            click.echo(f"  {layer.name}  <- {layer.source or 'local'}")
        resolved = lockstep.policy.resolve()
        click.echo(
            f"  = scan={resolved.scan_input or '(unset)'}"
            f" deny_tools={len(resolved.deny_tools)}"
            f" max_turns={resolved.max_turns if resolved.max_turns is not None else '(unset)'}"
            f" max_idle_turns={resolved.max_idle_turns if resolved.max_idle_turns is not None else '(unset)'}"
        )

    routes = lockstep.models.routes
    if routes:
        click.echo("")
        click.echo("models  (verb -> model; resolved per verb, or per lens as review/<lens>)")
        known = {v.value for v in Verb.known()}
        # Read only when a route names a lens: the lens list composes prompts, which is file IO a
        # repository routing by verb alone should not pay for.
        lenses = _review_lenses(lockstep) if any("/" in key for key in routes) else ()
        for routed_verb, model_id in sorted(routes.items()):
            click.echo(f"  {routed_verb:22} {model_id}{_route_flag(routed_verb, known, lenses)}")

    click.echo("")
    click.echo("workflows")
    for entry in registered():
        click.echo(f"  {entry.id}  ({entry.module})")


def _eval_harvest(from_cassette: str, into: str, family: str) -> None:
    """Cassette -> cases. Refuses rather than inventing when the recording cannot back one."""
    from pathlib import Path as _Path

    from .evaluation.harvest import NothingToHarvest, harvest

    if not from_cassette:
        raise click.ClickException(
            "pass --from <cassette>. Recordings live in .lockstep/cassettes/ after a `--record` run."
        )
    try:
        found = harvest(_Path(from_cassette), family=family)
    except NothingToHarvest as e:
        raise click.ClickException(str(e)) from None

    from .ai.replay import key_of, request_from
    from .privileged import sink

    target = _Path(into)
    # Belt and braces over `harvest`'s naming, and the reason is what it cost: a nine-turn tape
    # named every case after the ticket every turn shared, and this loop wrote nine files over each
    # other and then printed "9 case(s) from ...". Loss that reports as success is the one failure
    # this repository's whole eval story is about, so the count is checked rather than trusted.
    paths = [item.path_in(target) for item in found]
    if len(set(paths)) != len(paths):
        clashing = sorted({str(p) for p in paths if paths.count(p) > 1})
        raise click.ClickException(
            f"{len(found)} case(s) resolve to {len(set(paths))} path(s), so writing them would "
            f"silently keep the last: {clashing}"
        )
    for item in found:
        # Stamped here rather than in `harvest`, which may not import the hash: `evaluation` is a
        # leaf. This is the same join `_eval_subject` makes for a different reason — the layer that
        # can see both halves is the one that puts them together.
        #
        # The hash is of the request as the CASE carries it, so `eval run` re-hashing that same
        # request agrees with it. It is not the key the tape filed the entry under; those differ
        # whenever redaction masked anything, and using the tape's key made a redacted case fail
        # its own integrity check.
        stored = (item.case.get("input") or {}).get("request")
        if isinstance(stored, dict):
            item.case["harvested"]["key"] = key_of(request_from(stored))
        path = item.path_in(target)
        # Through the sink like every other write: a harvested case carries a whole request and a
        # whole answer, which is the pair most likely to have a credential in it.
        sink.write_json(path, item.case)
        click.echo(f"wrote  {path}")
    click.echo("")
    click.echo(f"{len(found)} case(s) from {from_cassette}")
    click.echo(
        "Expectations were derived from the answers that were recorded, so these pass against "
        "those answers today. What they buy is a baseline: run them after changing anything below "
        "the model and they settle; change the prompt and measuring it is a real call."
    )


def _eval_run(cases: list[Any], root: Path) -> None:
    """Replay each case's recorded request and grade the answer that comes back.

    A verdict kept beside a case is replayed the same way (`GATE-JUDGE-3`): a sidecar row whose
    replay key is this case's current rubric over its recorded answer settles the rubric with no
    model, and a row whose key no longer matches -- the rubric tightened, the answer re-recorded
    -- is not a verdict on this case and leaves it outstanding. Without this, the first sidecar
    made `make check` read the promoted case as undecided the moment it stated a rubric.

    Free, and it measures the harness rather than the prompt — the request replayed is the one that
    was recorded, so what is exercised is everything between a model's reply and an outcome. A case
    with no recorded request cannot be settled this way, and is reported as such rather than
    counted against anything.

    "Replay" is now mostly a figure of speech: a harvested case carries the answer it was derived
    from, so settling it opens nothing. The cassette path is a fallback for cases written before
    that field existed, and it is the weaker of the two — a tape is scratch, and the ones worth
    harvesting from are made in CI and destroyed with the runner.
    """
    from .ai.replay import Cassette, key_of, request_from
    from .evaluation import summarize
    from .evaluation.cases import grade
    from .improver import CorpusImprover

    improver = CorpusImprover(root)
    asks = {ask.case: ask for ask in improver.corpus_rubrics()}
    kept = {verdict.key: verdict for verdict in improver.known_verdicts()}
    replayed = 0

    def verdict_for(case: Any) -> dict[str, Any] | None:
        nonlocal replayed
        ask = asks.get(case.name)
        verdict = kept.get((ask.rubric_sha256, ask.answer_sha256)) if ask is not None else None
        if verdict is None:
            return None
        replayed += 1
        return {"level": verdict.level, "reason": verdict.reason, "evidence": list(verdict.evidence)}

    tapes: dict[str, Any] = {}
    results: list[dict[str, Any]] = []
    unplayable: list[tuple[str, str]] = []

    for case in cases:
        request = (case.input or {}).get("request")
        if not isinstance(request, dict):
            unplayable.append((case.name, "no recorded request — written by hand, not harvested"))
            continue

        # The case's own answer first. A case that carries one needs nothing else on disk, which
        # is the point: it travels out of a runner in an artifact and into a repository, while the
        # cassette it was harvested from was deleted with the job that made it.
        if case.recorded:
            declared = str((case.harvested or {}).get("key", ""))
            actual = key_of(request_from(request))
            if declared and declared != actual:
                # The pair does not hash together, so one of the two was edited after recording.
                # Grading anyway would settle a question against the answer to a different one,
                # which is the fabrication the whole corpus exists to refuse.
                unplayable.append(
                    (case.name, f"its request and its answer are not the pair recorded as {declared[:12]}")
                )
                continue
            results.append(grade(case, _as_answer(str(case.recorded.get("content", ""))), verdict_for(case)))
            continue

        where = str((case.harvested or {}).get("cassette", ""))
        if not where:
            unplayable.append((case.name, "no recorded answer, and no cassette to find one in"))
            continue
        if where not in tapes:
            tapes[where] = Cassette.load(where)
        output = tapes[where].replay_provider(request_from(request))
        if output is None:
            unplayable.append(
                (case.name, f"its recording is gone from {where} ({key_of(request_from(request))[:12]})")
            )
            continue
        results.append(grade(case, _as_answer(output.content)))

    for name, why in unplayable:
        click.echo(f"  {'SKIP':<7} {name}  — {why}")
    for result in results:
        settled = result["deterministic_passed"]
        mark = "ok" if settled else ("FAIL" if settled is False else "—")
        click.echo(f"  {mark:<7} {result['case']}")
        for check in result["checks"]:
            if not check["passed"]:
                click.echo(f"          {check['check']}: {check['detail']}")

    summary = summarize(results)
    click.echo("")
    click.echo(f"cases        {summary['total']} replayed, {len(unplayable)} skipped")
    click.echo(f"decided      {summary['decided']}")
    click.echo(f"judged       {summary['judged']}  ({replayed} replayed from sidecars)")
    click.echo(f"outstanding  {summary['outstanding']}  (need a judge)")
    rate = summary["pass_rate"]
    click.echo(f"pass rate    {'n/a — nothing decided' if rate is None else f'{rate:.0%}'}")
    click.echo("")
    click.echo("Replayed, so nothing was spent and no prompt was tested. This settles the path")
    click.echo("between a model's reply and an outcome; changing the prompt is a real model call.")
    # A judged rubric that failed fails the settle like a failed check (GATE-JUDGE-1): a kept
    # verdict below the bar is evidence about this answer, and `make check` should say so.
    if any(r["deterministic_passed"] is False or r.get("rubric_passed") is False for r in results):
        raise SystemExit(EXIT_FAILED)


def _as_answer(content: str) -> Any:
    """A recorded reply as the grader should see it: parsed when it is JSON, text when it is not."""
    import json as _json

    try:
        return _json.loads(content)
    except ValueError:
        return content


@main.command(name="eval")
@click.argument("action", default="report")
@click.option("--corpus", default="", help="Where the cases live.")
@click.option(
    "--from",
    "from_cassette",
    default="",
    type=click.Path(),
    help="harvest: the recording to build cases from.",
)
@click.option(
    "--into", default="", type=click.Path(), help="harvest: where to write them. Defaults to --corpus."
)
@click.option("--family", default="", help="harvest: a directory to group the new cases under.")
@click.option(
    "--judge",
    is_flag=True,
    help="run: put every rubric to the bound judge, as a recorded run. Without it, nothing is spent.",
)
@click.option(
    "--budget",
    type=float,
    default=None,
    help="--judge: hard ceiling, in USD. Without one, lockstep.py must declare a budget.",
)
def eval_cmd(
    action: str, corpus: str, from_cassette: str, into: str, family: str, judge: bool, budget: float | None
) -> None:
    """Build cases from recorded runs, and settle them offline.

    `harvest` turns a cassette into cases — real requests that were really sent, with expectations
    derived from the answers that really came back. `run` replays each one and grades it. `report`
    says what the corpus asks for without running anything, and `list` names the cases.

    Deterministic expectations are settled. Rubric expectations are reported as OUTSTANDING,
    because a judge has not answered them — recording them as passes would put a perfect score
    computed from no evidence into a baseline that is then compared against forever.

    `run --judge` is the one form that spends: it is a run of the `judge/corpus` workflow, on the
    model routed for `judge`, recorded and priced like every other run, and every verdict it
    gets is kept beside its case so the next `--judge` pays only for what changed. Plain `run`
    stays what `make check` calls: offline, nothing billed.
    """
    from pathlib import Path as _Path

    from .evaluation import load_cases

    root = _Path(corpus) if corpus else _Path(__file__).parent / "corpus"

    if action == "harvest":
        _eval_harvest(from_cassette, into or str(root), family)
        return

    if not root.exists():
        raise click.ClickException(f"no corpus at {root}")

    cases = load_cases(root)
    if action == "list":
        for case in cases:
            rubric = " (rubric)" if case.rubric else ""
            click.echo(f"{case.name}{rubric}")
        click.echo(f"\n{len(cases)} case(s)")
        return

    if action == "run":
        if judge:
            _eval_judge(root, budget)
            return
        _eval_run(cases, root)
        return

    # No model runs here and no answer exists, so nothing is decided: this reports what the
    # corpus asks of one. It used to grade each case against `None` and let `summarize` count
    # what fell out, which read as "outstanding" only for as long as an unjudged rubric hid a
    # failed check; once a failing half decides a case (GATE-JUDGE-3), a check graded against
    # no answer is a failure, and a report that printed `0%` over nothing would be the
    # reassuring-looking number this ledger refuses.
    scored = sum(1 for case in cases if case.rubric is not None and case.rubric.scored)
    summary: dict[str, Any] = {"total": len(cases), "decided": 0, "outstanding": scored, "pass_rate": None}

    by_family: dict[str, int] = {}
    for case in cases:
        family = case.path.parent.parent.name if case.path else "?"
        by_family[family] = by_family.get(family, 0) + 1
    for family in sorted(by_family):
        click.echo(f"  {family:<12} {by_family[family]} case(s)")

    click.echo("")
    click.echo(f"cases        {summary['total']}")
    click.echo(f"decided      {summary['decided']}")
    click.echo(f"outstanding  {summary['outstanding']}  (need a judge)")
    rate = summary["pass_rate"]
    click.echo(f"pass rate    {'n/a — nothing decided' if rate is None else f'{rate:.0%}'}")
    click.echo("")
    click.echo("A rubric nobody judged is outstanding, not passed.")


@main.command(name="history")
@click.option("--push", is_flag=True, help="Publish the branch. Needs push access; never automatic.")
@click.option(
    "--pull",
    is_flag=True,
    help="Fetch the remote's records onto the local branch, record by record. Pushes nothing.",
)
@click.option(
    "--bundle", default="", type=click.Path(), help="Write the branch to a file to travel as an artifact."
)
@click.option("--from-bundle", default="", type=click.Path(), help="Take in history another job recorded.")
@click.option(
    "--from-artifacts",
    default="",
    metavar="NAME",
    help="Absorb every outstanding bundle the host still holds under this artifact name, once each.",
)
@click.option(
    "--acknowledge",
    default="",
    metavar="COMMIT",
    help="Stand behind one flagged rewrite by name. Needs --reason; --by defaults to the git author.",
)
@click.option("--reason", default="", help="Why the rewrite in --acknowledge was legitimate.")
@click.option("--by", default="", help="Who is acknowledging. Defaults to the configured git author.")
@click.option("--limit", type=int, default=20, show_default=True)
@click.option(
    "--explain",
    default="",
    metavar="RUN",
    help="One run's record, every field, in words. A prefix finds the latest matching run.",
)
def history_cmd(
    push: bool,
    pull: bool,
    bundle: str,
    from_bundle: str,
    from_artifacts: str,
    acknowledge: str,
    reason: str,
    by: str,
    limit: int,
    explain: str,
) -> None:
    """Run records, on an orphan branch that touches nothing anybody works on.

    Records are committed locally as each run finishes. Publishing is a separate act, because
    reaching a remote needs credentials and is a side effect nobody asked for by typing a command
    in a terminal — so a laptop accumulates history and CI, or a person, pushes it.
    """
    from .platform.ledger import GitLedger, HistoryError

    if explain:
        _explain_run(explain)
        return

    ledger = GitLedger()

    if pull:
        # The second engineer's act: what a colleague pushed comes onto this clone's branch. A
        # read never does this by itself -- a network call nobody asked for -- so `report` says how
        # far the two refs differ and this is the command that closes the gap (#307).
        try:
            pulled = ledger.pull()
        except HistoryError as e:
            raise click.ClickException(str(e)) from None
        what = "created the local branch from" if pulled.created else "folded into the local branch from"
        click.echo(f"pulled    {what} {ledger.remote}/{ledger.branch}  (+{pulled.gained} record(s))")

    # Absorb first, so `--from-bundle --push` in one invocation does what it reads like.
    if from_bundle:
        try:
            ledger.absorb(from_bundle)
        except HistoryError as e:
            raise click.ClickException(str(e)) from None
        click.echo(f"absorbed  {from_bundle}")

    if from_artifacts:
        # The sweep. What `lockstep.yml`'s `publish` job missed -- a run cancelled by the next
        # push, a refused push -- sits in an artifact on a 30-day clock, and this takes each one
        # in once, deciding "once" the way `reconcile.py` says. Pushed below with everything else
        # when `--push` is given, so the absorbs and the push are one act as they read.
        from .platform.ledger.reconcile import absorb_outstanding

        lockstep, _recorder = _default_lockstep()
        try:
            swept = absorb_outstanding(ledger, _bound_scm(lockstep), from_artifacts)
        except (HistoryError, RuntimeError) as e:
            raise click.ClickException(str(e)) from None
        for taken in swept.taken:
            click.echo(f"absorbed  artifact {taken.id}  (run {taken.run_id})")
        for empty in swept.empty:
            click.echo(f"empty     artifact {empty.id}  (run {empty.run_id}) carried no bundle")
        for failed, why in swept.failed:
            click.echo(f"failed    artifact {failed.id}  (run {failed.run_id}): {why}", err=True)
        if swept.expired:
            click.echo(
                f"expired   {len(swept.expired)} artifact(s) expired before anyone absorbed them; "
                f"those records are gone"
            )
        click.echo(
            f"artifacts {len(swept.taken)} absorbed, {len(swept.empty)} empty, "
            f"{len(swept.failed)} failed, {len(swept.expired)} expired"
        )
        found_local = ledger.resolved()
        if push and found_local is not None and found_local[0] != ledger.ref:
            # A sweep that took nothing on a checkout with only the remote-tracking ref made no
            # local branch, and `push()` rightly refuses to send the remote's own commit back at
            # it. The first scheduled sweep after #307 did exactly this and went red on a night
            # nothing happened (#312): nothing to publish is the ordinary quiet outcome, said.
            click.echo(
                "pushed    nothing: the sweep absorbed nothing and this checkout has no history of its own"
            )
            push = False

    if acknowledge:
        # A person's act, by name. `--by` defaults to the configured git author whether or not the
        # module opted into recording it on runs: the opt-in exists so nobody is named who did not
        # choose to be, and running this command is the choice. No name at all is refused rather
        # than signed "in-lockstep", because a note the framework signed is one nobody stands
        # behind.
        from .platform.identity import GitAuthor

        who = by or GitAuthor().claim(str(ledger.root))
        try:
            note = ledger.acknowledge(acknowledge, reason=reason, by=who)
        except HistoryError as e:
            raise click.ClickException(str(e)) from None
        click.echo(f"acknowledged  {note.commit[:12]}  rewrote {len(note.lines)} record(s)  (by {note.by})")
        for line in note.lines:
            click.echo(f"              {line}")

    found = ledger.resolved()
    if found is None:
        click.echo(f"no history yet on {ledger.branch}; it is created by the first run that records one")
        return

    ref, head = found
    records = ledger.records()
    # Which ref was read, when it was not the local branch: a fresh clone reading the remote's
    # copy is served, and told so, rather than told there is nothing here (#307).
    where = "" if ref == ledger.ref else f", read from {ref}"
    click.echo(f"branch    {ledger.branch}  ({head[:12]}, {len(records)} record(s){where})")
    click.echo("")
    for record in records[-limit:]:
        kind = str(record.get("kind", "run"))
        status = str(record.get("status", "?"))
        cost = record.get("cost_usd")
        money = f"${float(cost):.4f}" if isinstance(cost, (int, float)) else "     -"
        click.echo(f"  {str(record.get('run_id', '?')):<34} {kind:<10} {status:<10} {money}")

    if bundle:
        try:
            written = ledger.bundle(bundle)
        except HistoryError as e:
            raise click.ClickException(str(e)) from None
        click.echo("")
        click.echo(f"bundled   {written}")

    if not push:
        return
    try:
        where = ledger.push()
    except HistoryError as e:
        raise click.ClickException(str(e)) from None
    click.echo("")
    click.echo(f"pushed to {where}")


def _latest_matching(ledger: Any, prefix: str) -> dict[str, Any] | None:
    """The newest record whose run id starts with `prefix`.

    Run ids carry a per-invocation stamp, so `--explain review-security` — the name a person
    actually remembers — should find the latest such run rather than demanding the stamp be typed
    back. An exact id still wins outright; this is the fallback when the exact read found nothing.
    """
    matches: list[dict[str, Any]] = [
        r for r in ledger.records() if str(r.get("run_id", "")).startswith(prefix)
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: (str(r.get("ts", "")), str(r.get("run_id", ""))))
    chosen = matches[-1]
    if len(matches) > 1:
        click.echo(f"matched   {chosen.get('run_id')}  (latest of {len(matches)} starting {prefix!r})")
    return chosen


def _explain_run(run_id: str) -> None:
    """Everything one record says, in the order a person asks: what ran, what happened, what it
    cost, and under whose authority — plus where the session transcript is, when one exists."""
    ledger = _ledger()
    record = asyncio.run(ledger.read(run_id))
    if record is None:
        record = _latest_matching(ledger, run_id)
    if record is None:
        raise click.ClickException(
            f"no record for {run_id!r} in {getattr(ledger, 'branch', None) or getattr(ledger, 'root', '?')}"
        )
    # The full id, whichever way the record was found: the transcript lookup below is keyed by it.
    run_id = str(record.get("run_id", run_id))

    def line(label: str, key: str, render: Any = str) -> None:
        value = record.get(key)
        if value not in (None, "", [], {}):
            click.echo(f"{label:<10}{render(value)}")

    line("run", "run_id")
    what = str(record.get("kind", "run"))
    for extra in ("workflow", "aspect", "strategy"):
        if record.get(extra):
            what += f"  {record[extra]}"
    click.echo(f"{'what':<10}{what}")
    status = str(record.get("status", "?"))
    if record.get("reason"):
        status += f"  ({record['reason']})"
    if record.get("decided") is False:
        status += "  decided nothing"
    click.echo(f"{'status':<10}{status}")
    line("when", "ts")
    line("head", "head")
    line("branch", "branch")
    if record.get("dirty"):
        click.echo(f"{'dirty':<10}yes — head does not describe what the run actually saw")
    line("base", "base")
    line("config", "config")
    line("model", "model")
    # The label, not the key. `--by subject` groups on the 32-hex identity, because two runs that
    # composed different prompts must not merge into one row however alike they read; a person
    # asking what one run measured wants the sentence.
    line("subject", "subject_label")
    approval = record.get("approval")
    if isinstance(approval, dict) and approval.get("by"):
        watched = "attended" if approval.get("attended") else "unattended"
        click.echo(f"{'approved':<10}{approval['by']}  ({watched})")
    line("ci actor", "ci_actor")
    line("identity", "identity")
    args = record.get("args")
    if isinstance(args, dict) and args:
        click.echo(f"{'args':<10}" + "  ".join(f"{k}={v}" for k, v in sorted(args.items())))
    tokens = record.get("tokens")
    cost = record.get("cost_usd")
    if tokens is not None or cost is not None:
        spent = f"${float(cost):.4f}" if isinstance(cost, (int, float)) else "unmeasured"
        click.echo(f"{'spend':<10}{spent}  ({tokens} tokens, {record.get('wall_seconds', '?')}s)")
    turns = record.get("turns")
    if turns is not None:
        calls = record.get("tool_calls")
        by_name = (
            ", ".join(f"{name} {n}" for name, n in sorted(calls.items())) if isinstance(calls, dict) else ""
        )
        click.echo(f"{'turns':<10}{turns}" + (f"  ({by_name})" if by_name else ""))
    findings = record.get("findings")
    if isinstance(findings, dict) and findings.get("count"):
        click.echo(f"{'findings':<10}{findings['count']}")
        for item in findings.get("items") or []:
            if isinstance(item, dict):
                where = f"{item.get('path')}:{item.get('line')} " if item.get("path") else ""
                click.echo(f"          {where}{item.get('id', '')}: {item.get('message', '')}")
    # The steps, so the line above can be checked against what actually ran: which step went red,
    # and what it said. A verdict without its steps is a number to take on trust.
    steps = record.get("steps")
    if isinstance(steps, list) and steps:
        click.echo("steps")
        for step in steps:
            if not isinstance(step, dict):
                continue
            label = f"{step.get('step', '?')}"
            state = str(step.get("status", "?"))
            if step.get("reason"):
                state += f"  ({step['reason']})"
            if step.get("decided") is False:
                state += "  decided nothing"
            # The label is data (`step=` names are the caller's), so it gets at least one space
            # rather than the fixed-width pad the short fixed labels above can rely on.
            click.echo(f"  {label}{' ' * max(1, 10 - len(label))}{state}")
            step_findings = step.get("findings")
            if isinstance(step_findings, dict):
                for item in step_findings.get("items") or []:
                    if isinstance(item, dict):
                        where = f"{item.get('path')}:{item.get('line')} " if item.get("path") else ""
                        click.echo(f"            {where}{item.get('id', '')}: {item.get('message', '')}")
    from .ai.transcript import TranscriptWriter

    transcript = TranscriptWriter(run_id).path()
    if transcript.exists():
        click.echo(f"{'session':<10}{transcript}  (per-turn transcript)")


def _attempts_from(artifact: str) -> tuple[tuple[Any, Any], ...]:
    """Earlier attempts to resume from, or none.

    A path that does not exist is a REFUSAL rather than a silent fresh start. Somebody who typed
    `--resume` and got a clean run would be paying for the restart they asked to avoid, and would
    have no way to tell from the output that it had happened.
    """
    if not artifact:
        return ()
    from .platform.artifacts import read_changeset, read_verdict

    try:
        changeset = read_changeset(artifact)
    except Exception as e:  # noqa: BLE001 - the message names the path, which is what a user needs
        raise click.ClickException(f"--resume {artifact}: could not read an attempt there ({e})") from e
    # A verdict is optional and its absence is reported to the model as "never tested", never as a
    # pass — `attempts._verdict` is where that distinction is written down.
    try:
        verdict = read_verdict(artifact)
    except Exception:  # noqa: BLE001 - an attempt without a verdict is a real, ordinary case
        verdict = None
    return ((changeset, verdict),)


def _history_line(verify: Any, tampered: list[Any], acknowledged: list[Any] = []) -> str:  # noqa: B006
    """Whether the numbers just printed came from a history that is still append-only.

    Its own function because both report shapes have to end with it. It was written once, inside
    the grouped-table branch, and the richer report added beside it would have inherited every
    number and none of the caveat.

    An acknowledged rewrite is named in the footer too, because "append-only" would then be a
    sentence with an exception somebody has to scroll up to find (#295). The default is a shared
    empty list that is never mutated, so the two older callers stay as they were.
    """
    if not callable(verify):
        # The file store keeps no history, so there is nothing to verify — and saying nothing
        # would read as verified. Absent is not zero, for evidence as much as for numbers.
        return "history   unverifiable (file store keeps no history; tamper-evidence needs the git ledger)"
    noted = f", {len(acknowledged)} rewrite(s) acknowledged by name" if acknowledged else ""
    if tampered:
        return f"history   NOT APPEND-ONLY: {len(tampered)} record(s) rewritten (see above){noted}"
    if acknowledged:
        return (
            f"history   append-only across the retained chain, except {len(acknowledged)} rewrite(s) "
            f"acknowledged by name (see above)"
        )
    return "history   append-only across the retained chain"


def _ledger_line(ledger: Any) -> str:
    """Which ref the numbers above were read from, and how far it is from the other one.

    A clone that never wrote the branch reads the remote-tracking ref, and a laptop that never
    pulled reads a local branch the remote has moved past; either way the page is honest only if
    it says so. A count it cannot take -- no remote copy fetched, or no local branch -- renders as
    a dash, never as "0 behind" (#307).
    """
    diverged = getattr(ledger, "divergence", None)
    if not callable(diverged):
        return "ledger    file store (one directory; nothing to diverge from)"
    found = diverged()
    remote = f"{ledger.remote}/{ledger.branch}"
    if found.read != ledger.ref:
        return f"ledger    read {found.read}; no local branch yet (`history --pull` creates one)"
    if found.local_only is None or found.remote_only is None:
        return f"ledger    read {found.read}; {remote} — (not fetched)"
    return (
        f"ledger    read {found.read}; {found.local_only} record(s) here not on {remote}, "
        f"{found.remote_only} there not here"
        + ("  (`history --pull` brings them)" if found.remote_only else "")
    )


def _around_report(records: list[dict[str, Any]], around: str, subject: str) -> None:
    """`report --around`: resolve the merge, pick the subject, cut the windows, compare, render.

    Composing and translating, none of it deciding: which records fall on which side is
    `windows_around`'s, the arithmetic and the epoch refusal are `compare()`'s, and the text is
    `metrics.around_lines`'s. What is here is the three refusals a person can act on -- a merge
    that is not one, a subject nothing resolves, a subject nobody recorded -- each by name.
    """
    from . import metrics
    from .platform.ledger.store import MIN_RUNS, LedgerError, compare, windows_around

    # Loaded once and only if a question needs it -- a pull request number, or a subject to
    # derive -- because loading the module registers its workflows, and twice is a duplicate.
    loaded: dict[str, Any] = {}

    def module() -> Any:
        if "lockstep" not in loaded:
            loaded["lockstep"], _recorder = _default_lockstep()
        return loaded["lockstep"]

    sha, when = _resolve_merge(around, module)
    key = subject or _subject_of_merge(sha, module)
    before, after = windows_around(records, ts=when, subject=key)
    if not before and not after:
        raise click.ClickException(
            f"no record carries the subject {key!r} on either side of {sha[:12]}. A subject is the "
            f"first word of `history --explain`'s `subject` line (`review/security`) or its key; "
            f"`report --by subject --by-kind` lists the ones this ledger holds."
        )
    try:
        rows = compare(before, after)
    except LedgerError as e:
        raise click.ClickException(str(e)) from None
    for line in metrics.around_lines(
        rows,
        subject=key,
        merge=sha[:12],
        when=when.isoformat(timespec="seconds"),
        before_runs=len(before),
        after_runs=len(after),
        min_runs=MIN_RUNS,
    ):
        click.echo(line)


def _resolve_merge(around: str, module: Callable[[], Any]) -> tuple[str, Any]:
    """A merge, as a commit and the tz-aware moment it landed. `#N` or a bare number asks the
    bound host for the pull request's merge commit and is refused by name when it was not
    merged; anything else is a commit git can show, and its committer date is the moment."""
    import re as _re
    import subprocess
    from datetime import datetime

    if _re.fullmatch(r"#?\d+", around):
        number = int(around.lstrip("#"))
        try:
            host: Any = _bound_scm(module())
        except Exception as e:  # noqa: BLE001 - the refusal names the reason, whatever raised it
            raise click.ClickException(f"cannot resolve pull request {number}: {e}") from None
        lookup = getattr(host, "merged_change", None)
        if lookup is None:
            raise click.ClickException(
                f"{type(host).__name__} cannot say when a pull request merged; pass the merge commit instead"
            )
        merged = asyncio.run(lookup(number))
        if merged is None:
            raise click.ClickException(
                f"pull request {number} is not merged, or is not a pull request; `report --around` "
                f"compares the runs before a merge with the runs after it, so there has to be one"
            )
        sha, merged_at = merged
        return sha, datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    shown = subprocess.run(
        ["git", "show", "-s", "--format=%H%n%cI", f"{around}^{{commit}}"], capture_output=True, text=True
    )
    if shown.returncode != 0:
        raise click.ClickException(f"{around!r} is not a commit git can show: {shown.stderr.strip()}")
    sha, iso = shown.stdout.strip().splitlines()[:2]
    return sha, datetime.fromisoformat(iso)


def _subject_of_merge(sha: str, module: Callable[[], Any]) -> str:
    """The subject a merged change is about, from the one declared `Improvable` body it touched.
    Refused by name when it touched none, or more than one: a subject nobody stated is not one to
    guess at, and `--subject` is the line that states it."""
    import subprocess

    touched = subprocess.run(
        ["git", "show", "--name-only", "--format=", sha], capture_output=True, text=True
    ).stdout.split()
    declared = tuple(getattr(module(), "improve", ()) or ())
    hits = [body for body in declared if body.body in touched]
    if len(hits) == 1:
        return str(hits[0].label)
    bodies = ", ".join(body.body for body in declared) or "(none declared)"
    why = "more than one" if hits else "no"
    raise click.ClickException(
        f"{sha[:12]} touched {why} declared Improvable body (declared: {bodies}), so its subject "
        f"cannot be derived; pass `--subject <strategy id or key>` to say which runs to compare"
    )


def _report_host() -> tuple[Any, str]:
    """The host `report --scm` asks, or None and the reason it could not be found. Never raises.

    `_bound_scm`, not `GitHubScm()`: on a GitLab project, naming the GitHub adapter here would
    read nothing while claiming to have tried. Loaded once and shared by the two questions the
    flag asks, because loading the module twice would register its workflows twice.
    """
    try:
        lockstep, _recorder = _default_lockstep()
        return _bound_scm(lockstep), ""
    except Exception as e:  # noqa: BLE001 - a report degrades with a reason, it does not fail
        return None, str(e)


def _bundles_line(host: Any, records: list[dict[str, Any]], ledger: Any, *, reason: str) -> str:
    """How many run records are still in artifacts the host holds, or a dash and why.

    The branch is not the whole record: a review job holding `contents: read` can only bundle its
    record into an artifact, and fifty of them sat there on a 30-day clock while `report` presented
    eight paid runs as everything (#294). So the footer says how many are outstanding, by the run
    id a record and an artifact both carry -- no download -- and a count it cannot take renders as
    a dash carrying the reason, never as zero.
    """
    from .platform.ledger.reconcile import RUN_ARTIFACT, outstanding

    if host is None:
        return f"bundles   — ({reason})"
    listing = getattr(host, "run_artifacts", None)
    if listing is None:
        return f"bundles   — ({type(host).__name__} cannot list run artifacts)"
    notes = getattr(ledger, "absorbed_runs", None)
    try:
        found = outstanding(listing(RUN_ARTIFACT), records, absorbed=notes() if callable(notes) else ())
    except Exception as e:  # noqa: BLE001 - a report degrades with a reason, it does not fail
        return f"bundles   — (not counted: {e})"
    gone = f", {len(found.expired)} expired before anyone absorbed them" if found.expired else ""
    return (
        f"bundles   {len(found.pending)} outstanding under `{RUN_ARTIFACT}`{gone}; "
        f"`history --from-artifacts {RUN_ARTIFACT} --push` absorbs them"
    )


def _with_delivery(report: Any, host: Any, reason: str) -> Any:
    """`report` plus what the host says happened to the work, or `report` and a printed reason.

    Never raises. A metrics page is a document somebody reads, and the alternative to "we could not
    reach the host" is not "no page" — it is a page that quietly omits a section, which is the
    failure `Measured` exists to prevent, one level up.

    `delivery_rows` is asked for with `getattr` rather than declared on the `Scm` port: the local
    git host has no pull requests at all, and obliging every host to answer a reporting question
    would put a method on the port that most of them can only refuse.
    """
    from dataclasses import replace as _replace

    from . import metrics

    if host is None:
        click.echo(f"delivery   skipped: {reason}", err=True)
        return report
    try:
        rows = getattr(host, "delivery_rows", None)
        if rows is None:
            click.echo(
                f"delivery   skipped: {type(host).__name__} cannot list pull requests and issues",
                err=True,
            )
            return report
        pulls, issues = rows()
    except Exception as e:  # noqa: BLE001 - a report degrades with a reason, it does not fail
        click.echo(f"delivery   skipped: {e}", err=True)
        return report
    return _replace(report, delivery=metrics.delivery(pulls, issues))


@main.command(name="report")
@click.option(
    "--by",
    "group_by",
    default="kind",
    type=click.Choice(["kind", "workflow", "model", "strategy", "aspect", "status", "subject", "actor"]),
    show_default=True,
    help="What one row aggregates over. `actor` also turns the full report's who-and-how into the spread.",
)
@click.option(
    "--names",
    is_flag=True,
    help="Name askers instead of the stable pseudonyms. The signal is the spread, not the person.",
)
@click.option("--format", "fmt", default="table", type=click.Choice(["table", "json"]), show_default=True)
@click.option(
    "--by-kind",
    "grouped",
    is_flag=True,
    help="The original grouped table, one row per --by value, instead of the full report.",
)
@click.option(
    "--html",
    "html_path",
    default="",
    help="Also write a standalone page here — inline style, inline SVG, nothing to fetch.",
)
@click.option(
    "--scm",
    "with_scm",
    is_flag=True,
    help="Also ask the host what happened to the work: merges, and how long issues stayed open.",
)
@click.option(
    "--around",
    default="",
    metavar="SHA|#PR",
    help="Compare the runs after this merge with the runs before it, on one subject. No verdict.",
)
@click.option(
    "--subject",
    default="",
    metavar="STRATEGY|KEY",
    help="--around: the subject both windows share (`review/security`, or a subject key). Derived "
    "from the merged change's declared Improvable body when omitted.",
)
def report_cmd(
    group_by: str,
    names: bool,
    fmt: str,
    grouped: bool,
    html_path: str,
    with_scm: bool,
    around: str,
    subject: str,
) -> None:
    """What the ledger adds up to: runs, failures, spend, effort, and what it keeps finding.

    Reads whichever store this repository records into — the orphan branch in a git repository,
    the file store elsewhere, or whatever the module bound. The numbers follow the ledger's own
    discipline: absent is not zero, so a number nobody measured renders as a dash carrying its
    denominator rather than as a reassuring 0.

    `--scm` is opt-in because it needs a token and a network call, and because evidence this
    framework wrote and evidence it asked somebody else for are kept apart on the page.

    `--by actor` is the one `--by` value that shapes the full report: it turns the who-and-how
    section into a table per asker -- outcome mix, turns and spend per success, findings per run --
    and the spread between askers, each number with the runs it came from. Askers are pseudonyms
    unless `--names` is passed, because a report that reads as a leaderboard is gamed or resented.

    `--around <merge>` is the question the improve loop could not answer until Phase 7: did the
    runs after a merged proposal differ from the runs before it? Two windows on one subject, each
    metric before, after and delta, each window with its run count, and no verdict -- whether a
    lower failure rate means the change helped is the reader's call (`GATE-LEDGER-2`).
    """
    from . import metrics
    from .platform.ledger.store import summarize

    ledger = _ledger()
    reader = getattr(ledger, "records", None)
    if reader is None:
        raise click.ClickException(
            f"{type(ledger).__name__} cannot list records; report needs a store that can"
        )
    records = reader()
    if around:
        _around_report(records, around, subject)
        return
    if not records:
        click.echo("no records yet; the first run that writes a ledger record creates them")
        return

    # Tamper-evidence at report time: the moment someone reads these numbers is the moment to
    # say whether the history they come from is still append-only. On stderr, so `--format json`
    # keeps a parseable stdout while a human running it still sees the alarm.
    verify = getattr(ledger, "verify", None)
    tampered = verify() if callable(verify) else []
    for problem in tampered:
        click.echo(f"TAMPERED  {problem}", err=True)
    # The rewrites somebody stood behind, printed where the alarm was and on the same stream: a
    # reader sees the same fact, now with a name and a reason attached, rather than nothing where
    # an alarm used to be (#295).
    noted = getattr(ledger, "acknowledged_rewrites", None)
    acknowledged = noted() if callable(noted) else []
    for note in acknowledged:
        click.echo(
            f"acknowledged  commit {note.commit[:12]} rewrote {len(note.lines)} record(s): "
            f"{note.reason}  (by {note.by}, {note.ts})",
            err=True,
        )
    # The host is asked only under `--scm`, because asking needs a token and a network call.
    # Without it the bundles line is a dash that says so: the branch is not presented as the
    # whole record on the strength of a question nobody asked (#294).
    host, reason = (
        _report_host()
        if with_scm
        else (None, "pass --scm to ask the host how many run records are still in artifacts")
    )

    # The full report is the default now. `--format json` and `--by-kind` keep the original two
    # outputs working unchanged: the json shape is somebody's script, and a grouped table is what
    # you want when you already know which column you are reading.
    if fmt == "table" and not grouped:
        # `--by` reaches `summarize` below and nothing on this path, so a person who passed one and
        # got the full report was reading a page that had silently discarded their question. Said
        # rather than fixed by making `--by` change this report: the two outputs answer different
        # questions, and quietly turning one into the other would be a worse surprise than a line.
        # `actor` is the exception, re-decided rather than slipped past: it does not regroup the
        # report, it expands one section of it, which is what the flag's help says.
        by_actor = group_by == "actor"
        if (
            not by_actor
            and click.get_current_context().get_parameter_source("group_by") is not ParameterSource.DEFAULT
        ):
            click.echo(
                f"note      --by {group_by} applies to `--by-kind` and `--format json`, not this report"
            )
            click.echo("")
        report = metrics.build(records, names=names)
        if with_scm:
            report = _with_delivery(report, host, reason)
        for line in metrics.as_text(report, actors=by_actor):
            click.echo(line)
        # On this path too, and not only under `--by-kind`. The footer is what GATE-LEDGER-8
        # asserts, and a richer report that quietly stopped saying whether the history behind it
        # is append-only would be a page of numbers with the one caveat about them removed.
        click.echo(_history_line(verify, tampered, acknowledged))
        click.echo(_ledger_line(ledger))
        click.echo(_bundles_line(host, records, ledger, reason=reason))
        if html_path:
            # Written HERE and not by `metrics`, which is a leaf that may not reach `privileged`.
            # A report carries finding text and run ids, so putting it on disk is a redaction sink.
            #
            # `_atomic` because the alternative to a whole page is not "no page" — it is a
            # truncated one that still opens in a browser and renders as though the numbers simply
            # stopped. A report about evidence should not have that failure mode.
            sink.write_text_atomic(Path(html_path), metrics.as_html(report))
            click.echo("")
            click.echo(f"{'page':<10}{html_path}")
        return

    # The same pseudonyms the full report uses, so `actor-1` here is `actor-1` there. Derived onto
    # copies rather than read off the record, because no record carries an `actor` key: the
    # asker is `approval.by`, `ci_actor` or `identity`, and the store's summariser reads one key.
    stats = summarize(
        metrics.keyed_by_actor(records, names=names) if group_by == "actor" else records, by=group_by
    )
    if fmt == "json":
        payload = {
            key: {
                "runs": stat.runs,
                "failures": stat.failures,
                # Runs with no verdict to count; `failure_rate` is over the rest, or null.
                "unclassified": stat.unclassified,
                "failure_rate": stat.failure_rate,
                "tokens": stat.tokens,
                "cost_usd": stat.cost_usd,
                "mean_cost": stat.mean_cost,
                "seconds": stat.seconds,
            }
            for key, stat in stats.items()
        }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    width = max(len(k) for k in stats)
    click.echo(f"{group_by:<{width}}  runs  failed  tokens      cost      mean")
    for key in sorted(stats):
        stat = stats[key]
        tokens = f"{stat.tokens:>8}" if stat.tokens is not None else f"{'-':>8}"
        cost = f"${stat.cost_usd:>8.4f}" if stat.cost_usd is not None else f"{'-':>9}"
        mean = f"${stat.mean_cost:.4f}" if stat.mean_cost is not None else "-"
        click.echo(f"{key:<{width}}  {stat.runs:>4}  {stat.failures:>6}  {tokens}  {cost}  {mean}")
    unclassified = sum(stat.unclassified for stat in stats.values())
    if unclassified:
        click.echo(
            f"{unclassified} record(s) carry no verdict (written before schema 5 by a workflow that "
            f"returned no Outcome); they are in `runs` and not in `failed`"
        )
    click.echo("")
    click.echo(f"{len(records)} record(s); `in-lockstep history --explain <run>` for any one of them")
    click.echo(_history_line(verify, tampered, acknowledged))
    click.echo(_ledger_line(ledger))
    click.echo(_bundles_line(host, records, ledger, reason=reason))


@main.command(name="improve")
@click.option(
    "--explain",
    is_flag=True,
    help="Read the ledger and say what would stop a proposal. Opens nothing, spends nothing.",
)
@click.option(
    "--model", default="anthropic:claude-sonnet-4-6", help="The drafting model, when lockstep.py routes none."
)
@click.option("--corpus", default="", type=click.Path(), help="Promoted cases. Defaults to evidence/cases.")
@click.option("--budget", type=float, default=None, help="Hard ceiling, in USD.")
@click.option("--approve", is_flag=True, help="You are the human watching this run.")
@click.option("--approved-by", default="", help="Who asked, when nobody is watching. Recorded.")
@click.option(
    "--record/--no-record",
    default=None,
    help="Keep this run's model calls. On by default; --no-record declines.",
)
@click.option(
    "--cassette", default="", type=click.Path(), help="Where to keep it. Must be outside the repository."
)
def improve_cmd(
    explain: bool,
    model: str,
    corpus: str,
    budget: float | None,
    approve: bool,
    approved_by: str,
    record: bool | None,
    cassette: str,
) -> None:
    """Propose a prompt change the record supports, or say what stops one.

    Without a flag this runs `improve/measure` in-process: it reads the ledger for a qualifying
    trend, drafts a change to the one declared body that trend is attributed to, re-asks every
    promoted case that body is evidence for against the draft, and stages the change with its
    scorecard when the draft improved the measurement. It refuses before the first model call
    unless a trend qualifies, the body is writable by grant, and a promoted case fails against the
    body as it stands -- each refusal is `blocked`, exit 3, and costs nothing. Opening the staged
    change is `in-lockstep run improve/propose`, the job that holds a write token.

    `--explain` reads the ledger and names what would stop a proposal. It reaches no model, holds
    no key, writes no ledger record and opens nothing.
    """
    from . import metrics

    if not explain:
        _improve_measure(
            model=model,
            corpus=corpus,
            budget=budget,
            approve=approve,
            approved_by=approved_by,
            record=record,
            cassette=cassette,
        )
        return

    lockstep, _recorder = _default_lockstep()
    # WITH the lockstep, unlike `report` and `_explain_run`, which call `_ledger()` bare. Every
    # writer passes the bound store, so a reader that does not is reading a different ledger than
    # the one the runs went into. Not fixed for those two here; this one is new and starts right.
    ledger = _ledger(lockstep)
    reader = getattr(ledger, "records", None)
    if reader is None:
        raise click.ClickException(
            f"{type(ledger).__name__} cannot list records; improve needs a store that can"
        )
    records = reader()
    if not records:
        # Not a census in which nothing recurs, which is a different and far more reassuring
        # sentence than "there is no evidence here at all".
        click.echo("no records yet; the first run that writes a ledger record creates them")
        return

    trends = metrics.recurring(records)
    for line in metrics.as_trend_text(trends):
        click.echo(line)
    click.echo("")

    # The join between a finding id and the text that might answer it. Done here rather than in
    # `metrics`, which is a leaf that may not know what an `Improvable` is — and done by exact
    # membership, never by the shape of the id.
    declared = tuple(getattr(lockstep, "improve", ()) or ())
    guard = lockstep.guard
    claimed: set[str] = set()
    for body in declared:
        mine = [t for t in trends if body.answers_for(t.finding)]
        claimed.update(t.finding for t in mine)
        click.echo(f"body      {body.body}")
        click.echo(f"          {body.label}, verb {body.verb}, answers {', '.join(body.answers) or '—'}")
        if mine:
            # Every match, not the loudest one. `claimed` removes all of them from the
            # unattributed count below, so printing one would leave the two halves not summing
            # to the census — a finding silently attributed to a body nothing showed it against.
            for trend in sorted(mine, key=lambda t: -t.runs):
                click.echo(
                    f"          attributed: {trend.finding}  ({trend.runs} of {trend.considered} run(s))"
                )
        else:
            click.echo("          attributed: —  (no recorded finding matches what it answers)")
        refusal = guard.check_path(body.body)
        if refusal is not None and guard.check_path(body.body, workflow_id="improve/propose") is None:
            # Named by a tier AND lifted for exactly the workflow that writes: the state
            # GATE-IMPROVE-3 asks for, and the one `improve` proceeds under.
            click.echo(f"guard     granted to improve/propose — tier {refusal.tier}, rule {refusal.rule}")
        elif refusal is not None:
            click.echo(f"guard     refused — tier {refusal.tier}, rule {refusal.rule}; no grant lifts it")
            if refusal.tier == 1:
                # `.lockstep/prompts/` was the documented home for a house body, and it is
                # deny-always: a lens there is out of the loop's reach by construction (#311).
                click.echo(
                    "          a tier-1 path can never be granted; a body the loop may propose to "
                    "lives in `prompts/` at the repository root"
                )
        else:
            # Said this way on purpose. `prompts/` in tier 2 is anchored at the repository root, so
            # a body under `src/in_lockstep/prompts/` matches neither tier and is writable because
            # nothing names it — not because anything granted it. Printing a bare "permitted" here
            # is how the next change comes to believe the loop has permission to write there.
            click.echo("guard     permitted by omission — no tier names this path, so nothing granted it")

    unclaimed = [t for t in trends if t.finding not in claimed]
    if not declared:
        click.echo("body      —  (this lifecycle declares no Improvable, so nothing is attributed)")
    if unclaimed:
        click.echo(f"          {len(unclaimed)} finding id(s) answer to no declared body; attributed to —")
    click.echo("")

    ceiling = lockstep.declared_ceiling()
    # Parsed the way `_refuse_exhausted_daily_ceiling` parses it, not echoed. That function treats
    # a non-numeric value as no ceiling at all, so printing the raw string under a heading that says
    # "what a proposal run would meet" would show an unenforced variable as a control in force,
    # which is the one thing this screen must never do.
    raw_daily = os.environ.get("IN_LOCKSTEP_DAILY_LIMIT", "").strip()
    if not raw_daily:
        daily = "—  (IN_LOCKSTEP_DAILY_LIMIT is not set)"
    else:
        try:
            daily = f"${float(raw_daily):.2f} usd"
        except ValueError:
            daily = f"—  ({raw_daily!r} is not a number; not enforced)"

    click.echo("ceilings  what a proposal run would meet, in the order it would meet them")
    # Two things at once. The count is a dash because nothing here asked the host, and an unmeasured
    # ceiling is one that could stop a run — rendering it 0 would turn "nobody counted" into "there
    # is nothing open". And the declared number is printed INSIDE the command that enforces it:
    # `gate` takes its ceiling from `--max` and deliberately never loads `.lockstep/lockstep.py`,
    # so the two can only agree if the reader is handed the flag rather than left to supply it.
    max_open = getattr(lockstep, "max_open_proposals", 1)
    click.echo(
        f"  open proposals  max {max_open}, open now —  (not counted here; "
        f"`in-lockstep gate --open-proposals <workflow> --max {max_open}` asks the host)"
    )
    # A dimension nobody set is left off rather than printed as `None`, and a Budget with nothing
    # set at all is a dash. `Budget()` is four `None`s, and rendering that as `$0.0000` would say
    # this run is capped at nothing when it is capped at nothing in the other sense.
    dimensions = [
        f"${ceiling.usd:.4f} usd" if ceiling.usd is not None else "",
        f"{ceiling.tokens:,} tokens" if ceiling.tokens is not None else "",
        f"{ceiling.wall_seconds:.0f}s wall" if ceiling.wall_seconds is not None else "",
        f"{ceiling.turns} turns" if ceiling.turns is not None else "",
    ]
    stated = ", ".join(d for d in dimensions if d)
    click.echo(f"  budget          {stated or '—  (this lifecycle declares no ceiling)'}")
    click.echo(f"  daily           {daily}")
    click.echo("")
    click.echo("opens     nothing here. `in-lockstep improve` runs improve/measure, which refuses before")
    click.echo("          the first model call unless a trend qualifies, the body is granted, and a")
    click.echo("          promoted case fails against it; `run improve/propose` opens what it staged.")


def _improve_measure(
    *,
    model: str,
    corpus: str,
    budget: float | None,
    approve: bool,
    approved_by: str,
    record: bool | None,
    cassette: str,
) -> None:
    """`in-lockstep improve`: the measuring half, in-process, with shipped defaults for what the
    module left unbound -- the same composition `implement` does for its adapter."""
    from .adapters.ai import AiImprove, Draft, Measure
    from .core.improve import Improver
    from .core.spend import Budget
    from .core.workflow import get
    from .improver import CorpusImprover
    from .middleware.approval import ApprovalGate
    from .workflows import improve as improve_workflows

    lockstep, recorder = _default_lockstep()
    keeping = _recording(record=record)
    if budget is not None:
        lockstep.budget = Budget(usd=budget)
    source = click.get_current_context().get_parameter_source
    # The flag wins when typed; otherwise the module's route, and the CLI's default only when the
    # module routed nothing -- so a repository that chose its drafting model is not overridden by
    # a default it never saw.
    if source("model") is not ParameterSource.DEFAULT or "improve" not in lockstep.models.routes:
        lockstep.models.route("improve", model)
    if not lockstep.container.has(Draft):
        adapter = AiImprove()
        lockstep.bind(Draft, adapter)
        lockstep.bind(Measure, adapter)
    if not lockstep.container.has(Improver):
        where = Path(corpus) if corpus else Path(lockstep.repo.root) / "evidence" / "cases"
        lockstep.bind(Improver, CorpusImprover(where))
    if get(improve_workflows.MEASURE) is None:
        improve_workflows.register()
    approval = _approval(approve, approved_by)
    if approval.granted and not any(getattr(m, "provides_approval", False) for m in lockstep.middleware):
        lockstep.middleware = [*lockstep.middleware, ApprovalGate()]
    entry = get(improve_workflows.MEASURE)
    click.echo("opens     nothing here; `in-lockstep run improve/propose` opens what this stages")
    _run_registered(
        lockstep,
        recorder,
        entry,
        (),
        False,
        approval,
        record=keeping,
        asked=record is True,
        cassette=cassette,
    )


@main.command(name="show-workflow")
@click.argument("name", default="")
@click.option(
    "--registered", is_flag=True, help="Print what this repository has registered, not the shipped source."
)
def show_workflow_cmd(name: str, registered: bool) -> None:
    """Print the source of a process the framework ships. Reads nothing, spends nothing.

    The answer to "what does `/implement` actually do?" that does not require owning a copy of it.
    `init` used to append ~560 lines of workflow source into the adopter's module so it could be
    read and edited; the reading was the part people wanted and the owning was what made every fix
    land twice and never reach a repository that had already scaffolded. This is the reading half
    on its own.

    Printed from `inspect.getsource` on the module that is actually imported, so what appears here
    is what runs -- not a rendering of it, and not a second copy that can drift. That is the whole
    property, and it is why this replaced `init --eject` rather than joining it.

    NAME is a family (`implement`, `fix`) or a single workflow id (`implement/propose`). With no
    argument it lists what can be shown.

    `--registered` answers the neighbouring question: not what the framework ships, but what THIS
    repository has put in force -- which is different the moment somebody writes their own, and is
    the case where reading the shipped source would mislead.
    """
    import inspect

    from .core.workflow import registered as registered_workflows

    if registered:
        # Load the lifecycle module first. Without it this reported `selfcheck` and nothing else --
        # the framework's own registration and none of the repository's -- which is the exact
        # opposite of what the flag says it answers.
        _default_lockstep()
        entries = sorted(registered_workflows(), key=lambda r: r.id)
        if not entries:
            click.echo("nothing registered; a lifecycle module is what puts a workflow in force")
            return
        for entry in entries:
            click.echo(f"{entry.id}  ({entry.module}.{entry.name})")
        return

    from .workflows import fix as fix_workflows
    from .workflows import implement as implement_workflows
    from .workflows import improve as improve_workflows

    families = {"implement": implement_workflows, "fix": fix_workflows, "improve": improve_workflows}
    if not name:
        click.echo("families")
        for family, module in families.items():
            ids = sorted(_workflow_ids(module))
            click.echo(f"  {family:12} {', '.join(ids)}")
        click.echo("")
        click.echo("`show-workflow <family>` prints a whole module; `show-workflow <id>` prints one.")
        click.echo("`--registered` prints what this repository has in force instead.")
        return

    if name in families:
        click.echo(inspect.getsource(families[name]))
        return

    family = name.split("/")[0]
    chosen = families.get(family)
    if chosen is None or name not in _workflow_ids(chosen):
        known = sorted(i for m in families.values() for i in _workflow_ids(m))
        raise click.ClickException(
            f"no shipped workflow named {name!r}. Shipped: {', '.join(known)}. "
            f"For one this repository declares itself, `show-workflow --registered` names it and "
            f"the module it came from."
        )
    click.echo(inspect.getsource(_workflow_function(chosen, name)))


def _workflow_ids(module: Any) -> set[str]:
    """The ids a shipped module claims, read off its `register()` rather than a list kept beside it.

    A second list would be a second thing to forget: `register()` is what actually claims them.
    """
    import inspect

    return set(re.findall(r'workflow\(id="([^"]+)"\)', inspect.getsource(module.register)))


def _workflow_function(module: Any, workflow_id: str) -> Any:
    import inspect

    source = inspect.getsource(module.register)
    match = re.search(rf'workflow\(id="{re.escape(workflow_id)}"\)\((\w+)\)', source)
    if match is None:  # pragma: no cover - the caller checked membership first
        raise click.ClickException(f"{workflow_id} is not registered by {module.__name__}")
    return getattr(module, match.group(1))


@main.command(name="comment")
@click.option("--pr", "number", required=True, type=int, help="The change request to comment on.")
@click.option(
    "--body-file",
    required=True,
    type=click.Path(exists=True),
    help="The comment body, carrying the marker that anchors it — or a directory of them, one per lens.",
)
def comment_cmd(number: int, body_file: str) -> None:
    """Post a body somebody else composed, as a sticky comment — or every body in a directory,
    each as its own.

    Its own command, and its own job, because of the one split this framework's trampoline design
    rests on: the process that calls a model must not also hold the token that writes the
    repository. `review --comment` posts from inside the reviewing command, which is fine on a
    laptop and impossible in CI — `test_no_job_holds_a_provider_key_and_write_access` refuses a job
    with both. So the reviewing job writes what it composed and this posts it, with no provider SDK
    installed at all.

    The marker is read out of the body rather than passed as a flag. That is not economy: the lens
    a chat-ops review ran came out of an untrusted comment and was resolved in the OTHER job, so
    naming it here would mean putting it in a workflow file — the one place `GATE-REVIEW-3` says it
    must never be. `review_comment` already ends with its marker, so the body knows what it is.

    Matched with the writer's own pattern, not a copy. The copy this command kept excluded `-`,
    so a body a hyphenated lens had written was refused here as carrying no marker at all (#275).

    A directory is what the required check's several lenses wrote — `review --aspect a --aspect b
    --comment-out <dir>` leaves `<dir>/a.md` and `<dir>/b.md` — and each is posted under its own
    marker, so four lenses are four comments a reader can tell apart and a later run edits each in
    place. Every body is checked for its marker BEFORE any is posted: a directory with one
    unanchored file posts nothing, rather than three comments and a refusal.
    """
    from pathlib import Path as _Path

    from .platform.hosted import hosted_scm

    given = _Path(body_file)
    files = sorted(given.glob("*.md")) if given.is_dir() else [given]
    if not files:
        raise click.ClickException(f"{body_file} holds no .md body to post")
    anchored: list[tuple[_Path, str, str]] = []
    for file in files:
        body = file.read_text()
        found = _MARKER.search(body)
        if found is None:
            raise click.ClickException(
                f"{file} carries no in-lockstep marker, so a later run could not find this comment "
                f"to edit and would post a second one beside it. Two comments that disagree is worse "
                f"than one that is out of date."
            )
        anchored.append((file, body, found.group(0)))

    scm: Any = hosted_scm(".")
    if not hasattr(scm, "upsert_comment"):
        raise click.ClickException(f"{type(scm).__name__} cannot post comments")
    for _file, body, mark in anchored:
        asyncio.run(scm.upsert_comment(number, body, mark))
        click.echo(f"comment   posted to #{number} under {mark}")


@main.command(name="doctor")
@click.option("--strict", is_flag=True, help="What an organisation puts in a required check.")
@click.option(
    "--format",
    "fmt",
    default="table",
    type=click.Choice(["table", "json"]),
    show_default=True,
    help="json is for fleet scanners; codes and severities are stable.",
)
def doctor_cmd(strict: bool, fmt: str) -> None:
    """Will the target accept this, and are the controls actually in place?"""
    from . import doctor as doctor_module

    report = doctor_module.run(".", strict=strict)
    click.echo(doctor_module.as_json(report) if fmt == "json" else doctor_module.render(report))
    if not report.ok:
        raise SystemExit(EXIT_FAILED)


@main.command(name="provision")
def provision_cmd() -> None:
    """Build the repository's own environment, from what is bound to Provision.

    The step a scaffolded work job runs first (#185): an installed in-lockstep runs from an
    interpreter with nothing of the repository's in it, and the suite a strategy runs to prove a
    change needs the repository's. `uv sync --locked`, `npm ci`, whatever detection bound or the
    module says; `ls` shows which, and where each tool came from. Nothing bound is `not bound`
    and exit 0, never a success: absent is reported as absent. A step that fails, or a tool that
    is nowhere, fails here by name rather than twenty minutes later as a red suite.

    Invoked directly, outside the middleware chain, and deliberately: `ApprovalGate` keys on
    `EXECUTES_CODE`, and this runs before any actor has been verified. What it executes came from
    the trusted ref's module or from detection over the trusted checkout, never from the change
    under review, and `Sandbox` is the control for a deterministic adapter that executes code
    (`Lockstep._refuse_ungated_agency` makes the same argument). Nothing is decided, so nothing
    is recorded. A workflow that must provision mid-run still does `ctx.do(Provision())` through
    the chain, under the run's grant.
    """
    import asyncio
    from types import SimpleNamespace

    from .core.types import Provision

    if os.environ.get(DISABLE_ENV):
        # Before the module is loaded: loading it executes it, and "nothing executes" has to be
        # true of the module's import-time code as well as of the adapter.
        click.echo(f"provision  DISABLED  ({DISABLE_ENV} is set; nothing executes)")
        raise SystemExit(EXIT_BLOCKED)
    lockstep, _ = _default_lockstep()
    if not lockstep.container.has(Provision):
        # Which absence it is. A module is the truth when there is one and detection was not
        # consulted, so a uv.lock beside a module scaffolded before Provision existed is not
        # "detection found no uv.lock".
        source = str(getattr(lockstep, "config_source", "") or "")
        why = (
            "detection found no uv.lock, requirements.txt, package-lock.json or Makefile `deps` "
            "target; bind Provision in .lockstep/lockstep.py to say how"
            if source.startswith("none")
            else f"the module ({source}) binds nothing to Provision; add "
            "lockstep.bind(Provision, CommandProvision([...])) to .lockstep/lockstep.py to say how"
        )
        click.echo(f"provision  not bound  ({why})")
        return
    adapter: Any = lockstep.container.resolve(Provision)
    if isinstance(adapter, Locatable):
        for resolution in adapter.locations(lockstep.repo.root):
            click.echo(f"           {resolution.render()}")
    outcome = asyncio.run(adapter.invoke(SimpleNamespace(repo=lockstep.repo), Provision()))
    for step in getattr(outcome.value, "steps", ()) or ():
        click.echo(f"           ran {step}")
    reason = f"  ({outcome.reason})" if outcome.reason else ""
    click.echo(f"provision  {outcome.status.value}{reason}")
    for finding in outcome.findings[:5]:
        click.echo(f"  {finding.id}: {finding.message}")
    if outcome.blocked:
        raise SystemExit(EXIT_BLOCKED)
    if not outcome.succeeded:
        raise SystemExit(EXIT_FAILED)


@main.command(name="egress-manifest")
def egress_manifest_cmd() -> None:
    """Print the hosts a run may dial, one per line, for the proxy `ENFORCED_*` verifies.

    The framework never enforces destinations itself — the firewall is the host's, probe-verified
    — so this is the bridge between them: the operator feeds this list to the proxy, and
    `IN_LOCKSTEP_EGRESS=enforced` attests that proxy is in front. Computed from the endpoints of
    the providers the module routes to (every registered provider when no routes narrow it), plus
    whatever a bound `EgressPolicy(allow=...)` declares beyond them — an SCM host, a package
    registry someone decided on before an `EXECUTES_CODE` step needed it. Adapters bound with no
    explicit invoker resolve their model from these same routes, so the common case is fully
    visible; only a custom `ProviderRegistry` passed through an explicit `invoker_factory=` is
    not, the same limit `doctor`'s route checks state.
    """
    from .ai.auth import Auth
    from .ai.bootstrap import Model, default_registry
    from .privileged.egress import EgressPolicy

    lockstep, _ = _default_lockstep()
    try:
        registry = default_registry(Auth())
    except Exception as e:
        raise click.ClickException(str(e)) from None
    policy = (
        lockstep.container.resolve(EgressPolicy)
        if lockstep.container.has(EgressPolicy)
        else EgressPolicy.detect()
    )
    routes = dict(getattr(lockstep.models, "routes", None) or {})
    unknown = registry.unknown_endpoints()
    if routes:
        routed = {selected.provider for selected in (Model(m) for m in routes.values())}
        registrations = [
            registry.registration_for(Model(m))
            for m in routes.values()
            if Model(m).provider in registry.names()
        ]
        endpoints = [r.endpoint for r in registrations if r.endpoint]
        unknown = {name: why for name, why in unknown.items() if name in routed}
    else:
        endpoints = list(registry.endpoints())
    for host in policy.manifest(endpoints):
        click.echo(host)
    # On stderr, so the list a proxy is fed stays a list of hosts -- and said, so a manifest with
    # a route missing from it is not read as complete (#309).
    for name, why in unknown.items():
        click.echo(f"unknown   {name}: {why}", err=True)


@main.command(name="apply")
@click.option("--from-artifact", "artifact", required=True, type=click.Path())
@click.option("--dry-run", is_flag=True, help="Check the changeset against the guard; write nothing.")
@click.option("--title", default="", help="Change title. Defaults to the changeset's summary.")
@click.option("--body", default="", help="Change description.")
@click.option("--workflow", "workflow_id", default="implement", help="Names the run-scoped branch.")
@click.option("--run-id", default="", help="Names the run-scoped branch. Defaults to the CI run id.")
@click.option(
    "--base",
    default="",
    help="Open the change against this branch instead of the default — a release line, for a backport.",
)
def apply_cmd(
    artifact: str, dry_run: bool, title: str, body: str, workflow_id: str, run_id: str, base: str
) -> None:
    """Apply a ChangeSet produced by an earlier, unprivileged run.

    This is the privileged half of the two-job split. It holds a write token and never sees a
    provider credential — asserted below rather than left as a convention.

    The artifact crossed a trust boundary to get here, so the path guard runs again over it. A
    previous job having produced it is not a reason to trust it.

    Where it goes is `Scm.open_change`, and the branch it writes to is refused by the framework
    unless it is run-scoped. That refusal matters because the token here is ambient and can write
    any branch: branch protection is the host's half, and this is ours.
    """
    from .platform.scm import DirectPushRefused, GuardRefused, Scm

    changeset = _load_changeset(artifact)
    _guard_or_exit(changeset)

    click.echo(f"{len(changeset.changes)} change(s) pass the guard")
    for change in changeset.changes:
        click.echo(f"  {'delete' if change.deleted else 'write '} {change.path}")
    click.echo("")
    if dry_run:
        click.echo("guard only; nothing was written")
        return

    lockstep, _ = _default_lockstep()
    # The assertion the two-job split rests on, made about this process rather than about the
    # workflow file that started it. A job holding a write token must not be able to reach a
    # provider — and "we did not pass the flag" is a convention, where an empty registry at the
    # moment of writing is a fact.
    _refuse_provider_credential()
    # Nothing in this command spends, and it says so the way a replay does: as a ceiling of zero.
    _declare_zero_ceiling(lockstep)

    scm: Any
    if lockstep.container.has(Scm):
        # `type-abstract` fires because `Scm` is a Protocol, and mypy's rule is about
        # INSTANTIATING an abstract type. The container does not instantiate it — resolving a
        # protocol to a bound implementation is the entire purpose of `core/ports`, and the first
        # place in `src` to do it is here. Ignored locally rather than disabled globally, which
        # would hide the real version of this error everywhere else.
        scm = lockstep.container.resolve(Scm)  # type: ignore[type-abstract]
    else:
        scm = _detect_scm(lockstep.repo.root)
        click.echo(f"scm       {type(scm).__name__}")

    try:
        change = asyncio.run(
            scm.open_change(
                changeset,
                title=title or changeset.summary or "Proposed change",
                body=body,
                ticket=changeset.ticket,
                workflow=workflow_id,
                run_id=run_id or os.environ.get("GITHUB_RUN_ID", "local"),
                # A backport's changeset is relative to its release line, and applied anywhere
                # else it is a different change. Empty keeps the old behaviour.
                base=base,
            )
        )
    except DirectPushRefused as e:
        # A control refusing, not a failure. Exit 3 like every other refusal.
        click.echo(f"refused: {e}")
        raise SystemExit(EXIT_BLOCKED) from None
    except GuardRefused as e:
        click.echo(f"refused: {e}")
        raise SystemExit(EXIT_BLOCKED) from None
    except (OSError, RuntimeError) as e:
        # An error, never a quiet exit 0. A green `apply` is read as "the change landed", and that
        # reading is why this command used to refuse rather than pretend.
        raise click.ClickException(f"nothing was applied: {e}") from None

    click.echo(f"branch    {change.branch}")
    if change.url:
        click.echo(f"change    {change.url}")
    else:
        click.echo("change    (branch only; this SCM does not open pull requests)")


def _detect_scm(root: str) -> Any:
    """`GitHubScm` when there is a GitHub remote to open a change on, else plain git.

    "Is `gh` installed" is the wrong question and was the first one asked. `gh` is on plenty of
    laptops whose current repository has no remote at all, and the failure that produces is a push
    to `origin` that does not exist — reported after the branch was made and the commit written,
    which is the worst moment to discover it. What decides is whether this repository has a remote
    a pull request could be opened against.

    `GitLocal` makes the branch and stops there, which is a complete answer locally: the change is
    committed, it is on a run-scoped branch, and a person pushes it if they want to.
    """
    import shutil
    import subprocess

    from .platform.scm import GitHubScm, GitLocal

    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return GitLocal(root)

    url = remote.stdout.strip() if remote.returncode == 0 else ""
    if url and "github" in url and shutil.which("gh"):
        return GitHubScm(root)
    return GitLocal(root)


def _refuse_provider_credential() -> None:
    """The privileged job must be constructible with no ProviderRegistry.

    Checked rather than assumed, because the whole point of splitting the jobs is that this
    process cannot call a model — and a check that lives only in a comment in a YAML file is one
    a copied workflow silently loses.
    """
    from .ai.auth import Auth
    from .ai.bootstrap import MissingCredential, credentials_for

    try:
        credentials_for(Auth(), "anthropic")
    except MissingCredential:
        return
    except ImportError:  # pragma: no cover - the SDK is not installed, which is the point
        return
    raise click.ClickException(
        "this process can reach a model provider, and the job that writes must not. `apply` is "
        "the privileged half of the two-job split: it holds a write token, so a provider "
        "credential in the same process re-colocates exactly what the split separates. Remove the "
        "credential from this job's environment."
    )


def _review_lenses(lockstep: Any) -> tuple[str, ...] | None:
    """The lens names a review run would actually have, or None when nobody can say.

    The BOUND adapter's, when the module bound one — a repository that replaced its lens map gets
    exactly its own set, which is what carries `AiReview(lenses=...)` through to chat-ops. Read off
    `compositions()`, the declared inspection surface, rather than by reaching for a `lenses`
    attribute: that method exists because attribute sniffing across adapters is inference whose
    failure mode is silence. Its labels are qualified (`review/security`) and the lens is the last
    segment, which is the spelling `--aspect` takes.

    Otherwise the shipped map, because that is precisely what this command binds a few lines below.
    Not a guess about the default: the same source, read early.

    None when the module bound an adapter that is not `Inspectable`. This used to fall back to the
    shipped map, which was a guess about a stranger's adapter: the refusal built on it listed four
    lenses the repository did not have, and now that `--aspect` is resolved here too (#275) that
    list would have refused the lens the adapter actually had. A set nobody stated is not one to
    refuse against, and the two callers say what they do with None. An `Inspectable` adapter that
    declares an empty map is the other case, and it is exactly what it says: no lenses.
    """
    from .adapters.ai.review import Review
    from .prompts.review import LENSES

    if lockstep.container.has(Review):
        adapter: Any = lockstep.container.resolve(Review)
        if not isinstance(adapter, Inspectable):
            return None
        return tuple(sorted(str(label).rsplit("/", 1)[-1] for label in adapter.compositions()))
    return tuple(sorted(LENSES))


@main.command(name="review")
@click.option("--base", default="origin/main", help="What to diff against.")
@click.option("--head", default="HEAD")
@click.option(
    "--aspect",
    "aspects",
    multiple=True,
    default=("security",),
    help="Which lens. Repeatable: every lens named runs, each is recorded, and the worst status exits.",
)
@click.option(
    "--ask",
    default="",
    metavar="COMMENT",
    help="A chat-ops comment body. The lens it names is resolved here, not in a workflow.",
)
@click.option("--model", default="anthropic:claude-sonnet-4-6")
@click.option("--offline", is_flag=True, help="Serve model calls from a cassette. No keys, no spend.")
@click.option(
    "--record/--no-record",
    default=None,
    help="Keep this run's model calls. On by default; --no-record declines.",
)
@click.option(
    "--cassette",
    default="",
    help="Where to read or write a recording. Defaults to the shipped fixture when replaying.",
)
@click.option(
    "--budget",
    type=float,
    default=None,
    help="Hard ceiling, in USD. Without one, lockstep.py must declare a budget.",
)
@click.option(
    "--diff",
    "diff_file",
    default="",
    type=click.Path(),
    help="Review a saved diff instead of asking git for one.",
)
@click.option("--dry-run", is_flag=True, help="Canned answer; proves the wiring, not the prompt.")
@click.option("--comment", "post_comment", is_flag=True, help="Upsert the findings as one sticky PR comment.")
@click.option(
    "--comment-out",
    "comment_out",
    default="",
    type=click.Path(),
    help=(
        "Write the comment body for another job to post — a .md file for one lens, else a directory "
        "that gets <lens>.md per lens. The CI form of --comment."
    ),
)
@click.option(
    "--pr", "pr_number", type=int, default=None, help="The PR to comment on (else detected from CI)."
)
def review_cmd(
    base: str,
    head: str,
    aspects: tuple[str, ...],
    ask: str,
    model: str,
    offline: bool,
    record: bool | None,
    cassette: str,
    budget: float | None,
    diff_file: str,
    dry_run: bool,
    post_comment: bool,
    comment_out: str,
    pr_number: int | None,
) -> None:
    """Review a change with one lens or several, in-process.

    `--diff` reads a patch from a file rather than asking git for one. That is a real use — a
    patch that is not a commit yet, a diff produced somewhere else — and it is also what makes
    this command testable without constructing a repository with a history in it.

    `--aspect` more than once runs every lens named, each as its own run with its own record and
    its own routed model, and exits with the worst status. That loop lived in `lockstep.yml` as
    four shell statements -- `status=0; for aspect in …; do … || status=$?; done; exit $status`
    -- which is lifecycle logic in a file nothing tests (#314, GATE-CI-4). Here it is Python.
    """

    record = _recording(dry_run=dry_run, offline=offline, record=record)
    from .ai.auth import Auth
    from .ai.bootstrap import (
        MissingCredential,
        Model,
        default_registry,
        table_for,
    )
    from .ai.replay import (
        Cassette,
    )
    from .core.spend import Budget

    # The repository's own module, exactly as `run` and `ls` load it. Reviewing is the command
    # that spends money, so it is the last one that should be reading a different configuration
    # from the one `ls` prints — and until this call it built its own `Lockstep` from scratch,
    # which silently discarded every binding, policy contribution, budget and middleware the
    # module declared. "The module you write is the thing that runs" has to hold here first.
    lockstep, recorder = _default_lockstep()

    # `--budget` and `--model` override the module rather than replacing it, and only when the
    # user actually typed them: a flag left at its default must not outrank a declared ceiling.
    source = click.get_current_context().get_parameter_source
    # No default. A flag that silently supplies a ceiling makes GATE-BUDGET-1 unsatisfiable in
    # the one place it matters: every run would have a budget nobody chose, and the refusal could
    # never fire. Declaring it in lockstep.py or typing it are the two ways to mean it.
    if budget is not None:
        lockstep.budget = Budget(usd=budget)
    elif offline or dry_run:
        # A replay or a canned answer bills nothing, and states that as a ceiling of zero rather
        # than asking GATE-BUDGET-1 for an exemption. `_declare_zero_ceiling` says why.
        _declare_zero_ceiling(lockstep)
    # Resolved here, and here is the point. A workflow cannot do it — GitHub's expression
    # language has twelve functions and none of them splits a string — and it must not, because
    # `platform/chatops.py` records the rule: a comment is a command selector, never a command.
    #
    # Before `Auth()`, the registry, the cassette and the bind below, so a comment naming a lens
    # nobody declared costs nothing at all. That ordering is the fix, not a tidiness: the
    # adapter's own refusal arrives after `_run_id`, so an unrecognised aspect reaching the run
    # earns a ledger record — and `blocked` sits inside `failure_rate`'s denominator, so anyone
    # who could comment could deflate this repository's failure rate one typo at a time (#203).
    #
    # `--aspect` takes the same road, for the same reason (#275). It used to reach the adapter,
    # whose refusal is by name but arrives after `_run_id`, so a typo at the terminal wrote a
    # `blocked` record: the framework's word for a control stopping a run, spent on a typo
    # stopping nothing, inside the census #256 and #259 are about. Resolved before the run id
    # exists, a typo is an error and no record — and whatever was typed never reaches
    # `report.marker` on this path either, though the marker now escapes its argument rather than
    # relying on the ordering alone.
    from .platform.chatops import AspectRefused, aspect_from, resolve_aspect

    known = _review_lenses(lockstep)
    try:
        aspects = (
            (aspect_from(ask, known=known),)
            if ask
            else tuple(resolve_aspect(aspect, known=known) for aspect in aspects)
        )
    except AspectRefused as refused:
        # A message, not a traceback — the treatment this command already gives a missing
        # credential or a malformed provider. Exit 1 rather than 3: BLOCKED means a control
        # stopped a run that was otherwise going to happen, and a name that is not a lens is not
        # a run somebody may not have. Nothing is recorded either way, so no rate moves.
        raise click.ClickException(str(refused)) from None
    if len(aspects) > 1 and comment_out and comment_out.endswith(".md"):
        # A comment is one lens's findings for one thread; four lenses in one body is a wall. So
        # several lenses write several bodies, one per lens into a directory `--comment-out`
        # names, each anchored by its own marker — and a path that says it is one file is refused
        # rather than overwritten three times. `--comment` needs no such rule: it posts per lens
        # already, under `review:<lens>`, which is how the required check's four verdicts reach
        # the thread as four comments a reader can tell apart rather than one wall or, as before
        # #345, nothing but a job log.
        raise click.ClickException(
            f"--comment-out {comment_out} is one file and {len(aspects)} lenses were asked for; "
            f"name a directory and each lens writes <lens>.md into it"
        )
    if comment_out and not comment_out.endswith(".md"):
        # A directory for one lens too, so a scaffold that names `review-comments` and later gains
        # a second `--aspect` keeps working without renaming anything on either side.
        Path(comment_out).mkdir(parents=True, exist_ok=True)

    if pr_number and source("base") is ParameterSource.DEFAULT and source("head") is ParameterSource.DEFAULT:
        # `--pr` already meant "the change request this review is about" — it is what `--comment`
        # posts to. Reused rather than joined by a second flag, because two numbers that must agree
        # is a way for them to disagree.
        #
        # Only when neither ref was given. A comment event carries the number and nothing else:
        # `GITHUB_BASE_REF` is empty and `GITHUB_SHA` is the default branch's tip, so a run built
        # from those would diff the default branch against itself and report a clean bill of health
        # for a change it never read. An explicit `--base`/`--head` still wins, so nothing that
        # works today changes.
        scm: Any = _bound_scm(lockstep)
        try:
            refs = asyncio.run(scm.change_refs(pr_number)) if hasattr(scm, "change_refs") else None
        except RuntimeError as e:
            # Could not ask, which is a different sentence from "asked and it is not one": the
            # first names the token or the network, the second names the number (#346).
            raise click.ClickException(
                f"could not ask {type(scm).__name__} what change request {pr_number} points at "
                f"({str(e).strip()[:200]}). Nothing was reviewed."
            ) from None
        if refs is None:
            raise click.ClickException(
                f"{type(scm).__name__} could not say what change request {pr_number} points at. "
                f"Refused rather than guessed: an unreviewed change reported as clean is worse "
                f"than an error."
            )
        base, head = refs

    # `--offline` with nothing else works out of the box, because both halves of a replay ship:
    # the recording, and the diff it was recorded against. A cassette is keyed on the whole
    # composed prompt and therefore on the diff inside it, so a fixture without its diff would
    # replay for nobody — the key would never match anything a user actually has.
    supplied = ""
    if diff_file:
        from pathlib import Path as _Path

        # `patch`, not `source`: this function already binds `source` to `get_parameter_source`,
        # and shadowing it would have broken the "did the user actually type --model" check a few
        # lines above — silently, at runtime, in the command that spends money.
        patch = _Path(diff_file)
        if not patch.is_file():
            raise click.ClickException(f"no diff at {diff_file}")
        supplied = patch.read_text()

    fixture = _shipped_fixture() if offline else None
    # Set only on the demo path, and it is what selects the forgiving replay below. Supplying a
    # range means the user is replaying something of their own, and gets strict keying.
    recorded_request: Any = None
    if fixture is not None:
        cassette = cassette or str(fixture["cassette"])
        source = click.get_current_context().get_parameter_source
        if source("base") is ParameterSource.DEFAULT and source("head") is ParameterSource.DEFAULT:
            demo_diff = str(fixture["diff"])
            base, head, aspects = fixture["base"], fixture["head"], (str(fixture["aspect"]),)
            # Including the model. A recording is portable across provider implementations —
            # that is what the LLMInput/LLMOutput seam buys — but not across model ids, because
            # the id is part of the request being replayed. A repository's own route would
            # otherwise make the shipped fixture unreplayable for the person who most needs it.
            model = str(fixture["model"])
            recorded_request = fixture["request"]
            click.echo(f"replaying the shipped fixture: {fixture['label']}")
        else:
            demo_diff = ""
    else:
        demo_diff = ""
    cassette = cassette or _cassette_default(lockstep, "review")

    auth = Auth()
    try:
        registry = default_registry(auth)
    except MissingCredential as e:
        # Provider registration reads configuration, so a malformed value is caught here rather
        # than at the call. Same treatment either way: a setup problem is a message.
        raise click.ClickException(str(e)) from None
    tape = Cassette.load(cassette)
    # One tape for every lens: a cassette is keyed on the whole composed prompt and each lens
    # composes a different one, so they accumulate rather than overwrite, and one harvest at the
    # end covers the lot. The worst status is what exits, and every lens runs even after one
    # fails: failing fast would let one transient refusal cost the recordings of the lenses behind
    # it, and the required check is where most of the eval corpus comes from.
    worst = Status.SUCCEEDED
    for aspect in aspects:
        if source("model") is ParameterSource.DEFAULT:
            # The lens's own route first, then the verb's -- `routed_model` is the one rule for
            # which key wins, shared with the adapter's own resolution so `ls`, this command and
            # a module-bound adapter agree about the model a lens runs on (#204).
            from .ai.bootstrap import routed_model

            model = routed_model(lockstep.models.routes, "review", aspect) or model
        selected = Model(model)
        table = table_for(registry, selected, _bound_cost_table(lockstep))
        outcome = _review_one(
            lockstep,
            recorder,
            aspect,
            base=base,
            head=head,
            diff=supplied or demo_diff,
            selected=selected,
            table=table,
            tape=tape,
            registry=registry,
            auth=auth,
            recorded_request=recorded_request,
            # One word for where answers come from, decided once above by `_recording`'s refusal
            # of a contradiction; the helper cannot be handed the pair it refused.
            source_of_answers=(
                "dry-run"
                if dry_run
                else "fixture"
                if recorded_request is not None
                else "offline"
                if offline
                else "record"
                if record
                else "live"
            ),
            comment_out=(
                str(Path(comment_out) / f"{aspect}.md")
                if comment_out and not comment_out.endswith(".md")
                else comment_out
            ),
            post_comment=post_comment,
            pr_number=pr_number,
        )
        worst = _worse(worst, outcome.status)

    if worst is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    if worst is not Status.SUCCEEDED:
        raise SystemExit(EXIT_FAILED)


def _worse(a: Status, b: Status) -> Status:
    """The status that exits: a failure outranks a refusal outranks success, so one failed lens
    among four is the check's verdict and a refused one is `blocked` only when nothing failed."""
    rank = {Status.SUCCEEDED: 0, Status.BLOCKED: 1}
    return a if rank.get(a, 2) >= rank.get(b, 2) else b


def _review_one(
    lockstep: Lockstep,
    recorder: Recorder | None,
    aspect: str,
    *,
    base: str,
    head: str,
    diff: str,
    selected: Any,
    table: Any,
    tape: Any,
    registry: Any,
    auth: Any,
    recorded_request: Any,
    source_of_answers: str,
    comment_out: str,
    post_comment: bool,
    pr_number: int | None,
) -> Any:
    """One lens, one run, one record: the body `review` runs once per `--aspect`. Returns the
    outcome, and the caller decides the exit from the worst of them. `source_of_answers` is one
    of dry-run, fixture, offline, record or live: `review` resolved the flags into one word, so
    this takes no pair of flags it would have to refuse a second time."""
    record = source_of_answers == "record"
    from .adapters.ai.review import AiReview, Review
    from .ai.bootstrap import LLMProvider, MissingCredential, caps_for, credentials_for
    from .ai.invoker import AiInvoker, InvokePolicy
    from .ai.replay import DryRunProvider, FixtureProvider, RecordingProvider, ReplayProvider, request_from
    from .privileged.egress import EgressPolicy

    def build_invoker(_ctx: Any) -> AiInvoker:
        provider: LLMProvider
        if source_of_answers == "dry-run":
            provider = DryRunProvider()
        elif source_of_answers == "fixture":
            provider = FixtureProvider(tape, request_from(recorded_request), on_drift=_say_drift)
        elif source_of_answers == "offline":
            provider = ReplayProvider(tape)
        else:
            creds = credentials_for(auth, selected.provider)
            provider = registry.provider_for(selected, creds)
            if record:
                provider = RecordingProvider(provider, tape, Redact())
        return AiInvoker(
            provider,
            model=selected.name,
            # What the registration says the model can do; a schema call or a tool-holding
            # session is refused by name before the first turn (GATE-MODEL-1).
            caps=caps_for(registry, selected),
            cost_table=table,
            spend=_ctx.spend,
            redact=Redact(),
            # The documented opt-out, finally read. `egress.py`'s own refusal says "or bind
            # UnsandboxedEgress deliberately", and ADR 0001 and the crosswalk both name that
            # binding — while nothing resolved it, so the one escape hatch the design offers was
            # a sentence. Absent a binding this is `detect()`, which reads the environment.
            egress=(
                lockstep.container.resolve(EgressPolicy)
                if lockstep.container.has(EgressPolicy)
                else EgressPolicy.detect()
            ),
        )

    # Only if the module did not bind one. A repository that ships its own Review adapter has
    # said something more specific than this default, and the CLI must not overrule it — and it
    # has also chosen its own model, so `selected` below is not a fact about the run.
    review_model_is_ours = not lockstep.container.has(Review)
    if review_model_is_ours:
        lockstep.bind(
            Review,
            AiReview(
                build_invoker,
                repo_root=lockstep.repo.root,
                policy=InvokePolicy.under(
                    lockstep.policy.resolve(),
                    max_turns=_REVIEW_TURNS,
                    max_tokens=_REVIEW_MAX_TOKENS,
                    deadline_seconds=300,
                ),
            ),
        )

    # The tape reaches the run, which is what makes the seam in `ai.bootstrap` fire. Without it
    # `ctx.recording` was None and a module-bound adapter recorded nothing, so the documented
    # extension recipe in `docs/extending.md` -- bind your own Review -- spent money and kept
    # nothing, silently (#264). `run` and `selfcheck` passed a tape from the beginning; these five
    # did not, and the difference was invisible because the CLI's own `build_invoker` wraps for
    # the case where the module binds nothing, which is the case everybody tested.
    ctx = _context(lockstep, _run_id(f"review-{aspect}"), recording=tape if record else None)
    try:
        outcome = asyncio.run(ctx.do(Review(base=base, head=head, aspect=aspect, diff=diff)))
    except BodyNotFound as e:
        # The bound lens points at a body that is not there. A setup problem is a message naming
        # the fix, and `doctor` would have said the same before a credential was resolved (#311).
        raise click.ClickException(str(e)) from None
    except LookupError as e:
        raise click.ClickException(
            f"{e} If this is the shipped fixture, it no longer matches the prompt it was recorded "
            f"against — a prompt or guardrail changed, and re-recording is a real model call."
        ) from None
    except (ImportError, MissingCredential) as e:
        # Both are setup steps with one obvious remedy, not bugs. Forty lines of traceback around
        # the one line that helps is how a fixable problem reads as a broken tool.
        raise click.ClickException(str(e)) from None

    click.echo(
        f"review/{aspect}  {outcome.status.value}"
        + (f"  ({outcome.reason})" if outcome.reason else "")
        + ("" if outcome.decided else "  (decided nothing)")
    )
    for finding in outcome.findings:
        where = f"{finding.path}:{finding.line} " if finding.path else ""
        click.echo(f"  {where}{finding.id}: {finding.message}")

    cost = outcome.cost
    click.echo("")
    click.echo(f"tokens    {cost.input_tokens} in, {cost.output_tokens} out")
    click.echo(f"cost      ${cost.usd:.4f}{_billing_note(cost)}")
    # Where the tape went, on the same terms `run` reports it. A recording nobody is told about is
    # one nobody goes back to. `_report_what_was_kept` holds the rule about when to speak, so
    # these five and `run` cannot drift the way two writers of one decision always do.
    if record:
        _report_what_was_kept(tape, ctx, asked=_asked_to_record(click.get_current_context()))
    _echo_telemetry(recorder)

    # The ledger line the first-value assertion checks. Written even on failure: a run that cost
    # money and produced nothing is exactly the run worth having a record of.
    _write_ledger(lockstep, ctx, outcome, aspect, selected.id if review_model_is_ours else "")

    if comment_out:
        # Written, not posted. The job that called the model must not also hold the token that
        # writes the repository, so in CI the reviewing job composes and a job with no provider SDK
        # posts — `in-lockstep comment --body-file`. Through the sink like every escaping write: a
        # review body carries a model's words about the diff it was given.
        #
        # Atomic, like every other sanctioned write here. A comment body is read by a later job, so
        # a run cancelled mid-write would hand it half a file — and `write_text` is a primitive the
        # sinks scan refuses by name anyway, which is the rule pointing at the better call.
        from pathlib import Path as _Path

        from .platform.report import review_comment

        sink.write_text_atomic(_Path(comment_out), review_comment(aspect, outcome))
        click.echo(f"comment   wrote {comment_out}")

    if post_comment:
        _post_review_comment(lockstep, aspect, outcome, pr_number)

    return outcome


_TRIAGE_DRY_RUN = (
    '{"kind": "bug", "priority": "normal", "reason": "canned dry-run answer; proves the wiring, '
    'not the model.", "missing": [], "acceptance_criteria": [], "labels": [], "comment": ""}'
)


@main.command(name="triage")
@click.option("--ticket", default="", help="A ticket key to read from the tracker, e.g. '#42'.")
@click.option(
    "--ticket-file",
    default="",
    type=click.Path(),
    help="A ticket read off disk — JSON (the eval-corpus shape) or markdown. No tracker, no network.",
)
@click.option("--model", default="anthropic:claude-haiku-4-5", help="Triage is a cheap reading task.")
@click.option("--offline", is_flag=True, help="Serve model calls from a cassette. No keys, no spend.")
@click.option(
    "--record/--no-record",
    default=None,
    help="Keep this run's model calls. On by default; --no-record declines.",
)
@click.option("--cassette", default="", help="Where to read or write a recording.")
@click.option(
    "--budget",
    type=float,
    default=None,
    help="Hard ceiling, in USD. Without one, lockstep.py must declare a budget.",
)
@click.option("--dry-run", is_flag=True, help="Canned answer; proves the wiring, not the prompt.")
def triage_cmd(
    ticket: str,
    ticket_file: str,
    model: str,
    offline: bool,
    record: bool | None,
    cassette: str,
    budget: float | None,
    dry_run: bool,
) -> None:
    """Read one issue and place it: what kind of work, how urgent, what it is missing.

    Read-only and label-only. The analyst reports a decision; acting on it — a comment, a label,
    a duplicate link — is a separate step the caller takes through `TicketSource`, which is why
    the `triage` guardrail denies the issue-writing tools.
    """
    record = _recording(dry_run=dry_run, offline=offline, record=record)
    from .adapters.ai.triage import AiTriage, Triage
    from .ai.auth import Auth
    from .ai.bootstrap import (
        LLMProvider,
        MissingCredential,
        Model,
        caps_for,
        credentials_for,
        default_registry,
        table_for,
    )
    from .ai.invoker import AiInvoker, InvokePolicy
    from .ai.replay import Cassette, DryRunProvider, RecordingProvider, ReplayProvider
    from .core.spend import Budget
    from .privileged.egress import EgressPolicy

    if bool(ticket) == bool(ticket_file):
        raise click.ClickException("pass exactly one of --ticket or --ticket-file")

    lockstep, recorder = _default_lockstep()

    source = click.get_current_context().get_parameter_source
    if budget is not None:
        lockstep.budget = Budget(usd=budget)
    elif offline or dry_run:
        # As `review` does: a run that cannot spend states a zero ceiling.
        _declare_zero_ceiling(lockstep)
    if source("model") is ParameterSource.DEFAULT:
        model = lockstep.models.routes.get("triage", model)

    spec = _triage_spec(ticket, ticket_file, lockstep.repo.root, source=_bound_ticket_source(lockstep))

    auth = Auth()
    try:
        registry = default_registry(auth)
    except MissingCredential as e:
        raise click.ClickException(str(e)) from None
    selected = Model(model)
    table = table_for(registry, selected, _bound_cost_table(lockstep))
    tape = Cassette.load(cassette or _cassette_default(lockstep, "triage"))

    def build_invoker(_ctx: Any) -> AiInvoker:
        provider: LLMProvider
        if dry_run:
            provider = DryRunProvider(_TRIAGE_DRY_RUN)
        elif offline:
            provider = ReplayProvider(tape)
        else:
            creds = credentials_for(auth, selected.provider)
            provider = registry.provider_for(selected, creds)
            if record:
                provider = RecordingProvider(provider, tape, Redact())
        return AiInvoker(
            provider,
            model=selected.name,
            # What the registration says the model can do; a schema call or a tool-holding
            # session is refused by name before the first turn (GATE-MODEL-1).
            caps=caps_for(registry, selected),
            cost_table=table,
            spend=_ctx.spend,
            redact=Redact(),
            egress=(
                lockstep.container.resolve(EgressPolicy)
                if lockstep.container.has(EgressPolicy)
                else EgressPolicy.detect()
            ),
        )

    # Only if the module did not bind one, the same rule `review` follows: a repository that ships
    # its own Triage adapter has said something more specific, and has chosen its own model too, so
    # `selected` below is not a fact about that run.
    triage_model_is_ours = not lockstep.container.has(Triage)
    if triage_model_is_ours:
        lockstep.bind(
            Triage,
            AiTriage(
                build_invoker,
                policy=InvokePolicy.under(
                    lockstep.policy.resolve(), max_turns=1, max_tokens=2048, deadline_seconds=120
                ),
            ),
        )

    # The tape reaches the run, which is what makes the seam in `ai.bootstrap` fire. Without it
    # `ctx.recording` was None and a module-bound adapter recorded nothing, so the documented
    # extension recipe in `docs/extending.md` -- bind your own Review -- spent money and kept
    # nothing, silently (#264). `run` and `selfcheck` passed a tape from the beginning; these five
    # did not, and the difference was invisible because the CLI's own `build_invoker` wraps for
    # the case where the module binds nothing, which is the case everybody tested.
    ctx = _context(
        lockstep, _run_id(f"triage-{spec.key.lstrip('#') or 'issue'}"), recording=tape if record else None
    )
    try:
        outcome = asyncio.run(ctx.do(spec))
    except LookupError as e:
        raise click.ClickException(
            f"{e} If this is a shipped fixture, it no longer matches the prompt it was recorded "
            f"against — a prompt or guardrail changed, and re-recording is a real model call."
        ) from None
    except (ImportError, MissingCredential) as e:
        raise click.ClickException(str(e)) from None

    click.echo(
        f"triage    {outcome.status.value}"
        + (f"  ({outcome.reason})" if outcome.reason else "")
        + ("" if outcome.decided else "  (decided nothing)")
    )
    decision = outcome.value
    if decision is not None:
        click.echo(f"  {decision.kind} / {decision.priority}  {decision.reason}")
        if decision.duplicate_of:
            click.echo(f"  duplicate of {decision.duplicate_of}")
        if decision.labels:
            click.echo(f"  labels: {', '.join(decision.labels)}")
    for finding in outcome.findings:
        where = f"{finding.path} " if finding.path else ""
        click.echo(f"  {where}{finding.id}: {finding.message}")

    cost = outcome.cost
    click.echo("")
    click.echo(f"tokens    {cost.input_tokens} in, {cost.output_tokens} out")
    click.echo(f"cost      ${cost.usd:.4f}{_billing_note(cost)}")
    # Where the tape went, on the same terms `run` reports it. A recording nobody is told about is
    # one nobody goes back to. `_report_what_was_kept` holds the rule about when to speak, so
    # these five and `run` cannot drift the way two writers of one decision always do.
    if record:
        _report_what_was_kept(tape, ctx, asked=_asked_to_record(click.get_current_context()))
    _echo_telemetry(recorder)

    _write_ledger(lockstep, ctx, outcome, "", selected.id if triage_model_is_ours else "", kind="triage")

    if outcome.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    if outcome.status is not Status.SUCCEEDED:
        raise SystemExit(EXIT_FAILED)


def _triage_spec(ticket: str, ticket_file: str, root: str, source: Any = None) -> Any:
    """A `Triage` request from a tracker key or a file. A JSON file may carry the richer eval-corpus
    shape (discussion, criteria_source); a markdown file or a real issue goes through the Ticket
    mapping, so what triage sees offline matches what it sees against a live tracker."""
    import json
    from pathlib import Path as _Path

    from .adapters.ai.triage import Triage

    if ticket_file:
        file = _Path(ticket_file)
        if not file.exists():
            raise click.ClickException(f"no ticket file at {ticket_file}")
        try:
            data = json.loads(file.read_text())
        except ValueError:
            data = None
        if isinstance(data, dict):
            inner = data.get("input")
            payload = inner if isinstance(inner, dict) else data
            return _triage_spec_from_dict(payload, fallback_key=file.stem)
    return Triage.from_ticket(_load_ticket(ticket, ticket_file, root, source=source))


def _triage_spec_from_dict(data: dict[str, Any], *, fallback_key: str) -> Any:
    from .adapters.ai.triage import Triage
    from .platform.tickets import criteria_from

    def _pairs(raw: object) -> tuple[tuple[str, str], ...]:
        out = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                out.append((str(item.get("author", "")), str(item.get("body", ""))))
            else:
                out.append(("", str(item)))
        return tuple(out)

    def _strs(raw: object) -> tuple[str, ...]:
        return tuple(str(x) for x in raw) if isinstance(raw, list) else ()

    description = str(data.get("description") or data.get("body") or "")
    # Criteria the same way a real ticket produces them: an explicit list wins, and when there is
    # none they come from the body's task list — the same `criteria_from` a live `--ticket` goes
    # through, so a JSON dump of an issue triages identically to the issue read from the tracker.
    criteria = _strs(data.get("acceptance_criteria")) or criteria_from(description)
    # An explicit source is honoured (the eval corpus states it); absent, it follows the criteria,
    # matching `Triage.from_ticket`. A filled criteria list under `criteria_source: none` would
    # tell the analyst to treat present criteria as missing.
    criteria_source = str(data.get("criteria_source") or ("description" if criteria else "none"))
    return Triage(
        key=str(data.get("key") or data.get("id") or f"#{fallback_key}"),
        summary=str(data.get("summary") or data.get("title") or ""),
        description=description,
        discussion=_pairs(data.get("discussion")),
        labels=_strs(data.get("labels")),
        acceptance_criteria=criteria,
        criteria_source=criteria_source,
    )


def _post_review_comment(lockstep: Lockstep, aspect: str, outcome: Any, pr_number: int | None) -> None:
    """Put the findings where a person reads them: one sticky PR comment.

    The PR number comes from `--pr` or from CI detection; without one there is nothing to comment
    on, and that is a note rather than a failure — a local `review --comment` with no PR is a
    reasonable thing to type, and refusing it would be surprising. A bound `Scm` that can upsert is
    used, else the GitHub default.
    """
    from .platform.ci import detect as detect_ci
    from .platform.hosted import hosted_scm
    from .platform.report import marker, review_comment
    from .platform.scm import Scm

    ci_env = detect_ci()
    number = pr_number if pr_number is not None else (ci_env.pr_number if ci_env else None)
    if not number:
        click.echo(
            "comment   no PR number (pass --pr, or run in a pull-request pipeline); not posted", err=True
        )
        return

    scm: Any = lockstep.container.resolve(Scm) if lockstep.container.has(Scm) else None  # type: ignore[type-abstract]
    if not hasattr(scm, "upsert_comment"):
        # The detected host's adapter, not GitHub's by name: on a GitLab merge-request pipeline
        # the sticky comment goes to the MR notes API, and hardcoding GitHubScm here would post
        # nowhere while claiming to have tried.
        scm = hosted_scm(lockstep.repo.root)
    body = review_comment(aspect, outcome)
    try:
        asyncio.run(scm.upsert_comment(int(number), body, marker(f"review:{aspect}")))
        click.echo(f"comment   posted to PR #{number}")
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as e:
        # Posting is the last step and the least essential: the review already ran and its record
        # is written, so a failed comment is reported, not raised. The catch is broad on purpose —
        # a `gh` timeout (`SubprocessError`) or a non-JSON response (`ValueError`) must not turn a
        # review that succeeded into a crash, nor swallow the real exit code of one that blocked.
        click.echo(f"comment   could not post to PR #{number}: {e}", err=True)


_RFE_DRY_RUN = (
    '{"title": "Canned dry-run draft", "problem": "canned dry-run answer; proves the wiring, '
    'not the model.", "proposal": "nothing", "acceptance_criteria": [], "open_questions": [], '
    '"labels": []}'
)


@main.command(name="rfe")
@click.option("--idea", default="", help="The rough request, in a sentence or three.")
@click.option(
    "--idea-file",
    default="",
    type=click.Path(),
    help="The rough request read off disk, as plain text or markdown.",
)
@click.option("--ticket", default="", help="An existing feature-kind issue to elaborate, e.g. '#42'.")
@click.option(
    "--create",
    is_flag=True,
    help="File the draft through the bound TicketSource. Off by default: a human reads first.",
)
@click.option("--model", default="anthropic:claude-haiku-4-5", help="Drafting is a cheap reading task.")
@click.option("--offline", is_flag=True, help="Serve model calls from a cassette. No keys, no spend.")
@click.option(
    "--record/--no-record",
    default=None,
    help="Keep this run's model calls. On by default; --no-record declines.",
)
@click.option("--cassette", default="", help="Where to read or write a recording.")
@click.option(
    "--budget",
    type=float,
    default=None,
    help="Hard ceiling, in USD. Without one, lockstep.py must declare a budget.",
)
@click.option("--dry-run", is_flag=True, help="Canned answer; proves the wiring, not the prompt.")
def rfe_cmd(
    idea: str,
    idea_file: str,
    ticket: str,
    create: bool,
    model: str,
    offline: bool,
    record: bool | None,
    cassette: str,
    budget: float | None,
    dry_run: bool,
) -> None:
    """Turn a rough feature idea into a ticket a team could pick up.

    The draft is printed, never filed by the model: an idea is untrusted input and a ticket in
    the tracker is an instruction to future agents, so the `rfe` guardrail denies the
    issue-writing tools and `--create` is the human step that takes the printed draft to
    `TicketSource.create`. Without `--create` this reads, drafts and stops.
    """
    record = _recording(dry_run=dry_run, offline=offline, record=record)
    from pathlib import Path as _Path

    from .adapters.ai.rfe import AiRfe, Rfe
    from .ai.auth import Auth
    from .ai.bootstrap import (
        LLMProvider,
        MissingCredential,
        Model,
        caps_for,
        credentials_for,
        default_registry,
        table_for,
    )
    from .ai.invoker import AiInvoker, InvokePolicy
    from .ai.replay import Cassette, DryRunProvider, RecordingProvider, ReplayProvider
    from .platform.tickets import TicketDraft, TicketSource, TicketType
    from .privileged.egress import EgressPolicy

    lockstep, recorder = _default_lockstep()
    source = click.get_current_context().get_parameter_source
    if budget is not None:
        from .core.spend import Budget

        lockstep.budget = Budget(usd=budget)
    elif offline or dry_run:
        # As `review` does: a run that cannot spend states a zero ceiling.
        _declare_zero_ceiling(lockstep)
    if source("model") is ParameterSource.DEFAULT:
        model = lockstep.models.routes.get("rfe", model)

    if sum(1 for x in (idea, idea_file, ticket) if x) != 1:
        raise click.ClickException("pass exactly one of --idea, --idea-file or --ticket")
    if idea_file:
        file = _Path(idea_file)
        if not file.exists():
            raise click.ClickException(f"no idea file at {idea_file}")
        spec = Rfe(idea=file.read_text(), key=file.stem)
    elif ticket:
        loaded = _load_ticket(ticket, "", lockstep.repo.root, source=_bound_ticket_source(lockstep))
        spec = Rfe.from_ticket(loaded)
    else:
        spec = Rfe(idea=idea)

    auth = Auth()
    try:
        registry = default_registry(auth)
    except MissingCredential as e:
        raise click.ClickException(str(e)) from None
    selected = Model(model)
    table = table_for(registry, selected, _bound_cost_table(lockstep))
    tape = Cassette.load(cassette or _cassette_default(lockstep, "rfe"))

    def build_invoker(_ctx: Any) -> AiInvoker:
        provider: LLMProvider
        if dry_run:
            provider = DryRunProvider(_RFE_DRY_RUN)
        elif offline:
            provider = ReplayProvider(tape)
        else:
            creds = credentials_for(auth, selected.provider)
            provider = registry.provider_for(selected, creds)
            if record:
                provider = RecordingProvider(provider, tape, Redact())
        return AiInvoker(
            provider,
            model=selected.name,
            # What the registration says the model can do; a schema call or a tool-holding
            # session is refused by name before the first turn (GATE-MODEL-1).
            caps=caps_for(registry, selected),
            cost_table=table,
            spend=_ctx.spend,
            redact=Redact(),
            egress=(
                lockstep.container.resolve(EgressPolicy)
                if lockstep.container.has(EgressPolicy)
                else EgressPolicy.detect()
            ),
        )

    # Only if the module did not bind one — the same rule `review` and `triage` follow.
    rfe_model_is_ours = not lockstep.container.has(Rfe)
    if rfe_model_is_ours:
        lockstep.bind(
            Rfe,
            AiRfe(
                build_invoker,
                policy=InvokePolicy.under(
                    lockstep.policy.resolve(), max_turns=1, max_tokens=2048, deadline_seconds=120
                ),
            ),
        )

    # The tape reaches the run, which is what makes the seam in `ai.bootstrap` fire. Without it
    # `ctx.recording` was None and a module-bound adapter recorded nothing, so the documented
    # extension recipe in `docs/extending.md` -- bind your own Review -- spent money and kept
    # nothing, silently (#264). `run` and `selfcheck` passed a tape from the beginning; these five
    # did not, and the difference was invisible because the CLI's own `build_invoker` wraps for
    # the case where the module binds nothing, which is the case everybody tested.
    ctx = _context(
        lockstep, _run_id(f"rfe-{spec.key.lstrip('#') or 'idea'}"), recording=tape if record else None
    )
    try:
        outcome = asyncio.run(ctx.do(spec))
    except LookupError as e:
        raise click.ClickException(
            f"{e} If this is a shipped fixture, it no longer matches the prompt it was recorded "
            f"against — a prompt or guardrail changed, and re-recording is a real model call."
        ) from None
    except (ImportError, MissingCredential) as e:
        raise click.ClickException(str(e)) from None

    click.echo(
        f"rfe       {outcome.status.value}"
        + (f"  ({outcome.reason})" if outcome.reason else "")
        + ("" if outcome.decided else "  (decided nothing)")
    )
    draft = outcome.value
    if draft is not None:
        click.echo("")
        click.echo(f"# {draft.title}")
        click.echo("")
        click.echo(draft.render())
        if draft.labels:
            click.echo("")
            click.echo(f"labels: {', '.join(draft.labels)}")
    for finding in outcome.findings:
        where = f"{finding.path} " if finding.path else ""
        click.echo(f"  {where}{finding.id}: {finding.message}")

    cost = outcome.cost
    click.echo("")
    click.echo(f"tokens    {cost.input_tokens} in, {cost.output_tokens} out")
    click.echo(f"cost      ${cost.usd:.4f}{_billing_note(cost)}")
    # Where the tape went, on the same terms `run` reports it. A recording nobody is told about is
    # one nobody goes back to. `_report_what_was_kept` holds the rule about when to speak, so
    # these five and `run` cannot drift the way two writers of one decision always do.
    if record:
        _report_what_was_kept(tape, ctx, asked=_asked_to_record(click.get_current_context()))
    _echo_telemetry(recorder)

    _write_ledger(lockstep, ctx, outcome, "", selected.id if rfe_model_is_ours else "", kind="rfe")

    if outcome.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    if outcome.status is not Status.SUCCEEDED:
        raise SystemExit(EXIT_FAILED)

    if not create:
        if draft is not None:
            click.echo("")
            click.echo(
                "not filed. Read it, then re-run with --create to file it — or edit and file it yourself."
            )
        return
    if draft is None:
        raise click.ClickException("nothing to file: the run produced no draft")
    tickets: Any = (
        lockstep.container.resolve(TicketSource)  # type: ignore[type-abstract]
        if lockstep.container.has(TicketSource)
        else None
    )
    if tickets is None:
        raise click.ClickException(
            "no TicketSource is bound, so there is nowhere to file the draft. Bind one in "
            "lockstep.py — e.g. lockstep.bind(TicketSource, GitHubIssues())."
        )
    labels = tuple(dict.fromkeys((*draft.labels, "rfe")))
    filed = asyncio.run(
        tickets.create(
            TicketDraft(
                title=draft.title,
                description=draft.render(),
                type=TicketType.STORY,
                labels=labels,
            )
        )
    )
    click.echo("")
    click.echo(f"filed     {getattr(filed, 'key', '') or draft.title}")


_BACKPORT_DRY_RUN = '{"files": [], "summary": "canned dry-run answer; proves the wiring, not the merge."}'


@main.command(name="backport")
@click.option("--target", required=True, help="The release line the change must land on, e.g. 'release-1.2'.")
@click.option(
    "--commit",
    "commits",
    multiple=True,
    help="A commit to pick, oldest first. Repeatable. Without it, commits are found by `Ticket:` trailer.",
)
@click.option(
    "--ticket", default="", help="The work item, e.g. '#42'. Finds the commits when none are named."
)
@click.option(
    "--ticket-file",
    default="",
    type=click.Path(),
    help="A ticket read off disk — JSON or markdown. No tracker, no network.",
)
@click.option("--source", default="HEAD", show_default=True, help="The line the commits live on.")
@click.option("--out", default="", type=click.Path(), help="Write the ChangeSet here for `apply --base`.")
@click.option(
    "--resolve",
    is_flag=True,
    help="Let a model merge a conflicted pick. Off, a conflict stops the run with the manual commands.",
)
@click.option("--model", default="anthropic:claude-sonnet-4-6", help="The conflict resolver, on --resolve.")
@click.option(
    "--approve",
    is_flag=True,
    help="You are the human in the loop for a --resolve run. Attended, local use only.",
)
@click.option("--approved-by", default="", help="Who asked. The unattended form of --approve; recorded.")
@click.option("--offline", is_flag=True, help="Serve model calls from a cassette. No keys, no spend.")
@click.option(
    "--record/--no-record",
    default=None,
    help="Keep this run's model calls. On by default; --no-record declines.",
)
@click.option("--cassette", default="", help="Where to read or write a recording.")
@click.option("--budget", type=float, default=None, help="Hard ceiling, in USD, for a --resolve run.")
@click.option("--dry-run", is_flag=True, help="Canned resolver answer; proves the wiring, not the merge.")
def backport_cmd(
    target: str,
    commits: tuple[str, ...],
    ticket: str,
    ticket_file: str,
    source: str,
    out: str,
    resolve: bool,
    model: str,
    approve: bool,
    approved_by: str,
    offline: bool,
    record: bool | None,
    cassette: str,
    budget: float | None,
    dry_run: bool,
) -> None:
    """Replay merged commits onto a release line. Deterministic first; a model only on conflict.

    The default run is plain `git cherry-pick -x` in a throwaway worktree — no key, no budget, no
    approval, because no model chooses anything. A conflict stops it with the exact commands a
    person would run. `--resolve` binds a conflict resolver instead, and because a model can then
    author file contents, the run needs what every writing spender needs: a budget and an
    approval (`--approve` or `--approved-by`).

    Nothing touches the working tree. The result is a ChangeSet relative to the TARGET line —
    `--out` serializes it, and `apply --from-artifact X --base <target>` opens it against the
    release line through the guard.
    """
    record = _recording(dry_run=dry_run, offline=offline, record=record)
    from .adapters.ai.backport import AiBackportResolver
    from .adapters.backport import Backport, GitBackport
    from .ai.auth import Auth
    from .ai.bootstrap import (
        LLMProvider,
        MissingCredential,
        Model,
        caps_for,
        credentials_for,
        default_registry,
        table_for,
    )
    from .ai.invoker import AiInvoker, InvokePolicy
    from .ai.replay import Cassette, DryRunProvider, RecordingProvider, ReplayProvider
    from .core.spend import Budget
    from .middleware.approval import ApprovalGate
    from .privileged.egress import EgressPolicy

    if ticket and ticket_file:
        raise click.ClickException("pass at most one of --ticket or --ticket-file")
    if not commits and not (ticket or ticket_file):
        raise click.ClickException(
            "name the commits (--commit), or a ticket whose `Ticket:` trailer finds them"
        )

    lockstep, recorder = _default_lockstep()

    param_source = click.get_current_context().get_parameter_source
    if budget is not None:
        lockstep.budget = Budget(usd=budget)
    elif offline or dry_run:
        # As `review` does: a run that cannot spend states a zero ceiling. Only a `--resolve` run
        # binds a spender, but zero is true of a deterministic backport too.
        _declare_zero_ceiling(lockstep)
    if param_source("model") is ParameterSource.DEFAULT:
        model = lockstep.models.routes.get("backport", model)

    # Approval, exactly as `implement` reasons about it — but only a `--resolve` run needs it,
    # because only there does an adapter declare that a model can write.
    approval = _approval(approve, approved_by)
    if approval.granted and not any(getattr(m, "provides_approval", False) for m in lockstep.middleware):
        lockstep.middleware = [*lockstep.middleware, ApprovalGate()]

    resolved_ticket = (
        _load_ticket(ticket, ticket_file, lockstep.repo.root, source=_bound_ticket_source(lockstep))
        if (ticket or ticket_file)
        else None
    )

    selected = Model(model)
    resolver = None
    if resolve:
        auth = Auth()
        try:
            registry = default_registry(auth)
        except MissingCredential as e:
            raise click.ClickException(str(e)) from None
        table = table_for(registry, selected, _bound_cost_table(lockstep))
        tape = Cassette.load(cassette or _cassette_default(lockstep, "backport"))

        def build_invoker(_ctx: Any) -> AiInvoker:
            provider: LLMProvider
            if dry_run:
                provider = DryRunProvider(_BACKPORT_DRY_RUN)
            elif offline:
                provider = ReplayProvider(tape)
            else:
                creds = credentials_for(auth, selected.provider)
                provider = registry.provider_for(selected, creds)
                if record:
                    provider = RecordingProvider(provider, tape, Redact())
            return AiInvoker(
                provider,
                model=selected.name,
                # What the registration says the model can do; a schema call or a tool-holding
                # session is refused by name before the first turn (GATE-MODEL-1).
                caps=caps_for(registry, selected),
                cost_table=table,
                spend=_ctx.spend,
                redact=Redact(),
                egress=(
                    lockstep.container.resolve(EgressPolicy)
                    if lockstep.container.has(EgressPolicy)
                    else EgressPolicy.detect()
                ),
            )

        resolver = AiBackportResolver(
            build_invoker,
            policy=InvokePolicy.under(
                lockstep.policy.resolve(), max_turns=1, max_tokens=16_384, deadline_seconds=300
            ),
        )

    # Only if the module did not bind one, the same rule every verb command follows.
    backport_is_ours = not lockstep.container.has(Backport)
    if backport_is_ours:
        lockstep.bind(Backport, GitBackport(lockstep.repo.root, resolver=resolver))

    key = str(getattr(resolved_ticket, "key", "") or "").lstrip("#")
    # The tape reaches the run, which is what makes the seam in `ai.bootstrap` fire. Without it
    # `ctx.recording` was None and a module-bound adapter recorded nothing, so the documented
    # extension recipe in `docs/extending.md` -- bind your own Review -- spent money and kept
    # nothing, silently (#264). `run` and `selfcheck` passed a tape from the beginning; these five
    # did not, and the difference was invisible because the CLI's own `build_invoker` wraps for
    # the case where the module binds nothing, which is the case everybody tested.
    # `tape` exists only where a resolver was asked for; a deterministic cherry-pick calls no
    # model, so there is nothing to keep and nothing to say about it.
    ctx = _context(
        lockstep,
        _run_id(f"backport-{key or target}"),
        approval,
        recording=tape if (resolve and record) else None,
    )
    spec = Backport(target=target, commits=tuple(commits), ticket=resolved_ticket, source=source)
    try:
        outcome = asyncio.run(ctx.do(spec))
    except LookupError as e:
        raise click.ClickException(f"{e} (a cassette replays only the prompt it was recorded on)") from None
    except (ImportError, MissingCredential) as e:
        raise click.ClickException(str(e)) from None

    report = outcome.value
    click.echo(
        f"backport  {outcome.status.value}"
        + (f"  ({outcome.reason})" if outcome.reason else "")
        + ("" if outcome.decided else "  (decided nothing)")
    )
    if report is not None:
        for pick in report.picked:
            click.echo(f"  picked  {pick.sha[:12]}  {pick.subject}")
        if report.conflict is not None:
            click.echo(f"  stuck   {report.conflict.commit[:12]}  {report.conflict.subject}")
    for finding in outcome.findings:
        where = f"{finding.path} " if finding.path else ""
        click.echo(f"  {where}{finding.id}: {finding.message}")

    cost = outcome.cost
    click.echo("")
    if resolve:
        click.echo(f"tokens    {cost.input_tokens} in, {cost.output_tokens} out")
        click.echo(f"cost      ${cost.usd:.4f}{_billing_note(cost)}")
    else:
        click.echo("cost      $0.0000  (deterministic; no model was consulted)")
    # Where the tape went, on the same terms `run` reports it. A recording nobody is told about is
    # one nobody goes back to. `_report_what_was_kept` holds the rule about when to speak, so
    # these five and `run` cannot drift the way two writers of one decision always do.
    if resolve and record:
        _report_what_was_kept(tape, ctx, asked=_asked_to_record(click.get_current_context()))
    _echo_telemetry(recorder)

    if out and report is not None and report.changeset.changes:
        _write_artifact(out, report.changeset)
        click.echo(f"changeset {out}")
        click.echo("")
        click.echo(
            f"Nothing was written. Open it against the release line with:  "
            f"in-lockstep apply --from-artifact {out} --base {target} --workflow backport"
        )

    _write_backport_ledger(
        lockstep,
        ctx,
        outcome,
        target,
        # The model, only when this command chose it AND it actually answered: a resolver that a
        # clean pick never consulted is a model that was never called, and recording it would put
        # a fabricated fact into a permanent record.
        selected.id if (resolve and backport_is_ours and cost.total_tokens > 0) else "",
        approval,
        ticket=resolved_ticket,
    )

    if outcome.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    if outcome.status is not Status.SUCCEEDED or not outcome.decided:
        raise SystemExit(EXIT_FAILED)


def _write_backport_ledger(
    lockstep: Any, ctx: Any, outcome: Any, target: str, model_id: str, approval: Any, ticket: Any
) -> None:
    """The same store every verb writes through, with the fields that differ for this one:
    which line, which commits, and which paths a model — rather than git — merged."""
    report = outcome.value
    _record(
        _ledger(lockstep),
        ctx.run_id,
        {
            "kind": "backport",
            **_provenance(lockstep),
            "target": target,
            **({"ticket": ticket.key} if ticket is not None and ticket.key else {}),
            **({"ticket_url": ticket.url} if ticket is not None and ticket.url else {}),
            **({"approval": approval.as_record()} if approval and approval.granted else {}),
            **({"model": model_id} if model_id else {}),
            "status": outcome.status.value,
            "reason": outcome.reason,
            "decided": outcome.decided,
            # The provenance a squashed apply cannot carry in `-x` lines: which commits this
            # change replays, and which files hold model-authored merges a reviewer reads first.
            "picked": [p.sha for p in report.picked] if report is not None else [],
            "resolved": list(report.resolved) if report is not None else [],
            "tokens": outcome.cost.total_tokens,
            "cost_usd": round(outcome.cost.usd, 6),
            "wall_seconds": round(outcome.cost.wall_seconds, 3),
            "findings": {
                "count": len(outcome.findings),
                "items": [f.as_record() for f in outcome.findings[:_LEDGER_MAX_FINDINGS]],
            },
        },
    )


def _eval_subject(lockstep: Any, *, kind: str, aspect: str, model_id: str) -> Any | None:
    """Which runs this one is comparable with, or `None` when that cannot be known.

    `EvalSubject` is this project's answer to that question and it had no caller: the hash was
    right and nothing had ever computed one, so `GATE-LEDGER-4` and `GATE-EVAL-1` sat `unit only`
    over a mechanism no run had been through. This is the call site.

    Built here rather than in the adapter that owns the composition, because `evaluation` is a leaf
    that may import nothing of ours — so the layer holding both the composition and the ledger is
    the one that can join them, and that layer is this one.

    Hashes the STATIC flatten (`Composition.text()`), which is guardrails, body, skills and
    contexts with no per-run diff in it. Hashing the rendered prompt would make every subject N=1
    and no baseline could ever accumulate, which is the failure `subject.py` opens by describing.

    Returns `None` rather than a partial subject in three cases, and each is "absent is not zero"
    rather than defensive coding:

      * **No model id.** The record already withholds `model` when the repository bound its own
        adapter and chose its own route, because this command's `--model` was never consulted. A
        subject keyed on a model that was not called would be worse than no subject: it is a
        comparison somebody could act on.
      * **No composition for the label**, or more than one candidate for a verb that names no
        aspect. Guessing which of two ran is the fabrication this file's neighbours refuse.
      * **The body will not resolve.** A missing prompt body is a real failure and it is the run's
        to report, not the ledger writer's to raise inside a record write.

    Not called for `implement` or `fix`. `AiStrategy._session` appends `house_rules(root)` at run
    time for `reads_house_rules` strategies, so their static flatten is NOT what ran, and a subject
    computed from it would be a hash of a prompt nobody sent. That is a measurement against a
    comparison that was never made, which is this repository's own named failure mode. When those
    verbs expose the layers a session actually used, they can carry one too.
    """
    if not model_id:
        return None

    from .evaluation import subject_for

    bound = _bound_compositions(lockstep)
    label = f"{kind}/{aspect}" if aspect else ""
    if not label:
        candidates = sorted(name for name in bound if name.startswith(f"{kind}/"))
        if len(candidates) != 1:
            return None
        label = candidates[0]
    composition = bound.get(label)
    if composition is None:
        return None
    try:
        composed = composition.text()
    except Exception:  # noqa: BLE001 - see docstring: a body that will not resolve is the run's to report
        return None
    layers = composition.layers
    return subject_for(
        verb=kind,
        strategy_id=label,
        composed_prompt=composed,
        # Bodies, not names. A skill renamed is the same instructions and should compare equal; a
        # skill rewritten under the same name is different instructions and must not.
        skills=tuple(text for _, text in layers.skills),
        context_recipe=tuple(text for _, text in layers.contexts),
        model_id=model_id,
        # Both halves of the display pair, the way `receipt.py` emits them. Carrying the class name
        # alone left `label()`'s version branch unreachable on every path a run can take, which made
        # `docs/extending.md`'s promise to adopters — declared `version` is "the human label that
        # travels alongside" a content hash — travel nowhere. Neither reaches `key`: identity stays
        # the hash precisely so a measurement is right whether or not somebody remembered to bump.
        prompt_id=type(composition.prompt).__name__,
        prompt_version=composition.prompt.version,
    )


def _turns_taken(ctx: Any) -> dict[str, Any]:
    """How many model calls the run made, and what they asked for, by tool name (GATE-RECORD-6).

    Read from the run's `Spend` rather than from a strategy's report, because the `Spend` is the
    one counter every invoker in the run charges through -- a delegated child's turns included,
    which no report sees -- and it is the number the turn ceiling was enforced against, so the
    record says what was bounded. Absent, not zero, on a context that carries no `Spend`.
    """
    spend = getattr(ctx, "spend", None)
    if spend is None:
        return {}
    return {"turns": spend.turns, "tool_calls": dict(sorted(spend.tool_calls.items()))}


def _write_ledger(
    lockstep: Any, ctx: Any, outcome: Any, aspect: str, model_id: str, *, kind: str = "review"
) -> None:
    """One writer, through the store that owns the format.

    This hand-rolled the record and stamped `"schema": 2` and `"epoch": "in-process"` as literals
    beside `InRepoLedger`, which owns those constants and exists for exactly this. Two writers of
    one format is not an organisation problem: an epoch bump would have moved one of them, and the
    reader refuses to compare across epochs precisely so that a silent mismatch cannot average a
    credits-era number with a tokens-era one.

    `kind` names the verb; `aspect` is the review lens and is omitted for verbs that have none, so
    a triage record does not carry an empty lens field that reads as a missing one.
    """
    subject = _eval_subject(lockstep, kind=kind, aspect=aspect, model_id=model_id)
    _record(
        _ledger(lockstep),
        ctx.run_id,
        {
            "kind": kind,
            **_provenance(lockstep),
            **({"aspect": aspect} if aspect else {}),
            # What this run is comparable WITH. A flat string on purpose: `summarize` groups on
            # `str(record.get(by))`, so a nested object would stringify its own repr into the row
            # label and every record would be its own group — a slice that looks like a slice and
            # aggregates nothing. Omitted rather than defaulted when the subject cannot be known;
            # `_eval_subject` says when and why.
            **({"subject": subject.key, "subject_label": subject.label()} if subject else {}),
            # Omitted when the repository bound its own Review adapter and therefore chose its
            # own model: this command's `--model` was never consulted, and writing it down
            # would put a model that was not called into a permanent record.
            **({"model": model_id} if model_id else {}),
            "status": outcome.status.value,
            # The machine-readable refinement, which the terminal printed and the record
            # dropped. `reason` is what a failure is grouped by — provider.authentication is a
            # different problem from cost.budget_exceeded, and `status` calls both "errored".
            "reason": outcome.reason,
            "decided": outcome.decided,
            **_turns_taken(ctx),
            "tokens": outcome.cost.total_tokens,
            "input_tokens": outcome.cost.input_tokens,
            "output_tokens": outcome.cost.output_tokens,
            "cost_usd": round(outcome.cost.usd, 6),
            # The findings themselves, not only how many. A record whose purpose is evidence
            # kept the count and discarded the content — so three real findings existed
            # nowhere but a terminal scrollback, and the ledger could say a run found things
            # without being able to say what. `count` is the true total; `items` is bounded,
            # so when a run finds more than the cap the mismatch between them says so.
            "findings": {
                "count": len(outcome.findings),
                "items": [f.as_record() for f in outcome.findings[:_LEDGER_MAX_FINDINGS]],
            },
            "wall_seconds": round(outcome.cost.wall_seconds, 3),
            # Absent rather than zero, and absent rather than one. The ledger's own rule is
            # that a measurement nobody took is not a measurement of nothing.
            **(
                {"priced_fraction": round(outcome.cost.priced_fraction, 4)}
                if outcome.cost.priced_fraction is not None
                else {}
            ),
            # What share of this actually cost money. A replay has real tokens and no cost, and
            # without this the record is indistinguishable from a model that was mispriced to
            # zero — the exact fabrication `pricing.py` exists to refuse.
            **(
                {"billed_fraction": round(outcome.cost.billed_fraction, 4)}
                if outcome.cost.billed_fraction is not None
                else {}
            ),
        },
    )


@main.command(name="gate")
@click.option("--actor", required=True, help="The login that asked for the run.")
@click.option("--association", default="", help="GitHub's author_association for that login.")
@click.option(
    "--codeowners",
    default=".github/CODEOWNERS",
    type=click.Path(),
    help="Read from the TRUSTED ref, never from a change under review.",
)
@click.option(
    "--open-proposals",
    "open_proposals",
    default="",
    metavar="WORKFLOW",
    help="Also refuse when this workflow already has change requests open. Asks the host.",
)
@click.option(
    "--max",
    "max_open",
    type=int,
    default=1,
    show_default=True,
    help="How many open change requests that workflow may have.",
)
def gate_cmd(actor: str, association: str, codeowners: str, open_proposals: str, max_open: int) -> None:
    """May this person ask for a run? Exit 0 if yes, 3 if no.

    A chat-ops trigger is an unauthenticated entry point wearing a familiar interface, and the
    decision about who may fire one is security-critical enough that it should not be `grep` inside
    a YAML `if:`. This is that decision as a Python function, where it has tests.

    It authorizes the ASKER. It says nothing about the issue text, which anyone can write and which
    the framework tags untrusted regardless of who invoked the run.
    """
    from pathlib import Path as _Path

    from .platform.actors import authorize

    file = _Path(codeowners)
    decision = authorize(
        actor=actor,
        association=association,
        codeowners=file.read_text() if file.is_file() else "",
    )
    click.echo(f"actor     {actor}")
    for item in decision.considered:
        click.echo(f"          {item}")
    click.echo(f"{'allowed' if decision.allowed else 'refused'}   {decision.reason}")
    if not decision.allowed:
        raise SystemExit(EXIT_BLOCKED)

    if not open_proposals:
        return

    # `hosted_scm` rather than `_bound_scm`: this command decides who may fire a run, and loading
    # `.lockstep/lockstep.py` to answer it would execute repository code inside the authorization
    # gate. The cost is that a repository binding its own `Scm` is not consulted here, which is the
    # right trade for a gate. Reached only after the actor is allowed, so a login this refuses
    # never causes a host call at all.
    from .platform.hosted import hosted_scm
    from .platform.scm import RUN_BRANCH_PREFIX, workflow_slug

    host: Any = hosted_scm(".")
    listing = getattr(host, "open_changes_by_workflow", None)
    reason = "" if listing is not None else f"{type(host).__name__} cannot list change requests"
    open_now: tuple[Any, ...] = ()
    if listing is not None:
        try:
            open_now = tuple(listing(open_proposals))
        except Exception as error:  # noqa: BLE001 - every failure to count is the same answer
            reason = str(error) or type(error).__name__

    if reason:
        # `report --scm` degrades and says why, because a missing column costs a reader a column.
        # A ceiling cannot do that: one that lets the run through because nobody could read it is
        # not a ceiling. So the two invert here on purpose, and the message says which this is.
        click.echo(f"proposals —  ({reason})")
        click.echo("refused   an uncounted ceiling is not an empty one")
        raise SystemExit(EXIT_BLOCKED)

    where = f"{RUN_BRANCH_PREFIX}/{workflow_slug(open_proposals)}/"
    click.echo(f"proposals {len(open_now)} open on {where}  (max {max_open})")
    if len(open_now) >= max_open:
        for change in open_now:
            click.echo(f"          {getattr(change, 'url', '') or getattr(change, 'branch', '')}")
        # Names its own source. This number came from `--max`, not from `lockstep.py`, because
        # this command does not load the lifecycle — so calling it "the ceiling this repository
        # declared" would credit a file it never read.
        click.echo(f"refused   --max {max_open} already open")
        raise SystemExit(EXIT_BLOCKED)


# What an implementing session gets. Both are ceilings rather than targets, and both are
# deliberately larger than the review lens's: a reviewer is handed everything it needs in one
# prompt, where an implementer has to go and find it. See `adapters.ai.implement` for why forty
# turns is chosen against the quadratic history cost rather than against patience.
_IMPLEMENT_TURNS = 40
_IMPLEMENT_MAX_TOKENS = 8192

# What `--dry-run` returns instead of calling a model. Shaped like an implement reply, because
# `DryRunProvider`'s own default is `{"findings": []}` — a review answer — and a wiring check that
# fails to parse reports the wrong thing.
#
# It stages nothing, which a dry run cannot, so the run ends `implement.no_changes` and exits
# non-zero. That is the honest result rather than a wart: what this flag proves is that the module
# loaded, the container resolved, approval and egress let the run start, the prompt composed and
# the ledger was written — everything up to the model, which is where most of the setup mistakes
# are. It cannot prove anything about the change, because there isn't one.
# What `run_script` runs inside by default. A default rather than a flag people remember, because
# the alternative default is executing a command a model chose on the host that holds the key —
# and a control whose safe setting is opt-in is a control most runs will not have.
#
# Under docker or podman this gets `--cap-drop=ALL`, `--security-opt=no-new-privileges` and
# `--network=none`, which is the egress constraint an in-process framework otherwise has no way to
# impose. A repository whose stack is not Python names its own; `--sandbox-image ''` asks for the
# host, deliberately.
_DEFAULT_SANDBOX_IMAGE = "docker.io/library/python:3.12-slim"

_DRY_RUN_REPLY = (
    '{"summary": "dry run: the wiring resolved and no model was called", '
    '"notes": [], "unfinished": ["nothing was implemented"]}'
)


def _ticket_from_file(path: str) -> Any:
    """A ticket read off disk, so this runs with no tracker and no network.

    JSON if it parses as an object, otherwise markdown: first heading is the title, the rest is
    the body, and acceptance criteria come from the task list — the same `criteria_from` a real
    tracker's body goes through, because a fixture that parsed differently from the real thing
    would be a fixture of something else.
    """
    import json
    from pathlib import Path as _Path

    from .platform.tickets import Ticket, criteria_from

    file = _Path(path)
    if not file.exists():
        raise click.ClickException(f"no ticket file at {path}")
    raw = file.read_text()

    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        body = str(data.get("description") or data.get("body") or "")
        return Ticket(
            key=str(data.get("key") or data.get("id") or file.stem),
            title=str(data.get("title") or ""),
            description=body,
            acceptance_criteria=tuple(data.get("acceptance_criteria") or criteria_from(body)),
        )

    lines = raw.splitlines()
    title = lines[0].lstrip("# ").strip() if lines else file.stem
    body = "\n".join(lines[1:]).strip()
    return Ticket(key=file.stem, title=title, description=body, acceptance_criteria=criteria_from(body))


@main.command(name="implement")
@click.option("--ticket", default="", help="Ticket key to fetch, e.g. '#42'.")
@click.option("--ticket-file", default="", type=click.Path(), help="Read the ticket from a file instead.")
@click.option(
    "--strategy",
    type=click.Choice(["oneshot", "tdd"]),
    default="oneshot",
    show_default=True,
    help="Which strategy class to bind: one exploring session, or red-then-green.",
)
@click.option("--model", default="anthropic:claude-sonnet-4-6")
@click.option("--out", default="", type=click.Path(), help="Write the ChangeSet here for `apply`.")
@click.option("--max-turns", type=int, default=_IMPLEMENT_TURNS, show_default=True)
@click.option("--execute/--no-execute", default=True, help="Let the model run commands, sandboxed.")
@click.option(
    "--sandbox-image",
    default=_DEFAULT_SANDBOX_IMAGE,
    show_default=True,
    help="Container image for run_script. Pass '' to run on the host instead, deliberately.",
)
@click.option(
    "--approve",
    is_flag=True,
    help="You are the human in the loop for this run. Attended, local use only.",
)
@click.option(
    "--approved-by",
    default="",
    help="Who asked for this run. The unattended form of --approve; recorded in the ledger.",
)
@click.option("--offline", is_flag=True, help="Serve model calls from a cassette. No keys, no spend.")
@click.option(
    "--record/--no-record",
    default=None,
    help="Keep this run's model calls. On by default; --no-record declines.",
)
@click.option("--cassette", default="", help="Where to read or write a recording.")
@click.option("--budget", type=float, default=None, help="Hard ceiling, in USD.")
@click.option(
    "--resume",
    "resume",
    default="",
    help=(
        "Reuse what earlier attempts on this ticket staged: a changeset artifact path. Opt-in, "
        "because a model handed its own wrong diff will defend it."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Canned answer. Proves everything up to the model; stages nothing, so it exits non-zero.",
)
def implement_cmd(
    ticket: str,
    ticket_file: str,
    strategy: str,
    model: str,
    out: str,
    max_turns: int,
    execute: bool,
    sandbox_image: str,
    approve: bool,
    approved_by: str,
    offline: bool,
    record: bool | None,
    cassette: str,
    budget: float | None,
    resume: str,
    dry_run: bool,
) -> None:
    """Implement one ticket, in-process, staging the change rather than writing it.

    Nothing here touches the working tree. The session stages writes into a `ChangeSet`, which
    `--out` serializes — and `apply-inline` or `apply --from-artifact` is what writes it, through
    the same guard a second time. That separation is the point: a model that has just read a
    ticket written by anybody is not the thing that should also hold the ability to write.
    """

    record = _recording(dry_run=dry_run, offline=offline, record=record)
    from .adapters.ai import TDD, Implement, Oneshot
    from .adapters.sandbox import Sandbox
    from .adapters.worktree import WorktreeRunner
    from .ai.auth import Auth
    from .ai.bootstrap import (
        LLMProvider,
        MissingCredential,
        Model,
        caps_for,
        credentials_for,
        table_for,
    )
    from .ai.bootstrap import (
        default_registry as default_providers,
    )
    from .ai.invoker import AiInvoker, InvokePolicy
    from .ai.replay import Cassette, DryRunProvider, RecordingProvider, ReplayProvider
    from .core.spend import Budget
    from .middleware.approval import ApprovalGate
    from .privileged.egress import EgressPolicy

    if bool(ticket) == bool(ticket_file):
        raise click.ClickException("pass exactly one of --ticket or --ticket-file")

    lockstep, recorder = _default_lockstep()

    source = click.get_current_context().get_parameter_source
    if budget is not None:
        lockstep.budget = Budget(usd=budget)
    elif offline or dry_run:
        # As `review` does: a run that cannot spend states a zero ceiling.
        _declare_zero_ceiling(lockstep)
    if source("model") is ParameterSource.DEFAULT:
        model = lockstep.models.routes.get("implement", model)

    # An implementing adapter declares WRITES_FILES and SPENDS_BUDGET, so `Lockstep.context`
    # refuses to start the run unless an approval path exists.
    #
    # Two forms, because the two situations differ in what is worth recording. `--approve` is the
    # local, attended one: the person typing it is the human, and there is no useful identity to
    # write down because it is the same person reading the output. `--approved-by` is the
    # unattended one — a name, recorded in the ledger, because a grant nobody can be traced to is
    # not much of a grant. A chat-ops trigger uses the second, passing the verified commenter.
    #
    # Neither is an environment approval in the system of record, and neither pretends to be. What
    # makes the unattended form defensible is what stands behind it: an actor gate decides who may
    # ask, and the change is staged into an artifact a person reviews as a pull request.
    approval = _approval(approve, approved_by)
    # The gate itself, only if the module has none. The GRANT lives on the context either way — a
    # gate that has to be CONSTRUCTED differently to be satisfiable would put the local and the
    # hosted paths back on separate plumbing, which is the seam this exists to remove.
    if approval.granted and not any(getattr(m, "provides_approval", False) for m in lockstep.middleware):
        lockstep.middleware = [*lockstep.middleware, ApprovalGate()]

    resolved = _load_ticket(
        ticket,
        ticket_file,
        lockstep.repo.root,
        source=_bound_ticket_source(lockstep),
        # So a second `implement` on the same ticket reads the review of the first, exactly as the
        # CI workflow does. The laptop and the runner are the same run with a different approval.
        scm=_bound_scm(lockstep),
    )

    auth = Auth()
    try:
        providers = default_providers(auth)
    except MissingCredential as e:
        raise click.ClickException(str(e)) from None
    selected = Model(model)
    table = table_for(providers, selected, _bound_cost_table(lockstep))
    tape = Cassette.load(cassette or _cassette_default(lockstep, "implement"))

    def build_invoker(_ctx: Any) -> AiInvoker:
        provider: LLMProvider
        if dry_run:
            provider = DryRunProvider(_DRY_RUN_REPLY)
        elif offline:
            provider = ReplayProvider(tape)
        else:
            creds = credentials_for(auth, selected.provider)
            provider = providers.provider_for(selected, creds)
            if record:
                provider = RecordingProvider(provider, tape, Redact())
        return AiInvoker(
            provider,
            model=selected.name,
            # What the registration says the model can do; a schema call or a tool-holding
            # session is refused by name before the first turn (GATE-MODEL-1).
            caps=caps_for(providers, selected),
            cost_table=table,
            spend=_ctx.spend,
            redact=Redact(),
            egress=(
                lockstep.container.resolve(EgressPolicy)
                if lockstep.container.has(EgressPolicy)
                else EgressPolicy.detect()
            ),
        )

    # Whether the CLI is the thing that chose the model. A repository shipping its own Implement
    # adapter has chosen its own, and `selected` is then a number this command would have used —
    # recording it would be a fabricated field in a permanent record.
    cli_chose_the_model = not lockstep.container.has(Implement)
    if cli_chose_the_model:
        # `--strategy` names the class directly: the strategy IS the adapter, so which one runs
        # is decided here, in code, and nothing a ticket can carry re-decides it.
        chosen = {"oneshot": Oneshot, "tdd": TDD}[strategy or "oneshot"]
        lockstep.bind(
            Implement,
            chosen(
                build_invoker,
                repo_root=lockstep.repo.root,
                # A sandbox, or nothing. `--no-execute` withholds the runner rather than the
                # tool, so the capability the tool set declares — and therefore what egress and
                # approval see — does not change with the flag. A run that could execute on some
                # other configuration must not read as harmless on this one.
                # `require_container` follows the image, so naming one and not getting one is a
                # refusal rather than a quiet downgrade to running on the host. Passing
                # `--sandbox-image ''` is the deliberate way to ask for the host, and it reads
                # like a decision because it is one.
                #
                # Wrapped in `WorktreeRunner` so a model's command runs in a throwaway worktree of
                # HEAD, not the live tree — its writes cannot reach the real `.git`/`.lockstep`,
                # which the RW bind mount would otherwise leave open past ChangeGuard.
                commands=(
                    WorktreeRunner(
                        Sandbox(image=sandbox_image, require_container=bool(sandbox_image)),
                        lockstep.repo.root,
                    )
                    if execute
                    else None
                ),
                policy=InvokePolicy.under(
                    lockstep.policy.resolve(),
                    max_turns=max_turns,
                    max_tokens=_IMPLEMENT_MAX_TOKENS,
                    deadline_seconds=1800,
                ),
            ),
        )

    # The tape reaches the run, which is what makes the seam in `ai.bootstrap` fire. Without it
    # `ctx.recording` was None and a module-bound adapter recorded nothing, so the documented
    # extension recipe in `docs/extending.md` -- bind your own Review -- spent money and kept
    # nothing, silently (#264). `run` and `selfcheck` passed a tape from the beginning; these five
    # did not, and the difference was invisible because the CLI's own `build_invoker` wraps for
    # the case where the module binds nothing, which is the case everybody tested.
    ctx = _context(
        lockstep,
        _run_id(f"implement-{resolved.key.lstrip('#')}"),
        approval,
        recording=tape if record else None,
    )
    try:
        outcome = asyncio.run(ctx.do(Implement(ticket=resolved, attempts=_attempts_from(resume))))
    except LookupError as e:
        raise click.ClickException(f"{e} (a cassette replays only the prompt it was recorded on)") from None
    except (ImportError, MissingCredential) as e:
        raise click.ClickException(str(e)) from None

    report = outcome.value
    label = report.strategy if report is not None and report.strategy else "implement"
    click.echo(
        f"{label}  {outcome.status.value}"
        + (f"  ({outcome.reason})" if outcome.reason else "")
        + ("" if outcome.decided else "  (decided nothing)")
    )
    if report is not None and report.summary:
        click.echo(f"  {report.summary}")
    for finding in outcome.findings:
        where = f"{finding.path} " if finding.path else ""
        click.echo(f"  {where}{finding.id}: {finding.message}")

    cost = outcome.cost
    click.echo("")
    idle = getattr(report, "idle_turns", 0) if report is not None else 0
    click.echo(
        f"turns     {report.turns if report is not None else 0}"
        + (f"  ({idle} idle at the end)" if idle else "")
    )
    click.echo(f"tokens    {cost.input_tokens} in, {cost.output_tokens} out")
    click.echo(f"cost      ${cost.usd:.4f}{_billing_note(cost)}")
    # Where the tape went, on the same terms `run` reports it. A recording nobody is told about is
    # one nobody goes back to. `_report_what_was_kept` holds the rule about when to speak, so
    # these five and `run` cannot drift the way two writers of one decision always do.
    if record:
        _report_what_was_kept(tape, ctx, asked=_asked_to_record(click.get_current_context()))
    _echo_telemetry(recorder)

    if out and report is not None and report.changeset.changes:
        _write_artifact(out, report.changeset)
        click.echo(f"changeset {out}")
        click.echo("")
        click.echo(f"Nothing was written. Apply it with:  in-lockstep apply-inline --from-artifact {out}")

    _write_implement_ledger(
        lockstep, ctx, outcome, label, selected.id if cli_chose_the_model else "", approval, ticket=resolved
    )

    if outcome.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    # An undecided run exits non-zero, which is where this differs from `review`. The turn cap
    # stopped a session mid-work, so the staged change is whatever it had reached — and a zero
    # exit beside a partial change set is the one reading that would get it applied unread.
    if outcome.status is not Status.SUCCEEDED or not outcome.decided:
        raise SystemExit(EXIT_FAILED)


def _bound_ticket_source(lockstep: Lockstep) -> Any:
    """The repository's own `TicketSource` if its module bound one, else None — so a repo that
    binds `JiraSource` reaches Jira and one that binds nothing gets the GitHub default."""
    from .platform.tickets import TicketSource

    if not lockstep.container.has(TicketSource):
        return None
    # Resolving a Protocol from the container is the whole point of the ports — the interface is
    # abstract by design, and a concrete adapter serves it. mypy's `type-abstract` guard is for
    # instantiation, which this is not.
    return lockstep.container.resolve(TicketSource)  # type: ignore[type-abstract]


def _bound_scm(lockstep: Lockstep) -> Any:
    """The repository's own `Scm` if its module bound one, else the detected host's.

    The detected host rather than GitHub by name, for the reason `_post_review_comment` gives: on
    a GitLab project, hardcoding the GitHub adapter reads nothing while claiming to have tried.
    """
    from .platform.hosted import hosted_scm
    from .platform.scm import Scm

    if lockstep.container.has(Scm):
        return lockstep.container.resolve(Scm)  # type: ignore[type-abstract]
    return hosted_scm(lockstep.repo.root)


def _load_ticket(key: str, path: str, root: str, source: Any = None, scm: Any = None) -> Any:
    """A ticket from a file, a bound `TicketSource`, or the GitHub default.

    A repository that binds `JiraSource` (or any other tracker) in its module has `source` passed
    in, so `implement --ticket PROJ-123` reaches Jira; with nothing bound, GitHub Issues is the
    zero-config default, matching what `_default_lockstep` assumes elsewhere.

    With `scm`, the ticket also carries what people said on the open change requests opened for it,
    so a second `implement` from a laptop reads the review the same way the CI workflow does. The
    note is echoed rather than swallowed: context that silently did not arrive is the kind of thing
    a person discovers six rounds later.
    """
    if path:
        loaded = _ticket_from_file(path)
    else:
        from pathlib import Path as _Path

        from .platform.tickets import GitHubIssues

        tracker = source if source is not None else GitHubIssues(root=_Path(root))
        try:
            loaded = asyncio.run(tracker.get(key))
        except (RuntimeError, OSError) as e:
            raise click.ClickException(f"could not read ticket {key!r}: {e}") from None

    if scm is None:
        return loaded
    from .platform.conversation import with_review

    reviewed, note = asyncio.run(with_review(loaded, scm))
    click.echo(note)
    return reviewed


def _write_artifact(path: str, changeset: Any) -> None:
    from .platform.artifacts import write_changeset

    write_changeset(path, changeset)


def _write_implement_ledger(
    lockstep: Any,
    ctx: Any,
    outcome: Any,
    strategy: str,
    model_id: str,
    approval: Any = None,
    ticket: Any = None,
) -> None:
    """The same store `review` writes through, with the fields that differ for this verb."""
    report = outcome.value
    _record(
        _ledger(lockstep),
        ctx.run_id,
        {
            "kind": "implement",
            **_provenance(lockstep),
            "strategy": strategy,
            # The ticket this run implemented, as structured fields rather than only inside the
            # run id: a record can be joined to its work item without parsing a string, and the
            # tracker URL — which a Jira source computes and the run id cannot carry — is kept.
            **({"ticket": ticket.key} if ticket is not None and ticket.key else {}),
            **({"ticket_url": ticket.url} if ticket is not None and ticket.url else {}),
            # Who asked. Absent for an attended local run, where the person reading the output
            # is the person who approved it and a name would be noise. Present for anything
            # unattended, where it is the only trace of a human in the loop.
            **({"approval": approval.as_record()} if approval and approval.granted else {}),
            # Omitted rather than guessed when the repository bound its own adapter, the same
            # way `priced_fraction` is omitted rather than written as zero. A record naming a
            # model that was never called is worse than one that is quiet about which was.
            **({"model": model_id} if model_id else {}),
            "status": outcome.status.value,
            "reason": outcome.reason,
            "decided": outcome.decided,
            **_turns_taken(ctx),
            # The paths, not the contents. A ledger record is committed to git and meant to
            # stay diffable; the change itself is the artifact, and duplicating it here would
            # write every proposed file into a permanent record twice.
            "paths": list(report.changeset.paths()) if report is not None else [],
            "unfinished": list(report.unfinished) if report is not None else [],
            "tokens": outcome.cost.total_tokens,
            "input_tokens": outcome.cost.input_tokens,
            "output_tokens": outcome.cost.output_tokens,
            "cost_usd": round(outcome.cost.usd, 6),
            **(
                {"billed_fraction": round(outcome.cost.billed_fraction, 4)}
                if outcome.cost.billed_fraction is not None
                else {}
            ),
            "findings": {
                "count": len(outcome.findings),
                "items": [f.as_record() for f in outcome.findings[:_LEDGER_MAX_FINDINGS]],
            },
            "wall_seconds": round(outcome.cost.wall_seconds, 3),
        },
    )


@main.command(name="apply-inline")
@click.option("--from-artifact", "artifact", required=True, type=click.Path())
@click.option("--dry-run", is_flag=True, help="Check against the guard; write nothing.")
def apply_inline_cmd(artifact: str, dry_run: bool) -> None:
    """Apply a ChangeSet to the working tree. The local default, and the third guarded path.

    The two-job trampoline exists because a job holding a provider credential should not also hold
    a write token. On a laptop there is no second job and no token — the developer's own shell is
    the privileged half — so this writes directly.

    That makes it the path most easily forgotten, which is why GATE-GUARD-1 names it: it reaches
    neither the tool boundary (a third-party MCP server writes without asking `Workspace`) nor
    `apply --from-artifact`. It makes the identical `_guard_or_exit` call the artifact path does.
    """
    changeset = _load_changeset(artifact)
    _guard_or_exit(changeset)

    if dry_run:
        click.echo(f"{len(changeset.changes)} change(s) pass the guard; nothing was written")
        return

    written = _write_changeset(changeset)
    for path in written:
        click.echo(f"  {path}")
    click.echo(f"{len(written)} change(s) applied to the working tree")


def _write_changeset(changeset: Any) -> list[str]:
    """Apply, after the guard. Deletions and writes, nothing clever."""
    from pathlib import Path as _Path

    touched: list[str] = []
    for change in changeset.changes:
        target = _Path(change.path)
        if change.deleted:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.contents or "")
        touched.append(change.path)
    return touched


@main.group(name="pack")
def pack_group() -> None:
    """Inspect what a configuration does, and what an installed extension pack would do to it.

    `describe` reads either subject — this repository with no argument, an installed pack with a
    name — and they are deliberately the same shape. The repository came first: a receipt format
    nobody has run against their own module is a format nobody has checked, and the questions it
    answers are the ones an adopter asks about their own repository before anyone else's.

    `ls` names what is installed, `try` measures one against your cases for nothing, and `add`
    (top level, because accepting is not inspecting) records what you accepted.
    """


@pack_group.command(name="ls")
def pack_ls_cmd() -> None:
    """List installed extension packs. None of them is in force.

    Installing a pack offers it; a line in `.lockstep/lockstep.py` is what puts it to work. That
    is the difference between this group and `in_lockstep.standards`, where installing IS
    applying — a standards package can only tighten, and an extension hands a model tools.

    Nothing here is imported. The names, versions and the `imports` column all come from
    distribution metadata, so listing a stranger's package runs no code it ships.
    """
    from .packs import PackError, installed

    packs = installed()
    if not packs:
        click.echo("no extension packs installed")
        click.echo("")
        click.echo("A pack is an ordinary distribution declaring an `in_lockstep.extensions`")
        click.echo("entry point. Installing one offers it; naming it in .lockstep/lockstep.py")
        click.echo("is what puts it in force.")
        return

    click.echo("installed packs  (offered, not in force — bind one in .lockstep/lockstep.py)")
    for found in packs:
        try:
            manifest = found.manifest()
            kind, summary = manifest.kind, manifest.summary
        except PackError as e:
            kind, summary = "?", str(e)
        click.echo(
            f"  {found.name:<26} {kind:<9} {found.version or '(unknown)':<10} "
            f"imports: {found.imports():<8} {summary}"
        )


@main.group(name="market")
def market_group() -> None:
    """The catalogs this repository reads for extension packs.

    A catalog is a static `index.toml` in a git repository — no service, no accounts, no ranking to
    defend. It is read when you search and when you accept a pack, and never during a run: a run of
    a repository that installed a pack is identical to one that vendored the same class by hand,
    which is what keeps a strategy from ever being selected by a name.
    """


@market_group.command(name="add")
@click.argument("name")
@click.argument("url")
def market_add_cmd(name: str, url: str) -> None:
    """Register a catalog at URL under NAME.

    Written to `.lockstep/market.toml` and committed, because a source decides where this
    repository looks for code and that belongs in review — the same argument the standards layer
    makes about a dependency being a reviewable diff. https only: a catalog says what to install,
    so it is fetched over a channel that cannot be rewritten in transit.
    """
    from .market import MarketError, add_source

    lockstep, _ = _default_lockstep()
    root = Path(lockstep.repo.root)
    try:
        path = add_source(root, name, url)
    except MarketError as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"registered  {name}  {url}")
    click.echo(f"wrote       {_relative(path, root)}  — commit it")


@market_group.command(name="ls")
def market_ls_cmd() -> None:
    """The catalogs this repository reads."""
    from .market import sources

    lockstep, _ = _default_lockstep()
    registered = sources(Path(lockstep.repo.root))
    if not registered:
        click.echo("no catalogs registered")
        click.echo("")
        click.echo("A catalog is a static index.toml in a git repository:")
        click.echo("  in-lockstep market add acme https://raw.example/acme/index.toml")
        return
    for source in registered:
        click.echo(f"  {source.name:<20} {source.url}")


@market_group.command(name="lint")
@click.argument("path", type=click.Path(exists=True))
def market_lint_cmd(path: str) -> None:
    """Check a catalog's own entries against the criteria it claims to apply.

    For whoever publishes one, in their CI. A criterion nobody re-checks is a sentence in a README,
    which is the failure this whole design is arranged against — so the catalog that states
    criteria is the catalog that can be failed for not meeting them.
    """
    from .market import MarketError, Source, criteria_failures, parse_index, receipt_at

    index = Path(path)
    try:
        catalog = parse_index(index.read_text(), Source(name=index.stem, url=str(index)))
    except MarketError as e:
        raise click.ClickException(str(e)) from None

    if not catalog.entries:
        click.echo("the catalog lists nothing")
        return

    failed = 0
    for entry in catalog.entries:
        try:
            receipt = receipt_at(index.parent, entry.receipt)
        except MarketError as e:
            raise click.ClickException(str(e)) from None
        problems = criteria_failures(entry, receipt) if catalog.criteria else []
        state = "ok" if not problems else "FAILS"
        click.echo(f"  {entry.name:<28} {entry.kind or '?':<9} {state}")
        for problem in problems:
            click.echo(f"  {'':<28} missing: {problem}")
        failed += bool(problems)

    click.echo("")
    if not catalog.criteria:
        click.echo(
            f"{len(catalog.entries)} entr(y/ies). This catalog claims no criteria, so nothing was "
            f"checked —\nwhich is a legitimate thing for a tap to be: an internal pack is trusted "
            f"by having been\npublished inside the company, not by passing a check."
        )
        return
    click.echo(f"{len(catalog.entries)} entr(y/ies), {failed} failing the criteria this catalog states")
    if failed:
        raise SystemExit(EXIT_FAILED)


@main.command(name="search")
@click.argument("query", default="")
def search_cmd(query: str) -> None:
    """Find packs across the registered catalogs.

    Grouped by source, because the difference matters: the project's catalog states entry criteria
    and an organisation's internal tap states none — an internal pack is trusted by the fact that
    somebody inside the company published it, which is a different question and a better answer.

    A name listed by two catalogs is reported rather than resolved. Guessing which one somebody
    meant is how the wrong code gets installed under the right name.
    """
    from .market import MarketError, criteria_failures, read_catalog, receipt_at, sources

    lockstep, _ = _default_lockstep()
    root = Path(lockstep.repo.root)
    registered = sources(root)
    if not registered:
        raise click.ClickException(
            "no catalogs registered. `in-lockstep market add <name> <https://.../index.toml>`"
        )

    seen: dict[str, list[str]] = {}
    for source in registered:
        try:
            catalog = read_catalog(source, root=root)
        except MarketError as e:
            click.echo(f"{source.name}: {e}", err=True)
            continue

        matches = [
            entry
            for entry in catalog.entries
            if not query or query.lower() in f"{entry.name} {entry.summary} {entry.kind}".lower()
        ]
        criteria = "states entry criteria" if catalog.criteria else "no criteria — a tap"
        click.echo(f"{source.name}  ({criteria})")
        if not matches:
            click.echo("  (nothing matches)")
        for entry in matches:
            seen.setdefault(entry.name, []).append(source.name)
            try:
                receipt = receipt_at(root, entry.receipt)
            except MarketError as e:
                click.echo(f"  {entry.name}: {e}", err=True)
                receipt = None
            problems = criteria_failures(entry, receipt) if catalog.criteria else []
            flag = "" if not problems else f"   <- {len(problems)} criterion/criteria unmet"
            click.echo(f"  {entry.name:<26} {entry.kind or '?':<9} {entry.summary}{flag}")
        click.echo("")

    for name, found in sorted(seen.items()):
        if len(found) > 1:
            click.echo(
                f"{name} is listed by {', '.join(found)}. Name the distribution you mean when you "
                f"install it; nothing here picks for you.",
                err=True,
            )


@main.command(name="add")
@click.argument("name")
@click.option("--accept", is_flag=True, help="Accept capabilities this pack did not previously hold.")
def add_cmd(name: str, accept: bool) -> None:
    """Accept an installed pack, and print the lines that would put it to work.

    Three things happen, and the one that does not happen is the point.

    It **re-derives** the receipt from the code that is actually installed and compares it with
    what this repository accepted before, in `.lockstep/packs/<name>.json`. A capability the pack
    did not hold last time is refused until `--accept` says otherwise, because more agency is
    exactly the change that should cost somebody a decision.

    It **records** what you accepted, as a committed file. That record is the acknowledgement:
    `doctor` re-derives and compares against it, so an upgrade that widens what a pack may do
    fails a check rather than arriving quietly.

    It **prints** the lines for `.lockstep/lockstep.py` and does not write them. That file can
    rebind any adapter, remove any middleware and grant any tool — it is the first entry in its own
    protected-path deny list and it loads from a trusted ref — and "every line in it was typed by a
    person" is worth more than two saved keystrokes.

    It does not install anything either. Putting a stranger's code on your machine is your package
    manager's job, in your dependency diff, and a framework that did it for you would be the one
    deciding what you trust.
    """
    from .packs import PackNotFound, pack, pinning
    from .receipt import compare, read_record, receipt_for_pack, render_pack, write_record

    lockstep, _ = _default_lockstep()
    root = Path(lockstep.repo.root)
    try:
        subject = pack(name)
    except PackNotFound as e:
        raise click.ClickException(str(e)) from None

    derived = receipt_for_pack(subject)
    drift = compare(read_record(root, name), derived)

    for line in render_pack(derived):
        click.echo(line)
    click.echo("")

    # A catalog's receipt is what its author's code did, not what its author said, so comparing it
    # against what this machine derives is the check that makes a listing falsifiable. It refuses
    # outright rather than behind `--accept`: a pack that holds more than the catalog published is
    # not a decision to weigh, it is a listing that does not describe the code.
    published = _published_receipt(root, name)
    if published is not None:
        against_catalog = compare(published, derived)
        if against_catalog.widened:
            raise click.ClickException(
                f"refused: the installed {name} may do more than the catalog published "
                f"(+{', +'.join(against_catalog.widened)}). The listing does not describe this "
                f"code — check the distribution and the index before going further."
            )
        for change in against_catalog.changes:
            click.echo(f"catalog  differs: {change}")
        if not against_catalog.changes:
            click.echo("catalog  the installed code matches the published receipt")
        click.echo("")

    if drift.widened:
        click.echo("capabilities this pack did not hold when you accepted it")
        for capability in drift.widened:
            click.echo(f"  + {capability}")
        if not accept:
            raise click.ClickException(
                f"refused: {name} may now do more than this repository accepted. Read what changed "
                f"above, then say so explicitly:\n\n    in-lockstep add {name} --accept\n"
            )
        click.echo("  accepted, because --accept said so")
        click.echo("")
    for change in drift.changes:
        click.echo(f"changed  {change}")
    if drift.changes:
        click.echo("")

    state = pinning(root, subject.distribution)
    if state == "unpinned":
        click.echo(
            f"NOT PINNED    {subject.distribution or name} is not fixed to a version here, so this "
            f"receipt describes\n              code that may not be the code installed next time. "
            f"`uv add {subject.distribution or name}`."
        )
        click.echo("")
    elif state == "unknown":
        click.echo("pin           could not be checked — no uv.lock or pyproject.toml to read")
        click.echo("")

    written = write_record(root, derived)
    click.echo(f"recorded      {_relative(written, root)}")
    click.echo("              commit it: the record IS the acknowledgement, and doctor reads it.")
    click.echo("")
    click.echo("paste into .lockstep/lockstep.py:")
    click.echo("")
    for line in _bind_lines(subject, derived):
        click.echo(f"    {line}")
    click.echo("")
    click.echo(f"until you do, nothing changes: `in-lockstep ls` will not mention {name}.")


def _published_receipt(root: Path, name: str) -> dict[str, Any] | None:
    """The receipt a registered catalog published for this pack, if one is registered at all.

    Best effort on purpose. A repository with no catalogs, or one whose catalog is unreachable
    right now, still accepts packs — the local derivation is the thing that decides what a pack may
    do, and the published receipt is a cross-check that is worth having and not worth blocking on.
    """
    from .market import MarketError, read_catalog, receipt_at, sources

    for source in sources(root):
        try:
            catalog = read_catalog(source, root=root)
            for entry in catalog.entries:
                if entry.name == name and entry.receipt:
                    return receipt_at(root, entry.receipt)
        except MarketError:
            continue
    return None


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:  # pragma: no cover - a record outside the repository
        return str(path)


def _bind_lines(subject: Any, receipt: dict[str, Any]) -> list[str]:
    """The lines a person would type, derived from what the pack turned out to offer.

    Printed rather than written, and derived rather than templated: a pack offering a strategy
    gets `use()`, one offering an adapter for a verb it brought along gets `bind()` with the
    request type it named, and a pack with nothing importable gets the resource spelling — which
    is a shape, not a snippet, because where a body belongs depends on the lens it replaces.
    """
    strategies = [offer for offer in receipt["offers"] if offer["offers"] == "strategy"]
    adapters = [offer for offer in receipt["offers"] if offer["offers"] == "verb"]
    lines: list[str] = []

    for offer in strategies:
        lines += [
            f"from {subject.module} import {offer['name']}",
            f"{offer['verb']} = lockstep.use({offer['name']})",
            "",
        ]
    for offer in adapters:
        request = offer["request"] or "<RequestType>"
        lines += [
            f"from {subject.module} import {offer['name']}, {request}",
            f"lockstep.bind({request}, {offer['name']}())",
            "",
        ]
    if not lines:
        lines = [
            "from in_lockstep.packs import pack",
            "",
            f"{_handle(subject.name)} = pack({subject.name!r})",
            "",
            "# then point a prompt subclass at one of its bodies, and pass any guardrails to the",
            "# adapter's `layers=` — `plus()` appends, so the shipped baseline stays underneath:",
            f"#     body = {_handle(subject.name)}.body('prompts/<name>.md')",
            f"#     layers = review_layers().plus(guardrails={_handle(subject.name)}.guardrails('<name>'))",
        ]
    return [line for line in lines if line is not None]


def _handle(name: str) -> str:
    """A legal identifier a person would plausibly have chosen for this pack."""
    handle = "".join(character if character.isalnum() else "_" for character in name).strip("_")
    return handle if handle and not handle[0].isdigit() else f"pack_{handle}"


@pack_group.command(name="try")
@click.argument("name")
@click.option("--corpus", "extra", default="", help="Your own cases, measured alongside the pack's.")
@click.option("--model", default="anthropic:claude-sonnet-4-6", help="Only read when recording.")
@click.option("--record", is_flag=True, help="Call the provider and write the pack's cassette.")
@click.option("--json", "as_json_out", is_flag=True, help="The states and counts, for a script.")
def pack_try_cmd(name: str, extra: str, model: str, record: bool, as_json_out: bool) -> None:
    """Measure a pack before trusting it — replaying its cassettes, for nothing.

    This is what makes a catalog worth anything. Everything else about a pack can be checked but
    none of it says whether the pack is any GOOD, and the honest answer to that is a measurement
    made on your own cases rather than a number its author published. `--corpus` is where yours go,
    and they are counted separately for exactly that reason.

    Read the states, not only the rate. A case with no recorded exchange is `unrecorded` and never
    a failure — a cassette holds what its author recorded, and an absence of evidence is not
    evidence of absence. A rubric is `outstanding` until a judge answers it. A corpus family this
    cannot drive yet is counted as unexercised rather than quietly dropped, because a pass rate
    over cases nobody ran is the reassuring number this project keeps refusing to print.

    `--record` is the other direction, and the only one that spends: it calls the provider and
    writes the cassette a pack needs before anyone else can measure it for free.
    """
    from .ai.auth import Auth
    from .ai.bootstrap import (
        LLMProvider,
        MissingCredential,
        Model,
        caps_for,
        credentials_for,
        default_registry,
        table_for,
    )
    from .ai.invoker import AiInvoker
    from .ai.replay import Cassette, RecordingProvider, ReplayProvider
    from .packs import PackNotFound, pack
    from .privileged.egress import EgressPolicy
    from .privileged.redact import Redact
    from .trial import DRIVEN as _DRIVEN
    from .trial import as_json, render, run

    lockstep, _ = _default_lockstep()
    try:
        subject = pack(name)
    except PackNotFound as e:
        raise click.ClickException(str(e)) from None

    tapes = [subject.file(f"cassettes/{stem}.json") for stem in subject.cassettes()]
    if not tapes and not record:
        click.echo(f"{name} ships no cassette, so there is nothing to replay and nothing to measure.")
        click.echo("")
        click.echo("That is a fact about the pack rather than a fault in it: somebody has to spend")
        click.echo("real money once, recording what a model actually said, before everyone after")
        click.echo(f"them can measure it for nothing.  `in-lockstep pack try {name} --record`")
        raise SystemExit(EXIT_FAILED)

    target = tapes[0] if tapes else subject.file("cassettes/trial.json")
    if target is None:
        raise click.ClickException(f"{name} could not be resolved to files on disk")
    tape = Cassette.load(target)

    auth = Auth()
    try:
        registry = default_registry(auth)
    except MissingCredential as e:
        raise click.ClickException(str(e)) from None
    selected = Model(model)
    table = table_for(registry, selected, _bound_cost_table(lockstep))

    def build_invoker(ctx: Any) -> AiInvoker:
        provider: LLMProvider = ReplayProvider(tape)
        if record:
            creds = credentials_for(auth, selected.provider)
            provider = RecordingProvider(registry.provider_for(selected, creds), tape, Redact())
        return AiInvoker(
            provider,
            model=selected.name,
            # What the registration says the model can do; a schema call or a tool-holding
            # session is refused by name before the first turn (GATE-MODEL-1).
            caps=caps_for(registry, selected),
            cost_table=table,
            spend=ctx.spend,
            redact=Redact(),
            egress=(
                lockstep.container.resolve(EgressPolicy)
                if lockstep.container.has(EgressPolicy)
                else EgressPolicy.detect()
            ),
        )

    lenses = _trial_lenses(subject)
    trial = run(subject, extra=Path(extra) if extra else None, invoker_factory=build_invoker, lenses=lenses)
    if record:
        tape.save()

    if as_json_out:
        click.echo(as_json(trial))
        if trial.summary()["pass_rate"] is None:
            raise SystemExit(EXIT_FAILED)
        return

    for line in render(trial, pack=name, recording=record):
        click.echo(line)

    undriven = sorted({r.family for r in trial.results if r.state == "not exercised"})
    if undriven:
        click.echo("")
        click.echo(
            f"families a trial cannot drive yet: {', '.join(undriven)}  (it drives {', '.join(_DRIVEN)})"
        )
    if trial.summary()["pass_rate"] is None:
        raise SystemExit(EXIT_FAILED)


def _trial_lenses(subject: Any) -> dict[str, Any]:
    """The lens map a trial composes: shipped lenses, plus the pack's own.

    Composed inside the SHIPPED layer stack rather than a repository's. Measuring a pack through
    your own guardrails would measure your configuration, and two repositories would then get
    different numbers for the same pack and have no way to tell why.

    The walk is over the PACK's prompts, not the shipped names. It was the other way round, which
    meant a pack could only be measured on a lens it OVERRODE: one shipping `prompts/a11y.md` was
    never looked at, its cases resolved an aspect the adapter had never heard of, and `pack try`
    reported a working pack as broken — for exactly the pack kind the extension story is about
    (#202). `aspect_of` already reads the pairing from the other side, so the convention was
    honoured on the read and not on the build.

    A fragment is only a lens when `corpus/review/<stem>-reviewer/` exists beside it. `guardrails()`
    reads its fragments out of the same `prompts/` directory — `examples/acme-review-prompts` ships
    `house.md` there as house rules — so admitting every `.md` would turn those into a phantom lens
    emitting `review.house`. Cases are what make a lens measurable, and this function only exists to
    measure.

    Existence is checked with `is_dir()` and not by asking `Pack.file` for a truthy answer: `file()`
    has no existence check and returns a live path whenever the pack root resolves, so a guard
    written against it never fires.
    """
    from .prompts.review import LENSES, ReviewPrompt

    lenses: dict[str, Any] = dict(LENSES)
    prompts = subject.file("prompts")
    if prompts is None or not prompts.is_dir():
        return lenses

    for path in sorted(prompts.glob("*.md")):
        aspect = path.stem
        base = LENSES.get(aspect)
        if base is None:
            cases = subject.file(f"corpus/review/{aspect}-reviewer")
            if cases is None or not cases.is_dir():
                continue
            base = ReviewPrompt
        lenses[aspect] = type(
            f"Pack{aspect.title()}Prompt",
            (base,),
            {
                "version": f"{subject.name}",
                # Stated rather than inherited. A lens the pack invented subclasses `ReviewPrompt`,
                # whose `aspect` is the generic "review", and the aspect is what a finding id is
                # built from — so inheriting it would file every invented lens's findings under
                # `review.review`.
                "aspect": aspect,
                "body": subject.body(f"prompts/{aspect}.md"),
            },
        )
    return lenses


@pack_group.command(name="describe")
@click.argument("name", default="")
@click.option("--json", "as_json", is_flag=True, help="The canonical form, which is what a digest is over.")
@click.option(
    "--no-load",
    is_flag=True,
    help="Never import the pack, at the cost of knowing what it offers.",
)
def pack_describe_cmd(name: str, as_json: bool, no_load: bool) -> None:
    """Derive a receipt — for an installed pack by NAME, or for this repository.

    With a NAME, the order of operations is the point: `imports` is computed from the AST of the
    files the distribution recorded, before anything is imported, so a pack reporting `none` has
    been shown to be inert by a path that never ran it. The module is loaded afterwards, and only
    when there is something to load, to see what it offers. `--no-load` declines even that.

    With no NAME the subject is this repository: what it binds, may do, and can prove.

    Everything printed is read off objects that already declare it — `capabilities` off the bound
    adapter, the projection off the composed prompt, the merged floor off the policy stack — so
    this describes what the code does rather than what anyone said about it. No key, no network,
    no spend.

    Two fields are the ones worth reading first. `guardrails_intact` says whether a prompt still
    opens with the framework's baseline, which is legal to change and must be visible. `corpus`
    says `none` rather than a borrowed number when this repository has measured nothing.
    """
    from .receipt import canonical, receipt_for, receipt_for_pack, render, render_pack

    if name:
        from .packs import PackNotFound, pack

        try:
            subject = pack(name)
        except PackNotFound as e:
            raise click.ClickException(str(e)) from None
        pack_receipt = receipt_for_pack(subject, load=not no_load)
        if as_json:
            click.echo(canonical(pack_receipt), nl=False)
        else:
            for line in render_pack(pack_receipt):
                click.echo(line)
        return

    lockstep, _ = _default_lockstep()
    receipt = receipt_for(lockstep, root=Path(lockstep.repo.root))
    if as_json:
        click.echo(canonical(receipt), nl=False)
        return
    for line in render(receipt):
        click.echo(line)


def _shipped_compositions() -> dict[str, Composition]:
    """Every prompt the framework ships, whether or not anything is bound to run it.

    All six verbs, where this used to name three: `fix`, `rfe` and `backport` prompts shipped and
    were unreachable from the one command that renders a prompt, which made them the prompts least
    likely to be read before they ran.
    """
    from .ai.prompt import compositions
    from .prompts.backport import BACKPORT_PROMPTS, backport_layers
    from .prompts.fix import FIX_PROMPTS, fix_layers
    from .prompts.implement import PROMPTS, implement_layers
    from .prompts.review import LENSES, review_layers
    from .prompts.rfe import RFE_PROMPTS, rfe_layers
    from .prompts.triage import TRIAGE_PROMPTS, triage_layers

    index: dict[str, Composition] = {}
    for prompts, layers, verb in (
        (LENSES, review_layers(), "review"),
        (PROMPTS, implement_layers(), "implement"),
        (FIX_PROMPTS, fix_layers(), "fix"),
        (TRIAGE_PROMPTS, triage_layers(), "triage"),
        (RFE_PROMPTS, rfe_layers(), "rfe"),
        (BACKPORT_PROMPTS, backport_layers(), "backport"),
    ):
        index.update(compositions(prompts, layers, verb=verb, source="shipped"))
    return index


def _bound_compositions(lockstep: Lockstep) -> dict[str, Composition]:
    """What this repository's own bindings compose, which is what a run would actually use."""
    from .ai.prompt import Inspectable

    index: dict[str, Composition] = {}
    for binding in lockstep.container.resolved():
        if isinstance(binding.impl, type):
            continue
        if isinstance(binding.impl, Inspectable):
            index.update(binding.impl.compositions())
    return index


def _is_shipped(shipped: dict[str, Composition], label: str, composed: Composition) -> bool:
    """Whether a bound composition is the framework's own, class for class.

    Class identity, not label presence: a subclass of `SecurityReviewPrompt` that only adds
    `emphasis` is still an override, and it is exactly the override a reader would want flagged.
    And a `Lens` that adds emphasis to the shipped class without subclassing it is the same
    override spelled without a class, so it is starred for the same reason.
    """
    origin = shipped.get(label)
    return origin is not None and type(origin.prompt) is type(composed.prompt) and not composed.emphasis


def _guardrail_chains(composed: dict[str, Composition]) -> list[tuple[str, str, str]]:
    """Each distinct guardrail chain an adapter composes, with a drift note and, when there is
    more than one, the lenses it applies to.

    One line per chain rather than one for the adapter. `ls` used to print the first lens's chain
    as though it were every lens's, which was true while a stack belonged to the adapter and
    became a lie the moment a `Lens` could carry its own (#204): a lens whose stack dropped the
    baseline would have been reported as leading with it. A single chain prints exactly as it
    always did, so a repository with one stack sees no new line.
    """
    chains: dict[tuple[str, ...], list[str]] = {}
    for label in sorted(composed):
        names = tuple(name for name, _ in composed[label].layers.guardrails)
        chains.setdefault(names, []).append(label.rsplit("/", 1)[-1])
    out: list[tuple[str, str, str]] = []
    for names, labels in chains.items():
        chain = ", ".join(names) or "(none)"
        drift = "" if names and names[0] == "baseline" else "   <- baseline does not lead"
        scope = f"  ({', '.join(labels)})" if len(chains) > 1 else ""
        out.append((chain, drift, scope))
    return out


def _route_flag(key: str, verbs: set[str], lenses: tuple[str, ...] | None) -> str:
    """What `ls` says beside a route that names nothing a run would resolve.

    A route is a line somebody wrote, and a line that names a verb nothing serves or a lens
    nothing binds reads as configured while doing nothing. `review/<lens>` is checked against the
    lenses the BOUND adapter has, the same list `/review` resolves a comment against; any other
    verb's routes resolve by verb alone today, and a slash there is said to do nothing rather
    than left to look like it does.

    `lenses` is None when the bound adapter states no set (#275). Said so, rather than silent:
    an empty flag beside a route reads as "checked, and fine", which is the reassurance this
    column exists to refuse.
    """
    verb, _, lens = key.partition("/")
    if verb not in verbs:
        return "  <- no such verb (typo?)"
    if not lens:
        return ""
    if verb != "review":
        return "  <- only review routes per lens; this resolves by verb"
    if lenses is None:
        return "  <- unchecked: the bound Review adapter declares no lenses"
    return "" if lens in lenses else "  <- no such lens (typo?)"


def _resolve_prompt(index: dict[str, Composition], name: str) -> str:
    """A qualified label (`review/security`), or a bare one where it is unambiguous.

    Bare names stay supported because `--aspect security` is how a review is asked for, and a
    reader who wants to see that lens should not have to learn a second spelling. Where two verbs
    ship a prompt of the same short name, the bare form resolves to neither and says so — guessing
    would show somebody a prompt other than the one they are about to run.
    """
    if name in index:
        return name
    tails: dict[str, list[str]] = {}
    for label in index:
        tails.setdefault(label.split("/", 1)[-1], []).append(label)
    matches = tails.get(name, [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise click.ClickException(f"{name!r} is ambiguous: {', '.join(sorted(matches))}. Name the verb too.")
    raise click.ClickException(f"no prompt named {name!r}; have {', '.join(sorted(index))}")


@main.command(name="show-prompt")
@click.argument("name", default="security")
@click.option("--projection", is_flag=True, help="Print the section-identity list only.")
@click.option("--diff", "diff_shipped", is_flag=True, help="Diff this repository's against the shipped one.")
@click.option("--shipped", "shipped_only", is_flag=True, help="Ignore what this repository binds.")
def show_prompt_cmd(name: str, projection: bool, diff_shipped: bool, shipped_only: bool) -> None:
    """Render a composed prompt offline, with per-fragment provenance.

    The successor to a committed flattened prompt tree. "What was the model actually told?" needs
    an answer that costs no run and no key — a cassette requires having already paid, and `ls`
    prints the container rather than the prompt.

    It reads the prompt off the **bound adapter**, so what it renders is what a run would use.
    Until it did, this command imported the shipped maps directly: a team that followed
    `docs/extending.md`, subclassed a lens and bound it through `AiReview(lenses=...)` was shown
    the prompt they had replaced, by the one command whose entire job is saying otherwise. A
    prompt you cannot render is a prompt nobody reviews.

    The projection it prints is the same one the characterization corpus asserts on, so one
    artifact serves both offline inspection and migration equivalence.

    NAME is a review aspect (`security`), a strategy prompt (`implement/oneshot`), or any other
    shipped or house prompt. `--shipped` renders the framework's version of it and `--diff` shows
    what a repository changed, which is the review question rather than the rendering one.
    """
    shipped = _shipped_compositions()
    index = dict(shipped)
    if not shipped_only:
        lockstep, _ = _default_lockstep()
        index.update(_bound_compositions(lockstep))

    label = _resolve_prompt(index, name)
    composed = shipped[label] if shipped_only else index[label]

    # A body that cannot be read is one line naming the body and the fix, not a traceback from
    # inside the composer: the most likely adopter mistake, met by the one command whose job is
    # showing what a run would send (#311).
    from .ai.prompt import BodyNotFound

    try:
        if projection:
            for section in composed.projection():
                click.echo(section)
            return
        _show_prompt_text(shipped, label, composed, diff_shipped=diff_shipped)
    except BodyNotFound as e:
        raise click.ClickException(f"{label}: {e}") from None


def _show_prompt_text(
    shipped: dict[str, Composition], label: str, composed: Composition, *, diff_shipped: bool
) -> None:
    """The rendering half of `show-prompt`, split out so one `BodyNotFound` handler covers it."""
    import difflib

    if diff_shipped:
        origin = shipped.get(label)
        if origin is None:
            click.echo(f"# {label} is not a shipped prompt; there is nothing to diff against.")
            return
        delta = list(
            difflib.unified_diff(
                origin.text().splitlines(),
                composed.text().splitlines(),
                fromfile=f"shipped/{label}",
                tofile=f"{composed.source}/{label}",
                lineterm="",
            )
        )
        if not delta:
            click.echo(f"# {label} is the shipped prompt, unmodified.")
            return
        for line in delta:
            click.echo(line)
        return

    click.echo(f"# composed prompt: {label}  (version {composed.prompt.version})")
    click.echo(f"# source: {composed.source}")
    described = composed.prompt.describe()
    if described:
        # From the body file's own header. It is the one thing a prompt says about itself that a
        # reader wants before reading two hundred lines of it — and it is never sent to the model.
        click.echo(f"# {described}")
    click.echo("#")
    for section in composed.projection():
        click.echo(f"#   {section}")
    click.echo("")
    click.echo(composed.text())


@main.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite lockstep.py (never the trampoline).")
@click.option(
    "--implement",
    "with_implement",
    is_flag=True,
    help="Also scaffold the /implement chat-ops trampoline and its two workflows.",
)
@click.option(
    "--fix",
    "with_fix",
    is_flag=True,
    help="Also scaffold the /fix chat-ops trampoline and its two workflows (the ai-generated-issue target).",
)
@click.option(
    "--review",
    "with_review",
    is_flag=True,
    help="Also scaffold the /review <lens> chat-ops trampoline: a lens on request, posted without a key.",
)
@click.option(
    "--host",
    type=click.Choice(["github", "gitlab"]),
    default=None,
    help="Which host's trampoline to write, when the remote and the CI files do not say.",
)
def init_cmd(force: bool, with_implement: bool, with_fix: bool, with_review: bool, host: str | None) -> None:
    """Scaffold a lifecycle definition and a CI trampoline.

    The trampoline is written once and never read back: there is no drift check on it, and no
    --force for it. The day something compares it against a freshly generated one, it has become
    generated output rather than a scaffold, which is the line this framework exists on the other
    side of.
    """
    from .loader import LEGACY_MODULE_FILE, MODULE_FILE

    legacy = Path(LEGACY_MODULE_FILE)
    if legacy.exists():
        # Said before anything is written. Scaffolding a second lifecycle module beside one that
        # is no longer read is how a repository ends up with two configurations and no idea which
        # one is in effect.
        click.echo(f"{LEGACY_MODULE_FILE} exists at the repository root, which is no longer read.")
        click.echo(f"  mkdir -p .lockstep && git mv {LEGACY_MODULE_FILE} {MODULE_FILE}")
        raise SystemExit(EXIT_FAILED)

    detected = Lockstep.detect()
    root = Path(detected.repo.root).resolve()
    if root in Path.cwd().resolve().parents:
        # Detection read the root and this command writes where it stands; the two have to be
        # one place, or a module is scaffolded from one directory's facts into a subdirectory
        # (#316). Only a working directory INSIDE the root is refused: on a runner detection
        # prefers GITHUB_WORKSPACE, which a scratch directory is not under at all.
        click.echo(f"run init at the repository root: detection read {root}, and that is where")
        click.echo("the module and the trampolines are written")
        raise SystemExit(EXIT_FAILED)
    facts = detected.repo.facts
    module = Path(MODULE_FILE)
    if module.exists() and not force:
        click.echo(f"{MODULE_FILE} exists (use --force to overwrite)")
    else:
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(_scaffold_module(facts))
        click.echo(f"wrote {MODULE_FILE}")
        found = facts.summary()
        if found:
            click.echo(f"  detected {'; '.join(found)}")
        # The moment an adopter learns their stack is not served is the moment they are told what
        # was looked for -- `init` is where the bindings they will otherwise hand-write are chosen.
        declined = facts.declined()
        if declined:
            click.echo(f"  declined {declined}")

    # Relative like every other path this command writes, and unlike the cassette default, which
    # is joined to the repository root. Different commands, different failure modes: `init` is run
    # once, by a person, at the root, and prints every path it touched. `--record` is a flag on a
    # run that happens anywhere, including from a subdirectory in CI, and writes silently.
    _write_gitignore(Path(".gitignore"))

    # The trampoline the detected host can actually run: a GitLab repository gets
    # `.gitlab-ci.yml`, not a `.github/workflows/` file it would silently ignore.
    from .platform.hosted import detect_host, origin_url

    host = host or detect_host()
    if not host and origin_url():
        # A self-hosted GitLab at `git.corp.example` has neither host's name in its remote and no
        # CI file yet, and `init` used to write it a GitHub workflow its host ignores, silently
        # (#314). The remote is a real answer that says "not GitHub", so nothing is written and
        # the two flags that would are named. A repository with no remote at all has said nothing
        # yet, and keeps the GitHub default below, said out loud. The module and the ignore lines
        # above are host-neutral and stay either way.
        click.echo("")
        click.echo("no CI trampoline written: nothing says which host this repository runs on.")
        click.echo("  looked for .github/workflows/, .gitlab-ci.yml, and 'github' or 'gitlab' in")
        click.echo("  the origin remote's URL. Say which: `init --host github` or `init --host gitlab`.")
        _disclose_what_a_run_keeps()
        return
    if not host:
        click.echo(
            "no origin remote yet, so the GitHub trampoline is written; `init --host gitlab` for the other."
        )
        host = "github"
    # The provider each trampoline installs and guards on is the one the module routes the verb
    # to, read now that the module exists; a module with no route gets the CLI's own default.
    try:
        configured, _recorder = _default_lockstep()
    except Exception:  # noqa: BLE001 - a module this cannot load still gets a trampoline for the default
        configured = None
    if host == "gitlab":
        if _write_trampoline(
            Path(".gitlab-ci.yml"), _SCAFFOLD_GITLAB_TRAMPOLINE, provider=_provider_for(configured, "review")
        ):
            click.echo("")
            click.echo("One active job, because reviewing is read-only, and a publish job that keeps")
            click.echo("its record. The gate/work/propose split for write-capable verbs is in the")
            click.echo("same file and active -- inert until a pipeline runs with LOCKSTEP_ISSUE (and")
            click.echo("LOCKSTEP_VERB=implement or fix) or LOCKSTEP_IMPROVE on the default branch.")
            click.echo("The file says which credential each job needs; docs/trampoline.md is the")
            click.echo("contract.")
            _gitlab_things_to_decide(facts)
    elif _write_trampoline(
        Path(".github/workflows/lockstep.yml"),
        _SCAFFOLD_TRAMPOLINE,
        provider=_provider_for(configured, "review"),
    ):
        click.echo("")
        click.echo("One job, because reviewing is read-only. Add the privileged `apply` job the")
        click.echo("day a verb of yours produces a change to write; the file says where.")

    if with_implement:
        _scaffold_implement(module, facts, provider=_provider_for(configured, "implement"), host=host)
    if with_fix:
        _scaffold_fix(module, facts, provider=_provider_for(configured, "fix"), host=host)
    if with_review:
        _scaffold_review(provider=_provider_for(configured, "review"), host=host)

    _disclose_what_a_run_keeps()


#: What `init` adds to a repository's `.gitignore`. Named individually rather than ignoring
#: `.lockstep/` and negating `lockstep.py` back in: a negation is one `!` away from silently
#: untracking the lifecycle definition, and a repository then runs on detected defaults with
#: nobody noticing. This is the same shape as this repository's own, for the same reason.
_SCAFFOLD_IGNORE = """\
# in-lockstep. `.lockstep/` holds two kinds of thing and only one of them is yours to commit.
#
# COMMITTED: `.lockstep/lockstep.py` is the lifecycle definition. It is executed, not parsed, so
# it belongs in review like any other code and is deliberately absent from the list below.
#
# SCRATCH: everything here belongs to one machine's attempt at one run. A cassette holds the
# request verbatim -- the whole composed prompt and the whole diff that was sent. A transcript
# holds every message and every tool result. Those two are the files most likely to be committed
# by a `git add .` after a run that went badly, which is when nobody is reading the diff.
#
# Run records are not here, because they are meant to survive a machine: they go to an orphan
# branch, append-only and tamper-checked.
.lockstep/runs/
.lockstep/cassettes/
.lockstep/cases/
.lockstep/ledger/
.lockstep/transcripts/
.lockstep/__pycache__/
"""


def _gitlab_things_to_decide(facts: Any) -> None:
    """What a GitLab adopter has to do by hand, printed once, the way `init --implement` prints
    its list on GitHub. Rendering: every item is a fact the scaffold already states in a comment."""
    stack = str(getattr(facts, "stack", "") or "")
    click.echo("")
    click.echo("Things to decide before it runs:")
    click.echo("  1. Scope IN_LOCKSTEP_SECRET to the lockstep-work environment, or federate the")
    click.echo("     provider with this GitLab's id_tokens; the work jobs say which.")
    click.echo("  2. Set the IN_LOCKSTEP_ORG_SPEND_LIMIT CI variable to the cap you set in the")
    click.echo("     provider console: `doctor` is a required step and refuses without it.")
    click.echo("  3. Scope a write-capable project token (api) as GITLAB_TOKEN to lockstep-propose")
    click.echo("     and lockstep-publish; a read_api one to lockstep-work.")
    if stack and stack != "python":
        click.echo("  4. Name the work jobs' image: it has to carry Python for the framework AND the")
        click.echo(f"     {stack} toolchain for the suite; the uv image carries Python and uv only, and")
        click.echo("     no image on a registry carries both by default.")
    else:
        click.echo("  4. The work jobs' image carries Python and uv, which a uv or pip repository's")
        click.echo("     provision and suite need; change it if the suite needs more.")
    click.echo("  5. A pipeline schedule on the default branch with LOCKSTEP_IMPROVE=1 runs the")
    click.echo("     learning loop; without one it never runs.")


def _write_gitignore(path: Path) -> None:
    """Ignore what a run writes, appending rather than replacing.

    `init` wrote none at all, while `docs/getting-started.md` told adopters `.lockstep/cassettes/`
    was gitignored and three module docstrings asserted it in prose. The property held in exactly
    one repository -- this one, where a person typed the lines by hand.

    Appended, never overwritten, and only the entries that are missing. An adopter's `.gitignore`
    is theirs, `--force` is scoped to the lifecycle module on purpose, and running `init` twice
    should not stack the block.
    """
    wanted = _SCAFFOLD_IGNORE.splitlines()
    if not path.exists():
        sink.write_text_atomic(path, _SCAFFOLD_IGNORE)
        click.echo(f"wrote {path}")
        return
    have = {line.strip() for line in path.read_text().splitlines()}
    missing = [line for line in wanted if line.startswith(".lockstep/") and line not in have]
    if not missing:
        click.echo(f"{path} already ignores what a run writes")
        return
    sink.append_text(
        path,
        "\n# in-lockstep: what a run writes. `.lockstep/lockstep.py` is not here on purpose.\n"
        + "\n".join(missing)
        + "\n",
    )
    click.echo(f"appended {len(missing)} line(s) to {path}")


def _disclose_what_a_run_keeps() -> None:
    """Say what a recording holds, where it goes and for how long, at the moment it starts.

    Recording is on by default everywhere now, not only in what this scaffolds -- an inference
    nobody kept is an opportunity spent and discarded -- and it is exactly the kind of default that
    has to be said out loud rather than discovered. That is what makes this the load-bearing half
    of the change rather than a courtesy: a default nobody was told about is a surprise, and the
    surprise here is a file on their disk holding a prompt and a diff. What follows is the whole
    disclosure: a person who reads only this knows what leaves their machine and what stays, and
    how to decline.
    """
    click.echo("")
    click.echo("What a run keeps:")
    click.echo("  A recording holds the request verbatim. For `review` that is the composed")
    click.echo("  prompt and the diff. For `implement` and `fix` it is more: every file the")
    click.echo("  model chose to open, every command's output, and a whole copy of each file")
    click.echo("  it wrote -- because a session re-sends its history every turn.")
    click.echo("  Redaction masks the credential shapes it knows; it never masks source.")
    click.echo("")
    click.echo(f"  Locally: {CASSETTE_DIR}/<verb>.json, gitignored. `--no-record` declines,")
    click.echo("  per run; nothing is kept for a run you decline, and nothing asks again.")
    click.echo("  In CI the recording is written OUTSIDE the checkout, and dies with the runner.")
    click.echo("  What survives is the cases harvested from it: the question that was asked, and")
    click.echo("  which files and commands the session reached for -- addresses, not contents.")
    click.echo("  Each case says what it left out, with a count, a size and a digest, so the")
    click.echo("  omission can be checked rather than taken on trust. They ride the run artifact,")
    click.echo("  which says how many days it is kept.")
    click.echo("  Run records are the exception and are meant to survive: an orphan branch.")


def _write_trampoline(path: Path, template: str, *, provider: str = "anthropic") -> bool:
    """Write a CI trampoline once, pinning the framework version. Returns whether it wrote.

    The version writing the scaffold is the version the scaffold installs: unpinned, every
    adopting repository floats on whatever the registry serves next, and one breaking release
    refuses them all at once in the job that holds the provider key. Left alone if it exists —
    a trampoline is never regenerated, or it has become generated output rather than a scaffold.
    """
    if path.exists():
        click.echo(f"{path} exists — left alone, deliberately")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_trampoline(template, provider))
    click.echo(f"wrote {path}")
    return True


#: What a trampoline needs to run a route at a provider: the extra that installs its SDK, and the
#: secrets the job is guarded on and handed, first one first. Every trampoline used to name
#: `anthropic` and `ANTHROPIC_API_KEY` outright, so a module routing review to Bedrock got a
#: green check that never reviewed (#316). A provider not listed here is refused by name at
#: `init` rather than scaffolded with a guard that would skip forever (O11).
_PROVIDER_CI: dict[str, tuple[str, tuple[str, ...]]] = {
    "anthropic": ("anthropic", ("ANTHROPIC_API_KEY",)),
    "bedrock": ("bedrock", ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_REGION")),
    "vertex": ("google", ("GOOGLE_APPLICATION_CREDENTIALS",)),
    "gemini": ("google", ("GOOGLE_APPLICATION_CREDENTIALS",)),
}


def _ci_recipe(provider: str) -> tuple[str, tuple[str, ...]]:
    """The extra and the secrets a trampoline for `provider` carries, or a refusal naming it."""
    recipe = _PROVIDER_CI.get(provider)
    if recipe is None:
        known = ", ".join(sorted(_PROVIDER_CI))
        raise click.ClickException(
            f"init cannot write a CI trampoline for a route to {provider!r}: it does not know which "
            f"extra installs that provider or which secret carries its credential (known: {known}). "
            f"Route CI at one of those, or write the trampoline by hand from docs/trampoline.md."
        )
    return recipe


def _render_trampoline(template: str, provider: str) -> str:
    """The template with its version and provider placeholders filled.

    `IN_LOCKSTEP_SECRET` on a line of its own inside an `env:` block is the credential handed to
    the step, and becomes one line per secret the recipe names, at the same indentation; every
    other occurrence -- the guard, the skip message -- is the first secret's name.
    """
    extra, secrets = _ci_recipe(provider)
    out: list[str] = []
    for line in template.replace("IN_LOCKSTEP_VERSION", __version__).splitlines(keepends=True):
        stripped = line.strip()
        if stripped in ("IN_LOCKSTEP_SECRET: ${{ secrets.IN_LOCKSTEP_SECRET }}",):
            indent = line[: len(line) - len(line.lstrip())]
            out.extend(f"{indent}{name}: ${{{{ secrets.{name} }}}}\n" for name in secrets)
            continue
        out.append(line.replace("IN_LOCKSTEP_EXTRA", extra).replace("IN_LOCKSTEP_SECRET", secrets[0]))
    return "".join(out)


def _provider_for(lockstep: Any, verb: str) -> str:
    """The provider a scaffolded trampoline for `verb` runs at: the module's route, else the
    default the CLI itself would choose for the verb."""
    from .ai.bootstrap import Model, routed_model

    routes = dict(getattr(getattr(lockstep, "models", None), "routes", None) or {})
    routed = routed_model(routes, verb) if routes else ""
    return Model(routed).provider if routed else "anthropic"


#: The container a write verb's `run_script` and suite run in, by the one stack detection found.
#: One stack, because a repository that is two is one nobody can pick a base image for.
_STACK_IMAGES = {
    "python": "docker.io/library/python:3.12-slim",
    "node": "docker.io/library/node:22-slim",
    "go": "docker.io/library/golang:1.23",
    "rust": "docker.io/library/rust:1-slim",
    "jvm": "docker.io/library/eclipse-temurin:21",
    # Of the seven `GATE-TOOLING-3` stacks, the five whose official image carries the whole
    # toolchain the bound commands need. `cpp` and `bazel` are left to the stub on purpose: no
    # library image ships CMake with a compiler, or Bazel at all, and naming one would be the
    # `python:3.12-slim`-for-everything mistake #316 removed.
    "ruby": "docker.io/library/ruby:3.3-slim",
    "php": "docker.io/library/php:8.3-cli",
    "elixir": "docker.io/library/elixir:1.17-slim",
    "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0",
    "swift": "docker.io/library/swift:5.10",
}


def _sandbox_image(facts: Any) -> str:
    """The image to scaffold, or `""` when nothing here says which: a Dockerfile (the repository
    already states its environment, and the image it builds is not a name detection can know), a
    stack of two, or no stack at all. `python:3.12-slim` for every stack was a Node repository
    getting a `run_script` container with no toolchain (#316)."""
    if getattr(facts, "dockerfile", False):
        return ""
    return _STACK_IMAGES.get(str(getattr(facts, "stack", "") or ""), "")


def _verb_config(template: str, facts: Any, *, guarded: bool) -> str:
    """A write verb's module block, fitted to what detection found: the Test line the plain
    scaffold writes (a stub when nothing was placed), and a workshop whose image is derived or
    a stub saying why not."""
    imports: list[str] = ["Test"]
    test_bind = _bind_line(facts, "Test", imports)
    if test_bind.startswith("lockstep.bind(Test,"):
        # The plain scaffold already wrote this line above; repeating it would rebind. `startswith`,
        # because the stub's own example line is a commented `lockstep.bind(Test, ...)`. What is
        # written is the rebind with a container, commented: the image is the adopter's to name.
        contained = test_bind[:-2] + ', sandbox=Sandbox(image="ghcr.io/you/ci:tag", require_container=True))'
        test_bind = (
            "# Test is bound above, from what detection found (`in-lockstep ls` prints it), on the host\n"
            "# runner. A test a model stages is refused there until Test is rebound with a container\n"
            "# whose image carries the suite's dependencies:\n"
            "#   from in_lockstep.adapters.sandbox import Sandbox\n"
            f"#   {contained}"
        )
    image = _sandbox_image(facts)
    if image:
        # Two lines at two positions, because ruff's isort sorts `in_lockstep` before
        # `in_lockstep.adapters.ai` and `adapters.sandbox` after it, and the adopter's own
        # Validate checks the file `init` just wrote.
        workshop_import = "from in_lockstep import Workshop\n"
        sandbox_import = "from in_lockstep.adapters.sandbox import Sandbox\n"
        workshop = f'Workshop(commands=Sandbox(image="{image}", require_container=True))'
        lines = f"lockstep.workshop = {workshop}"
        if guarded:
            lines = f"if lockstep.workshop.commands is None:\n    lockstep.workshop = {workshop}"
    else:
        why = (
            "a Dockerfile is here, and the image it builds is yours to name"
            if getattr(facts, "dockerfile", False)
            else f"the detected stack is {getattr(facts, 'stack', '') or 'nothing detection recognises'}"
        )
        workshop_import = sandbox_import = ""
        lines = (
            f"# No container image was derived: {why}.\n"
            f"# Until one is named, `run_script` is declared and withheld, and the suite is not run\n"
            f"# over a staged change. Bind one:\n"
            f"#   from in_lockstep import Workshop\n"
            f"#   from in_lockstep.adapters.sandbox import Sandbox\n"
            f"#   lockstep.workshop = Workshop(\n"
            f'#       commands=Sandbox(image="ghcr.io/you/ci:tag", require_container=True)\n'
            f"#   )"
        )
    return (
        template.replace("__WORKSHOP_IMPORT__", workshop_import)
        .replace("__SANDBOX_IMPORT__", sandbox_import)
        .replace("__WORKSHOP__", lines)
        .replace("__TEST_BIND__", test_bind)
    )


def _binds_lockstep(text: str) -> bool:
    """Whether a module assigns a top-level name `lockstep` — what the appended block binds onto.

    Parsed, not searched: a substring check passes on `# lockstep` in a comment, and the append
    then fails at load with a NameError the compile() guard never sees. Empty text is not a
    binding, so an empty pre-existing module is refused rather than turned into a broken one.
    """
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == "lockstep" for t in targets):
            return True
    return False


def _import_names(clause: str) -> tuple[str, str]:
    """`Outcome as Status` -> ("Outcome", "Status"); `Status` -> ("Status", "Status")."""
    source, _, alias = clause.partition(" as ")
    return source.strip(), (alias.strip() or source.strip())


def _without_duplicate_imports(existing: str, block: str) -> str:
    """The appended block, minus imports the module already has from the same module path.

    `--implement` and `--fix` each append a block that stands alone, so each imports everything it
    uses. Run both and the second block re-imports seven names the first already brought in, which
    is `F811` in the file `init` has just written (#190) — a red `validate` on the adopter's first
    selfcheck, in generated code.

    Matched on the module path, the name imported, AND the name it is bound to. All three,
    because any two of them can agree while the object differs: a repository importing its own
    `Status` from somewhere else is a different module, and `Outcome as Status` beside a plain
    `Status` is the same module and the same bound name and still a different object. The only
    thing dropped is a line that would import exactly what is already there, which is the only
    case where dropping cannot change what a name means.
    """
    import ast

    try:
        tree = ast.parse(existing)
    except SyntaxError:  # pragma: no cover - the caller refuses an unparseable module first
        return block
    # Module scope only, on both sides. A block imports inside a function when it wants the cost
    # paid per call, and a local import in the existing text does not put the name where a
    # module-level use in the appended block could see it. Treating one as covering the other
    # would drop an import the block needs.
    have = {
        (node.module or "", alias.name, alias.asname or alias.name)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    out: list[str] = []
    for line in block.splitlines(keepends=True):
        match = re.match(r"^from ([\w.]+) import ([^#]+)$", line.rstrip("\n"))
        if match is None:
            out.append(line)
            continue
        module = match.group(1)
        names = [n.strip() for n in match.group(2).split(",") if n.strip()]
        kept = [n for n in names if (module, *_import_names(n)) not in have]
        if not kept:
            continue
        out.append(line if len(kept) == len(names) else f"from {module} import {', '.join(kept)}\n")
    return "".join(out)


def _without_duplicate_definitions(existing: str, block: str) -> str:
    """The block, minus a top-level definition the module already has **character for character**.

    The other half of #190, missed because it fixed the imports. `--implement` and `--fix` each
    append a block that stands alone, and both carry `_last_unsuccessful` — so `init --implement
    --fix` wrote it twice, the second shadowing the first, in the file `init` had just generated.
    Ruff cannot see it (the first copy is called before the second is defined, so `F811` correctly
    does not fire) and mypy is not run over a lifecycle module by anything we ship, which is how
    it reached this repository's own `.lockstep/lockstep.py` and sat there.

    Byte-identical is the whole condition, and it is the same argument `_without_duplicate_imports`
    makes one function up: dropping a definition that is exactly the definition already present is
    the only case where dropping cannot change what a name means. Two functions of one name whose
    bodies differ are a real conflict, and the honest response to one is to leave both and let the
    adopter's type checker say so — not to pick a winner silently.

    Comment lines immediately above the definition go with it. They document that function and
    nothing else, and leaving them behind produces a paragraph attached to whatever follows.
    """
    import ast

    try:
        have = {
            node.name: ast.get_source_segment(existing, node)
            for node in ast.parse(existing).body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
        tree = ast.parse(block)
    except SyntaxError:  # pragma: no cover - the caller refuses an unparseable module first
        return block
    if not have:
        return block

    lines = block.splitlines(keepends=True)
    drop: set[int] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if have.get(node.name) != ast.get_source_segment(block, node):
            continue
        # `lineno` is the `def`; a decorator sits above it and is not part of the node's span.
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)]) - 1
        while start > 0 and lines[start - 1].lstrip().startswith("#"):
            start -= 1
        end = node.end_lineno or node.lineno
        # Trailing blank lines, so removing a block does not leave the gap it was sitting in.
        while end < len(lines) and not lines[end].strip():
            end += 1
        drop.update(range(start, end))
    return "".join(line for i, line in enumerate(lines) if i not in drop)


_SCAFFOLD_IMPLEMENT_CONFIG = """__WORKSHOP_IMPORT__from in_lockstep.adapters.ai import Oneshot
__SANDBOX_IMPORT__from in_lockstep.middleware.approval import ApprovalGate
from in_lockstep.platform.hosted import hosted_scm, hosted_tickets
from in_lockstep.platform.scm import Scm
from in_lockstep.platform.tickets import TicketSource

# -- /implement: the chat-ops implementing verb -------------------------------------------------
#
# Two workflows rather than one, because they must not be one process. `implement/from-ticket`
# runs unprivileged with the provider key and stages a change into an artifact;
# `implement/propose` runs privileged with a write token and no provider key. The trampoline in
# .github/workflows/implement.yml holds the trigger, the job split and the credentials — and
# nothing else. Run the first half locally with:
#
#     in-lockstep run implement/from-ticket --arg ticket='#42' --approve --budget 2.00



# Bound here rather than constructed inside the workflows, so `in-lockstep ls` can print what
# will actually run and a test can substitute either one. `hosted_*` bind the detected host's
# adapters — GitHub or GitLab — so this block runs unedited on either.
lockstep.bind(TicketSource, hosted_tickets())
lockstep.bind(Scm, hosted_scm())

lockstep.models.route("implement", "anthropic:claude-sonnet-4-6")

# An adapter that both spends money and writes files needs an approval path, or the run refuses
# to start. The grant itself arrives on the run context — `--approve` from a person at a
# terminal, `--approved-by` from a verified trigger — which is what lets the SAME command serve
# both without a rewrite.
lockstep.middleware += [ApprovalGate()]

# `run_script` executes inside a container with no network and no credentials in its
# environment; the absence of a container runtime is a refusal, never a fall back to this host.
# The image is named because `require_container=True` with no image refuses every command — it
# has nothing to run them in — so leaving it blank would make run_script permanently inert.
#
# Wrapped in `WorktreeRunner`, so a model's command runs in a throwaway worktree of HEAD rather
# than the live tree: the container still bind-mounts read-write, but what it mounts is a copy that
# is discarded, so a command cannot write `.git/hooks` or `.lockstep/lockstep.py` on the real
# repository past ChangeGuard. It does mean git-dependent commands see a linked worktree's gitlink
# (see docs/extending.md); pytest, ruff and mypy do not care.
# The strategy IS the adapter: `ls` prints `Implement -> Oneshot`, and that line is the whole
# answer to "how does implementing happen here". Swap `Oneshot` for `TDD` to require
# red-then-green. The model comes from the `models.route("implement", ...)` line above; the
# invoker is assembled per run, so no factory is threaded here.
__WORKSHOP__
lockstep.use(Oneshot)

# Test runs after the change is staged — against a throwaway worktree of HEAD plus the change — and
# its verdict rides the artifact into the PR body, so a reviewer sees whether the change passed
# before opening it. The suite that runs there contains a file the MODEL wrote, on a ticket
# nobody vetted, so the framework runs it only in a container with no network and a single
# writable mount; a Test whose runner would put it on this host is refused by name
# (`sandbox.host_fallback`) before the worktree is made. The default Sandbox the section above
# binds is that host runner -- right for `run selfcheck` over your own committed suite, and not
# enough here.
#
# Nothing is bound here. The section above binds whatever detection found -- `CommandTest(["npm",
# "test"])` on a Node repository -- and this block used to fall back to pytest when it found
# nothing, which was a runner invented for a stack detection could not place: the wrong default
# that runs. The image a container needs is the one that carries your suite's dependencies, which
# a stack's base image does not (the worktree is a copy of HEAD, so `.venv` and `node_modules`
# are not in it), and detection cannot know its name -- so it is named below as the line to
# write, not written. `mounts=` on `Sandbox` puts an environment the host built inside the
# container read-only; docs/extending.md shows both shapes.
__TEST_BIND__

# EGRESS, and read this before shipping the implement verb. The review scaffold above already
# bound `UnsandboxedEgress`, and that binding is global — so this implementing verb inherits it,
# and a successful injection in the untrusted issue text has somewhere to send what it read. That
# was a low-risk call for review (an empty tool set, nothing to exfiltrate through); it is a
# larger one here, because implement holds write and execute tools.
#
# What still bounds an implementing session under that binding: writes are STAGED into a
# ChangeSet and applied by nobody until a person runs `apply`, ChangeGuard stands at every write
# path with `.lockstep/lockstep.py` first in its deny list, and `run_script` runs only inside a
# no-network container (the Sandbox above) and refuses rather than falling back to this host. If
# your host CAN enforce egress (a self-hosted runner, a constrained container), delete the
# `UnsandboxedEgress` binding in the review section and set `IN_LOCKSTEP_EGRESS=enforced` there
# instead — the probe has to pass, which on a hosted runner it will not.

"""

_SCAFFOLD_FIX_CONFIG = """__WORKSHOP_IMPORT__from in_lockstep.adapters.ai import DiagnoseThenFix
__SANDBOX_IMPORT__from in_lockstep.middleware.approval import ApprovalGate
from in_lockstep.platform.hosted import hosted_scm, hosted_tickets
from in_lockstep.platform.scm import Scm
from in_lockstep.platform.tickets import TicketSource

# --- appended by `in-lockstep init --fix` --------------------------------------------------------
# The `/fix` chat-ops flow, and the target the ai-generated-issue hook routes to. Everything the
# comment does is Python here; .github/workflows/fix.yml holds only what CI owns. Bug Fix
# reproduces the bug as a failing test, fixes it, and proves both — see `fix/diagnose-then-fix`.


# Each binding is guarded, so this block works on its own and also composes with the implement
# scaffold without binding TicketSource, Scm or the approval gate a second time. `hosted_*`
# bind the detected host's adapters — GitHub or GitLab — so the block runs unedited on either.
# Test is not bound here: the section above binds what detection found, and a runner detection
# could not place is not invented (the stub below says how to bind one). The reproducer the model
# stages runs only in a container -- see the implement block, or the stub, for the line.
if not lockstep.container.has(TicketSource):
    lockstep.bind(TicketSource, hosted_tickets())
if not lockstep.container.has(Scm):
    lockstep.bind(Scm, hosted_scm())
if not any(getattr(m, "provides_approval", False) for m in lockstep.middleware):
    lockstep.middleware += [ApprovalGate()]
__TEST_BIND__

lockstep.models.route("fix", "anthropic:claude-sonnet-4-6")

# Fix writes and executes, like implement: a reproducer and a fix, each run in a throwaway worktree
# inside a no-network container, so a model's command reaches neither the network nor the real
# .git/.lockstep past ChangeGuard.
# The strategy IS the adapter: `ls` prints `Fix -> DiagnoseThenFix`. The model comes from the
# `models.route("fix", ...)` line above; the invoker is assembled per run.
__WORKSHOP__
lockstep.use(DiagnoseThenFix)

"""

REGISTER_TAIL = """
# -- the {family} process ----------------------------------------------------------
#
# Registered, not copied. These are framework code you can read at
# `in_lockstep/workflows/{family}.py`; the bindings above are yours.
#
# That split is the point. What a repository decides -- which adapters, which model, what the
# workshop grants, what the middleware chain is -- lives here. How the process runs is the
# framework's, and a fix to it reaches you by upgrading rather than never.
#
# A call rather than an import side effect: `@workflow` refuses a repeated id, so a module that
# defines its own `{family}/from-ticket` AND called this would be asking for two things under one
# name, and should hear about it.
#
# `in-lockstep show-workflow {family}` prints the source, so the process is readable without
# being a copy you then own and stop getting fixes for.
from in_lockstep.workflows import {family} as {family}_workflows

{family}_workflows.register()

"""


def _scaffold_implement(module: Path, facts: Any, *, provider: str, host: str = "") -> None:
    """The `/implement` chat-ops flow: a three-job trampoline, and the two workflows it fires.

    The headline feature used to require reverse-engineering this repository's own trampoline.
    The YAML holds only what CI owns — trigger, job split, credentials — and everything the
    comment actually does is appended to lockstep.py as Python. On GitLab the YAML half already
    lives in the scaffolded `.gitlab-ci.yml` (its gate/work/propose jobs), so only the Python half
    is appended here.
    """
    if host == "gitlab":
        click.echo("gitlab: the gate/work/propose jobs in .gitlab-ci.yml run implement/from-ticket")
        click.echo("        and implement/propose when a pipeline runs on the default branch with")
        click.echo("        LOCKSTEP_ISSUE set (LOCKSTEP_VERB defaults to implement). Scope their")
        click.echo("        credentials first; the file says which.")
    else:
        _write_trampoline(
            Path(".github/workflows/implement.yml"), _SCAFFOLD_IMPLEMENT_TRAMPOLINE, provider=provider
        )

    text = module.read_text() if module.exists() else ""
    config = _verb_config(_SCAFFOLD_IMPLEMENT_CONFIG, facts, guarded=False)
    # The old id is checked too: a module scaffolded before the `from-ticket` rename already has
    # the block, and appending a second copy would register duplicate workflows.
    if "implement/from-ticket" in text or "implement/from-issue" in text:
        click.echo(f"{module} already defines implement/from-ticket — left alone")
    elif not _binds_lockstep(text):
        # The block appends `lockstep.bind(...)` calls, so a module that does not assign a
        # top-level `lockstep` would take a NameError on its next load. This is an AST check, not
        # a substring one: the string "lockstep" in a comment or a literal (`# lockstep config`)
        # would pass a text search, compile cleanly, and fail only at load — a NameError the
        # compile() guard below cannot see. Rather than clobber a file we do not understand, print
        # where to find the block.
        click.echo(f"{module} is not a recognisable lockstep module — not modifying it.")
        click.echo("Add the implement workflows by hand; the block to paste is in the docs, or run")
        click.echo("`in-lockstep init --implement` in a fresh directory to see it.")
    else:
        tail = REGISTER_TAIL.format(family="implement")
        block = config + _without_duplicate_imports(config, tail)
        merged = text + _without_duplicate_definitions(text, _without_duplicate_imports(text, block))
        try:
            # Never leave a module that will not import: a bad append breaks every later command,
            # not just this one. Refuse before writing rather than after loading.
            compile(merged, str(module), "exec")
        except SyntaxError as e:
            click.echo(f"{module} would not parse after adding the implement block ({e}); left alone.")
        else:
            module.write_text(merged)
            click.echo(f"extended {module} with implement/from-ticket and implement/propose")

    click.echo("")
    click.echo("Things to decide before it runs:")
    click.echo(f"  1. Set the {_ci_recipe(provider)[1][0]} repository secret; the trampoline guards on it.")
    click.echo("  2. Set the IN_LOCKSTEP_ORG_SPEND_LIMIT repository variable to the cap you set in the")
    click.echo("     provider console: `doctor` is a required step and refuses without it.")
    image = _sandbox_image(facts)
    if image:
        click.echo(f"  3. The run_script image is {image}, derived from the detected stack; change it if")
        click.echo("     a model's command needs more than the base image carries.")
    else:
        click.echo("  3. Name the container image `run_script` runs in; the appended block says why none")
        click.echo("     was derived and where the line goes.")
    click.echo("  4. Name the image a model-staged test runs in: one that carries the suite's own")
    click.echo("     dependencies, which no stack image does. Until Test is rebound with it (the appended")
    click.echo("     block has the line), a run that stages a test is refused: sandbox.host_fallback.")
    click.echo("  5. Optionally add required reviewers to the `implement` environment in repository")
    click.echo("     settings — that makes the propose job an approval in the system of record.")
    click.echo("  6. Read the EGRESS note in the appended block: the review scaffold's")
    click.echo("     UnsandboxedEgress binding is global, so this write-capable verb inherits it.")
    click.echo("     The comment names what still bounds a session, and how to enforce egress.")


def _scaffold_fix(module: Path, facts: Any, *, provider: str, host: str = "") -> None:
    """The `/fix` chat-ops flow: a three-job trampoline and the two workflows it fires.

    `fix/from-ticket` is also the target the ai-generated-issue hook routes to: `ai-generated.yml`
    fires on an issue labeled `ai-generated` and runs the same workflow, closing the loop where a
    failed fix opens the next issue. The appended block guards each shared binding, so it composes
    with `--implement` without binding TicketSource, Scm, Test or the approval gate twice.
    """
    if host == "gitlab":
        # It used to say the jobs were "already in .gitlab-ci.yml" while the file ran implement
        # and nothing else (#314); the verb the jobs run is a pipeline variable now.
        click.echo("gitlab: the gate/work/propose jobs in .gitlab-ci.yml run fix/from-ticket and")
        click.echo("        fix/propose when a pipeline runs on the default branch with LOCKSTEP_ISSUE")
        click.echo("        set and LOCKSTEP_VERB=fix. GitLab has no issue-label trigger, so the")
        click.echo("        ai-generated loop is a pipeline run the same way. Scope the credentials")
        click.echo("        first; the file says which.")
    else:
        _write_trampoline(Path(".github/workflows/fix.yml"), _SCAFFOLD_FIX_TRAMPOLINE, provider=provider)
        _write_trampoline(
            Path(".github/workflows/ai-generated.yml"), _SCAFFOLD_AI_GENERATED_TRAMPOLINE, provider=provider
        )

    text = module.read_text() if module.exists() else ""
    config = _verb_config(_SCAFFOLD_FIX_CONFIG, facts, guarded=True)
    # The old id is checked too — see `_scaffold_implement`.
    if "fix/from-ticket" in text or "fix/from-issue" in text:
        click.echo(f"{module} already defines fix/from-ticket — left alone")
    elif not _binds_lockstep(text):
        click.echo(f"{module} is not a recognisable lockstep module — not modifying it.")
        click.echo("Run `in-lockstep init --fix` in a fresh directory to see the block.")
    else:
        tail = REGISTER_TAIL.format(family="fix")
        block = config + _without_duplicate_imports(config, tail)
        merged = text + _without_duplicate_definitions(text, _without_duplicate_imports(text, block))
        try:
            compile(merged, str(module), "exec")
        except SyntaxError as e:
            click.echo(f"{module} would not parse after adding the fix block ({e}); left alone.")
        else:
            module.write_text(merged)
            click.echo(f"extended {module} with fix/from-ticket and fix/propose")


def _scaffold_review(*, provider: str, host: str = "") -> None:
    """The `/review <lens>` chat-ops flow: a three-job trampoline, and nothing appended to the
    module.

    Nothing appended, because there is nothing to append. The two write verbs each register a
    pair of workflows and bind the ports they need; a review on request runs the same `review`
    command the `pull_request` trampoline runs, against the adapter the module already binds --
    the shipped one, or whatever `AiReview(lenses=...)` a repository wrote. What the comment may
    name is exactly that adapter's lens list, which `ls` prints, so the flow is real the moment
    the trampoline exists (#204).

    GitLab CI has no issue-comment trigger, so there is no file to write there. `.gitlab-ci.yml`'s
    review job already runs on every merge request; a lens on request is `in-lockstep review
    --aspect <lens>` from a laptop, or a pipeline run with variables driving the same command --
    the shape the work job takes for `LOCKSTEP_ISSUE`.
    """
    if host == "gitlab":
        click.echo("gitlab: no issue-comment trigger exists, so there is no /review file to write.")
        click.echo("        The review job in .gitlab-ci.yml runs on every merge request; a lens on")
        click.echo("        request is `in-lockstep review --aspect <lens>`, locally or from a")
        click.echo("        pipeline run with variables, the way the work job takes LOCKSTEP_ISSUE.")
        return
    if _write_trampoline(
        Path(".github/workflows/review.yml"), _SCAFFOLD_REVIEW_TRAMPOLINE, provider=provider
    ):
        click.echo("")
        click.echo("Three jobs, because posting is a write: `gate` holds no credential, `review`")
        click.echo("holds the provider key and reads, `post` holds the write token and no provider")
        click.echo("SDK. The lens a comment names is resolved in Python against the lenses your")
        click.echo("module binds -- `in-lockstep ls` prints them -- and the pull request's refs are")
        click.echo("asked of the host, because a comment event carries neither.")


def _scaffold_module(facts: Any) -> str:
    """The lifecycle scaffold, reflecting what detection found in the tree.

    The deterministic-verb binds are generated from the facts, the same way `detected_bindings`
    binds the drop-in defaults: a Node repository gets `CommandTest(["npm", "test"])`, a Makefile
    with a `build` target gets `CommandBuild(["make", "build"])`. A Test or Validate that detection
    could not place is a commented stub — with its own import — rather than a wrong default that
    runs; Provision, Build and Run get a line only when something was found, for the reason the
    inline comment below gives. Only the adapters actually bound are imported, so a generated
    module never ships an unused import.
    Everything else — the egress opt-out and the middleware — is identical in every scaffold; the
    trampoline is byte-identical across repos, and this file is the one `init` fits to the stack.
    """
    imports: list[str] = ["Test", "Validate"]
    test_bind = _bind_line(facts, "Test", imports)
    validate_bind = _bind_line(facts, "Validate", imports)
    # Provision, build and run get a line only when detection found one. No stub when it did not:
    # the selfcheck needs Test and Validate, so their absence is worth a comment that says how to
    # bind them, and build and run are the verbs a repository adds the day a workflow of its own
    # needs them, when it will know what to bind. Provision unbound is not silent either: the
    # scaffolded work job runs `in-lockstep provision` first, and that prints `not bound` with the
    # files detection looked for.
    extra: list[str] = []
    if getattr(facts, "provision_commands", ()):
        imports += ["Provision", "CommandProvision"]
        steps = [list(step) for step in facts.provision_commands]
        extra.append(f"lockstep.bind(Provision, CommandProvision({steps!r}))")
    if getattr(facts, "build_command", ()):
        imports += ["Build", "CommandBuild"]
        extra.append(f"lockstep.bind(Build, CommandBuild({list(facts.build_command)!r}))")
    if getattr(facts, "run_command", ()):
        imports += ["Run", "CommandRun"]
        extra.append(f"lockstep.bind(Run, CommandRun({list(facts.run_command)!r}))")
    # Set off by a blank line, because the line above them may be a commented stub whose last
    # line is an example bind, and a real bind directly under it reads as part of the example.
    extra_binds = ("\n" + "".join(f"\n{line}" for line in extra)) if extra else ""
    # Nothing detected for a verb → its interface is referenced only in a comment, so drop it from
    # the import to avoid an unused name; the stub carries its own commented import instead. If
    # nothing was placed at all, there is no adapter import line — an empty one is a syntax error.
    used = sorted(set(imports))
    # One line while it fits ruff's default width, else one name per line with a trailing comma,
    # which ruff's isort keeps multi-line under any width. The repository's own `Validate` checks
    # this file on its first selfcheck against a configuration the scaffold cannot read, and an
    # import block that needs reformatting is a red first run (found by the #185 proof, when a
    # Provision bind pushed a Python repository's import past 100 columns).
    one_line = f"from in_lockstep.adapters import {', '.join(used)}"
    wrapped = "from in_lockstep.adapters import (\n" + "".join(f"    {name},\n" for name in used) + ")"
    # The trailing newline belongs to the value, so an undetected stack leaves no line at all
    # rather than a comment sitting between two imports, which is `I001` at ruff's own defaults in
    # the file `init` has just written. The note it used to carry moved to the stubs it points at.
    adapter_import = (one_line if len(one_line) <= _SCAFFOLD_WIDTH else wrapped) + "\n" if used else ""
    return _SCAFFOLD_MODULE.format(
        adapter_import=adapter_import,
        test_bind=test_bind,
        validate_bind=validate_bind,
        extra_binds=extra_binds,
    )


#: ruff's default `line-length`, the narrowest width a scaffolded module is likely to be checked at.
_SCAFFOLD_WIDTH = 88


def _bind_line(facts: Any, verb: str, imports: list[str]) -> str:
    """One deterministic-verb bind for the scaffold, plus the imports it needs (appended in place).
    A commented, self-contained stub when detection placed nothing, so the generated module has no
    default that runs unbidden and no name imported but unused."""
    if verb == "Test":
        if getattr(facts, "pytest", False):
            from .adapters.detected import PYTEST_ARGS

            imports.append("PytestTest")
            return f"lockstep.bind(Test, PytestTest(args={list(PYTEST_ARGS)!r}))"
        if getattr(facts, "test_command", ()):
            imports.append("CommandTest")
            return f"lockstep.bind(Test, CommandTest({list(facts.test_command)!r}))"
        imports.remove("Test")
        return (
            "# No test runner was detected. Bind one, e.g.:\n"
            "#   from in_lockstep.adapters import CommandTest, Test\n"
            '#   lockstep.bind(Test, CommandTest(["npm", "test"]))'
        )
    if getattr(facts, "ruff", False):
        imports.append("RuffValidate")
        return "lockstep.bind(Validate, RuffValidate())"
    if getattr(facts, "lint_command", ()):
        imports.append("CommandValidate")
        return f"lockstep.bind(Validate, CommandValidate({list(facts.lint_command)!r}))"
    imports.remove("Validate")
    return (
        "# No linter was detected. Bind one, e.g.:\n"
        "#   from in_lockstep.adapters import RuffValidate, Validate\n"
        "#   lockstep.bind(Validate, RuffValidate())"
    )


_SCAFFOLD_MODULE = '''"""The lifecycle for this repository.

This file IS the configuration: it is executed, not parsed. Anything you can express in Python
you can express here — but keep it pure, because it is imported to be inspected as well as run.
"""

from in_lockstep import Lockstep
{adapter_import}from in_lockstep.middleware import CostBudget, otel
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress

lockstep = Lockstep.detect()

# Deterministic verbs bind adapters over real tools. `in-lockstep ls` prints what detection found.
{test_bind}
{validate_bind}{extra_binds}

# THIS IS AN OPT-OUT FROM A CONTROL, and it is a visible line on purpose. A review reads a diff
# authored by whoever opened the change, so the model is sent UNTRUSTED_EXTERNAL content, and
# egress enforcement is mandatory for that — but a laptop and a GitHub-hosted runner both have
# open internet, where an asserted `enforced` mode is disproven by a probe and the review is
# refused. Binding UnsandboxedEgress is what lets the review run at all in those places.
#
# What makes it defensible for REVIEW specifically: the review tool set is empty, so an injection
# in the diff has no tool to exfiltrate through — the only outbound channel is the model call
# itself, which is the point of the review. A verb that grants write or execute tools is a
# different calculation; do not carry this binding to one without re-deciding.
lockstep.bind(EgressPolicy, UnsandboxedEgress())

# Cross-cutting behaviour is middleware. Redaction, egress and the kill switch are NOT here:
# they are privileged, and `--no-middleware` cannot reach them.
lockstep.middleware += [otel(), CostBudget(usd=2.00)]
'''

_SCAFFOLD_TRAMPOLINE = """# Invokes the CLI. Contains no lifecycle logic, and is never regenerated.
#
# Two jobs. Reviewing is read-only, so the job that does it needs a provider credential and
# `contents: read` and nothing else -- and the record it writes can therefore only leave as a
# bundle in its artifact. The second job holds `contents: write` and no provider credential, and
# publishes that bundle to the `lockstep-history` branch. That split -- an unprivileged job that
# talks to a model, a privileged one that writes -- is what keeps an API key and a write token
# out of the same process, and it is the same shape a verb of yours that produces a change will
# use the day you add one.
#
# The base ref is passed explicitly because configuration is loaded from it: lockstep.py comes
# from the base branch, never from the ref under review, or a pull request could supply the file
# defining the bindings and policy that constrain reviewing it.
#
# Everything that runs next to the provider key is pinned: the framework by version, the actions
# by commit SHA. An unpinned install hands whatever the registry serves next to the one job
# holding a credential, and a floating release breaks every adopting repository at once, with no
# repo-local diff to blame. Update the pins deliberately, as a reviewed change.
name: lockstep

on:
  pull_request:

permissions:
  contents: read

concurrency:
  group: lockstep-${{ github.ref }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-24.04
    # Without this the CI default is 360 minutes, not 20.
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
        with:
          fetch-depth: 0
          # The token `actions/checkout` would otherwise leave in `.git/config` is one `git config`
          # away from a test the model staged into a linked worktree; this job pushes nothing (#308).
          persist-credentials: false
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - name: Are the controls in place?
        # No credential here: doctor reads config from the trusted base ref, so it never executes
        # the change under review, and giving the key to a step that does not call a model just
        # widens where it can leak.
        run: uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep doctor
        # Not `continue-on-error`: a verdict nothing acts on is a diagnostic nobody reads. What
        # it checks first is the provider-side spend cap you set in the console, attested here as
        # a repository variable; without it this step is red, on purpose.
        env:
          IN_LOCKSTEP_ORG_SPEND_LIMIT: ${{ vars.IN_LOCKSTEP_ORG_SPEND_LIMIT }}
      - name: Review
        # Skipped without a credential rather than failed: a pull request from a fork gets no
        # secrets, and a red check the contributor cannot fix teaches everyone to ignore red.
        # The `secrets` context reads in a step `if`, which keeps the key scoped to this one step
        # instead of every step in the job.
        if: ${{ secrets.IN_LOCKSTEP_SECRET != '' }}
        # `security` is one of four shipped lenses; `intent`, `performance` and `tests` are the
        # others, and `--aspect <name>` runs any of them. One is scaffolded rather than four
        # because each is a real model call against the ceiling below, so four lenses is four
        # times the bill -- your decision rather than ours. Adding one is another invocation
        # here, and `in-lockstep ls` prints the lenses your module actually has -- including
        # any your own `AiReview(lenses=...)` added, which is the same list `/review <aspect>`
        # resolves against.
        run: |
          uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep review \
            --base "origin/${GITHUB_BASE_REF}" \
            --head "${GITHUB_SHA}" \
            --aspect security \
            --budget 0.75 \
            --record \
            --cassette "${RUNNER_TEMP}/review.json" \
            --comment-out review-comments
        env:
          IN_LOCKSTEP_SECRET: ${{ secrets.IN_LOCKSTEP_SECRET }}
          # A variable rather than a secret: a workspace id identifies, it does not authenticate.
          # Leave it unset unless your key is identity-linked; empty sends no header.
          ANTHROPIC_WORKSPACE_ID: ${{ vars.ANTHROPIC_WORKSPACE_ID }}
      # Recording is ON, and here is what that means, in the file rather than in a doc somebody
      # has to find. `--record` above writes the request that was really sent — the whole composed
      # prompt and the whole diff — to RUNNER_TEMP, which the runner destroys when the job ends.
      # No path below reaches it, so the tape itself never leaves the runner.
      #
      # What survives is what this step makes of it: cases under `.lockstep/cases/`, each a real
      # request with expectations derived from the answer that really came back. `in-lockstep init`
      # gitignores that directory, and the artifact below says how long a copy is kept.
      #
      # Delete both steps if you would rather keep nothing. Nothing else depends on them.
      - name: Harvest what the review recorded
        if: ${{ secrets.IN_LOCKSTEP_SECRET != '' }}
        # Harvest refuses rather than inventing, so a recording it cannot build a case from exits
        # non-zero. That is right for harvest and the wrong reason to fail somebody's review.
        continue-on-error: true
        run: |
          uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep eval harvest \
            --from "${RUNNER_TEMP}/review.json" \
            --into .lockstep/cases \
            --family review
      - name: No provider credential
        if: ${{ secrets.IN_LOCKSTEP_SECRET == '' }}
        run: echo "no IN_LOCKSTEP_SECRET (fork pull request?) — review skipped, nothing failed"
      # `review` is read-only and this job has `contents: read`, so it cannot publish its own
      # record. The bundle carries it out, and `publish` below pushes it.
      - run: uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep history --bundle history.bundle
        if: always()
        continue-on-error: true
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4
        if: always()
        with:
          name: lockstep-run
          path: |
            history.bundle
            review-comments/
            .lockstep/
          if-no-files-found: ignore
          # `.lockstep/` is a dotted path and upload-artifact@v4 excludes hidden files by DEFAULT,
          # so without this the directory above is silently dropped and the artifact arrives empty.
          include-hidden-files: true
          # Stated rather than inherited. The vendor default is 90 days, and this artifact can
          # hold prompts and diffs. Two weeks is long enough to notice a run and download it, and
          # short enough that inaction deletes rather than accumulates.
          retention-days: 14

  # Where the run record becomes durable. `review` holds the provider credential and
  # `contents: read`, so its record can only leave as the bundle in the artifact above; this job
  # holds `contents: write` and no provider credential, and pushes it to the `lockstep-history`
  # branch -- the split the write verbs use, so an API key and a write token never share a
  # process. `always()`, because a review that was refused is the run most worth a record of;
  # `continue-on-error`, because a record that could not be pushed must not turn the review red.
  # What this misses -- a run cancelled by the next push -- `in-lockstep history --from-artifacts
  # lockstep-run --push` absorbs from a job holding the write token, once each; `report --scm`
  # counts what is outstanding.
  publish:
    needs: review
    if: ${{ always() && !github.event.pull_request.head.repo.fork }}
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    permissions:
      contents: write
      # For the comment below. Here and not in `review`: the job that calls a model must not also
      # hold a token that writes to the repository, and a comment is a write.
      pull-requests: write
      actions: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093  # v4
        with:
          name: lockstep-run
          path: ${{ runner.temp }}/review
        continue-on-error: true
      # No provider extra: this job must be unable to reach a model.
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep history \
            --from-bundle "${RUNNER_TEMP}/review/history.bundle" \
            --push
        continue-on-error: true
      # The lens's verdict and findings as a sticky comment on the pull request, one per lens,
      # each found again by the marker inside its body so a re-review edits it in place. Without
      # this the review is a green check and a job log.
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep comment \
            --pr "$PR" \
            --body-file "${RUNNER_TEMP}/review/review-comments"
        continue-on-error: true
        env:
          PR: ${{ github.event.pull_request.number }}
          GH_TOKEN: ${{ github.token }}
"""

_SCAFFOLD_GITLAB_TRAMPOLINE = """\
# Invokes the CLI. Contains no lifecycle logic, and is never regenerated.
#
# The same trampolines lockstep.yml, implement.yml, fix.yml and improve.yml are on GitHub, in
# GitLab's own terms; docs/trampoline.md is the host-neutral contract both are written against.
# Two ACTIVE jobs on every merge request: review, which is read-only and needs a provider
# credential and the read the runner already has, and publish, which keeps review's record and
# holds the push token and no provider. The gate/work/propose split for write-capable verbs is
# below and active, held inert by its `rules:` until a pipeline runs with LOCKSTEP_ISSUE set on
# the default branch; the learning loop's measure/propose pair the same way under
# LOCKSTEP_IMPROVE.
#
# One GitLab-specific warning, and it is the important one: a merge-request pipeline runs THIS
# FILE from the source branch — the change under review can edit it. The framework's own
# configuration is safe regardless (lockstep.py is loaded from the trusted target ref, never the
# ref under review), but the YAML is host-owned, so protect it at the host: name .gitlab-ci.yml
# in CODEOWNERS with required approvals, or point the project's CI config path at a protected
# location. The framework install is pinned by version for the same reason the GitHub scaffold
# pins by version and SHA: an unpinned install feeds whatever the registry serves next to the job
# holding the provider key. Update the pin deliberately, as a reviewed change.
stages: [gate, review, publish, work, propose]

review:
  stage: review
  image: python:3.11-slim
  # Without this the instance default applies, which is measured in hours, not minutes.
  timeout: 20m
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  variables:
    # Full history: the review diffs base...head, which a shallow clone cannot resolve.
    GIT_DEPTH: "0"
  script:
    - pip install --quiet 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION'
    # The target branch is not fetched by default on a merge-request pipeline, and the trusted
    # config ref and the diff base both live on it.
    - git fetch --quiet origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
    # No credential needed: doctor reads config from the trusted target ref, so it never executes
    # the change under review. Not `|| true`: a verdict nothing acts on is a diagnostic nobody
    # reads, and the first thing it checks is IN_LOCKSTEP_ORG_SPEND_LIMIT, a CI variable
    # attesting the spend cap you set in the provider console.
    - in-lockstep doctor
    # Skipped without a credential rather than failed: a merge request from a fork gets no
    # protected variables, and a red check the contributor cannot fix teaches everyone to
    # ignore red.
    # Recording is ON. `--record` writes the request that was really sent — the whole composed
    # prompt and the whole diff — to /tmp, outside the checkout, in a container this job does
    # not share. `paths:` below names `.lockstep/` and the bundle and nothing else, so the tape
    # never leaves. What survives is the cases harvested from it, under `.lockstep/cases/`,
    # which `init` gitignores and the artifact keeps for the stated number of days. Delete the
    # two lines if you would rather keep nothing; nothing else depends on them.
    #
    # `|| true` on the harvest: harvest refuses rather than inventing, so a recording it cannot
    # build a case from exits non-zero, and that is the wrong reason to fail somebody's review.
    - |
      # `security` is one of four shipped lenses; `intent`, `performance` and `tests` are the
      # others, and `--aspect <name>` runs any of them -- repeat it to run several, each its own
      # run and record, the worst status exiting. One is scaffolded rather than four because
      # each is a real model call against the ceiling below. `in-lockstep ls` prints the lenses
      # your module actually has, including any of your own.
      if [ -n "$IN_LOCKSTEP_SECRET" ]; then
        in-lockstep review \\
          --base "origin/${CI_MERGE_REQUEST_TARGET_BRANCH_NAME}" \\
          --head "${CI_COMMIT_SHA}" \\
          --aspect security \\
          --budget 0.75 \\
          --record \\
          --cassette /tmp/review.json \\
          --comment-out review-comments
        in-lockstep eval harvest \\
          --from /tmp/review.json \\
          --into .lockstep/cases \\
          --family review || true
      else
        echo "no IN_LOCKSTEP_SECRET (fork merge request?) — review skipped, nothing failed"
      fi
    # The run's record, out of a job that cannot push: the ledger commits this job wrote, as a
    # bundle the publish job absorbs. A review that was refused is the run most worth a record,
    # so this runs whatever the review said; `|| true` because a job that made no record has no
    # bundle to make, and that is not a failure of the review.
    - in-lockstep history --bundle history.bundle || true
  artifacts:
    when: always
    paths: [.lockstep/, history.bundle, review-comments/]
    # Said out loud rather than inherited: the instance default is measured in weeks or forever
    # depending on how this GitLab is configured, and this artifact can hold prompts and diffs.
    expire_in: 14 days

# The pipe #294 found open on GitHub and this file re-shipped (#314): a review job holding a read
# token bundled its record, and nothing downstream held the write token to push it, so every
# GitLab review's record died with the job. The same split propose uses -- push token, no
# provider credential, no provider SDK -- absorbs it, on every merge request, whatever review
# said. What the file cannot do: GitLab hands protected variables only to protected refs, so a
# token scoped to lockstep-publish as a PROTECTED variable reaches no merge-request pipeline
# from a branch. Scope it unprotected to that environment and protect this file instead (the
# warning at the top), or accept that only protected-ref pipelines publish; `report --scm`
# prints a dash for what GitLab cannot list either way.
publish:
  stage: publish
  image: python:3.11-slim
  timeout: 5m
  needs: [review]
  environment: lockstep-publish
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      when: always
  script:
    - pip install --quiet 'in-lockstep==IN_LOCKSTEP_VERSION'
    - git remote set-url origin "https://oauth2:${GITLAB_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git"
    - in-lockstep history --from-bundle history.bundle --push || true
    # The lens's verdict and findings as a note on the merge request, one per lens, edited in
    # place on a re-review. `|| true` for the reason the push has it.
    - in-lockstep comment --pr "$CI_MERGE_REQUEST_IID" --body-file review-comments || true

# -- write-capable verbs: the gate/work/propose credential split --------------------------------
#
# The same three-part split implement.yml and fix.yml carry on GitHub — gate authorizes the asker
# holding no write-capable credential; work talks to the model holding the provider key and a
# READ token; propose opens the merge request holding the write token and no provider key —
# expressed in GitLab's terms. ONE pair of jobs for both verbs rather than two, and the verb is
# a pipeline variable: on GitHub the two files exist because the two triggers differ
# (`/implement` and `/fix` are different comments), and on GitLab the trigger is the same
# run-pipeline-with-variables either way, so a second copy of the jobs would be one more place
# credential scoping has to be right and nothing else. LOCKSTEP_VERB is `implement` unless the
# pipeline says `fix`; `in-lockstep run` refuses any other value by name, because the workflow
# ids are what the module registered. GitLab has no issue-label trigger, so the ai-generated loop
# (a failed fix opening the next ticket) is a pipeline run with LOCKSTEP_VERB=fix the same way.
#
# Who may fire it: running a pipeline with variables already requires Developer here, a check
# GitLab makes before the pipeline exists, but the gate job still runs first so the decision (and
# the record of who asked) lives in the framework, where it has tests, not in a `rules:` clause.
# Every job's rules also pin the run to the DEFAULT branch: the asker picks the ref when they run
# a pipeline, and the gate's CODEOWNERS must come from a ref the asker cannot supply.
#
# ACTIVE, and inert until asked for: every `rules:` below requires LOCKSTEP_ISSUE on the default
# branch, so a repository that never runs a pipeline with that variable never runs these jobs.
# Shipping them commented out meant an adopter's first write-verb run was a YAML editing exercise
# against a file the framework does not regenerate, which is the opposite of what O3 asks.
#
# Before the first run: create environments `lockstep-work`, `lockstep-propose` and
# `lockstep-publish`; then SCOPE the credentials — GitLab's default variable scope is every
# environment, and an unscoped variable quietly puts both credentials in both jobs, which unmakes
# the split without any visible failure. Scope IN_LOCKSTEP_SECRET and a read-only project access
# token (read_api, as GITLAB_TOKEN — what `permissions: issues: read` is on GitHub, so from-ticket
# can fetch the issue on a private project) to lockstep-work; scope a write-capable project
# access token (api, as GITLAB_TOKEN) to lockstep-propose and lockstep-publish; mark
# lockstep-propose protected with required approvers — the same approval-in-the-system-of-record
# the GitHub scaffold's `environment: implement` provides. Then run a pipeline on the default
# branch with LOCKSTEP_ISSUE set (Run pipeline, or the trigger API).
#
gate:
  stage: gate
  image: python:3.11-slim
  timeout: 5m
  rules:
    - if: $LOCKSTEP_ISSUE && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  script:
    - pip install --quiet 'in-lockstep==IN_LOCKSTEP_VERSION'
    # No association here — GitLab computes no author_association — so the gate answers from
    # CODEOWNERS alone, read from this checkout, which the rules above pin to the default
    # branch. GitLab recognises three CODEOWNERS locations; take the first that exists.
    - |
      in-lockstep gate --actor "$GITLAB_USER_LOGIN" \\
        --codeowners "$(ls CODEOWNERS .gitlab/CODEOWNERS docs/CODEOWNERS 2>/dev/null | head -1)"

work:
  stage: work
  # Keyless, where this GitLab is federated with the provider. `id_tokens:` mints a short-lived
  # JWT per job and puts it in the environment; `ANTHROPIC_IDENTITY_TOKEN` is the name the SDK's
  # own jwt-bearer chain reads, and the framework hands that case straight to it -- so there is
  # no GitLab-specific resolver here and none is needed, which is why this is three lines rather
  # than a port.
  #
  # Honest about the half this repository cannot test: whether a federation rule can be created
  # for a GitLab issuer is a question for the provider, and this repository is GitHub-hosted, so
  # it has never performed this exchange. If it is not available to you, delete these three lines
  # and scope IN_LOCKSTEP_SECRET to lockstep-work instead. The run is identical either way, and a
  # long-lived key in a scoped, protected variable is a real position rather than a fallback.
  id_tokens:
    ANTHROPIC_IDENTITY_TOKEN:
      aud: https://api.anthropic.com
  # uv's image rather than python:3.11-slim: the same slim Python plus the `uv` a uv.lock
  # repository's Provision binding runs. The framework needs Python and the suite needs the
  # repository's toolchain, and no registry image carries both for a Node, Go, Rust or JVM
  # repository -- `init` names this line in its list when it detected one of those, because an
  # image detection could not know is not invented. Pin by digest as a reviewed change.
  image: ghcr.io/astral-sh/uv:python3.11-bookworm-slim
  timeout: 30m
  environment: lockstep-work
  rules:
    - if: $LOCKSTEP_ISSUE && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  variables:
    GIT_DEPTH: "0"
    # A pipeline variable of the same name wins over this default: `fix` runs the fixing verb.
    LOCKSTEP_VERB: implement
  script:
    - pip install --quiet 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION'
    # The repository's own environment, before anything runs in it; docs/trampoline.md says why
    # this runs here and never in review. Not `|| true`: an environment that could not be built
    # is this job's failure, named.
    - in-lockstep provision
    - in-lockstep doctor
    - |
      in-lockstep run "${LOCKSTEP_VERB}/from-ticket" --arg ticket="#${LOCKSTEP_ISSUE}" \\
        --approved-by "${GITLAB_USER_LOGIN}"
    - in-lockstep history --bundle history.bundle || true
  artifacts:
    when: always
    paths: [changeset/, history.bundle]

propose:
  stage: propose
  image: python:3.11-slim
  timeout: 10m
  environment: lockstep-propose
  rules:
    - if: $LOCKSTEP_ISSUE && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  variables:
    LOCKSTEP_VERB: implement
  script:
    - pip install --quiet 'in-lockstep==IN_LOCKSTEP_VERSION'
    # Moved out of the workspace, deliberately: artifacts extract into it, and left there the
    # changeset directory would be swept into the commit open_change makes.
    - mv changeset /tmp/changeset
    - mv history.bundle /tmp/history.bundle || true
    # The runner's default token cannot push; the propose token can, and it is the only
    # credential this job holds.
    - git remote set-url origin "https://oauth2:${GITLAB_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git"
    - |
      in-lockstep run "${LOCKSTEP_VERB}/propose" --arg ticket="#${LOCKSTEP_ISSUE}" \\
        --arg artifact=/tmp/changeset
    - in-lockstep history --from-bundle /tmp/history.bundle --push || true

# -- the learning loop: improve/measure and improve/propose, on the same split ------------------
#
# What improve.yml is on GitHub. Measure reads the ledger and the eval corpus, drafts a prompt
# change and measures it, holding the provider credential and a read token; propose opens the
# merge request holding the write token and no provider. Fired by a pipeline SCHEDULE on the
# default branch that sets LOCKSTEP_IMPROVE=1 -- GitLab's `schedule` source carries variables,
# so no second file and no `rules: - if: $CI_PIPELINE_SOURCE == "schedule"` deciding what a
# schedule means. `--blocked-ok`: a measurement refused before spending (nothing recurs yet, the
# ceiling is reached) is the control working, and the record says which; the flag keeps that
# translation in the framework, where it has a test, rather than in a `test "$?" -eq 3` here.
measure:
  stage: work
  id_tokens:
    ANTHROPIC_IDENTITY_TOKEN:
      aud: https://api.anthropic.com
  image: ghcr.io/astral-sh/uv:python3.11-bookworm-slim
  timeout: 40m
  environment: lockstep-work
  rules:
    - if: $LOCKSTEP_IMPROVE && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  variables:
    # Every branch, so `origin/lockstep-history` is here for the census to read.
    GIT_DEPTH: "0"
  script:
    - pip install --quiet 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION'
    - in-lockstep provision
    - in-lockstep doctor
    - in-lockstep run improve/measure --record --blocked-ok
    - in-lockstep history --bundle history.bundle || true
  artifacts:
    when: always
    paths: [changeset/, attempt/, history.bundle, .lockstep/cases/]

improve-propose:
  stage: propose
  image: python:3.11-slim
  timeout: 10m
  needs: [measure]
  environment: lockstep-propose
  rules:
    - if: $LOCKSTEP_IMPROVE && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
      when: always
  script:
    - pip install --quiet 'in-lockstep==IN_LOCKSTEP_VERSION'
    - mv changeset /tmp/changeset || true
    - mv history.bundle /tmp/history.bundle || true
    - git remote set-url origin "https://oauth2:${GITLAB_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git"
    # No guard on whether measure staged anything: `improve_propose` reads the artifact and
    # refuses by name when there is no proposal in it, and `--blocked-ok` reads that refusal as
    # the scheduled nothing-to-do it is.
    - in-lockstep run improve/propose --arg artifact=/tmp/changeset --blocked-ok
    - in-lockstep history --from-bundle /tmp/history.bundle --push || true
"""

_SCAFFOLD_IMPLEMENT_TRAMPOLINE = """\
# `/implement` on an issue. Hand-written and permanent; nothing generates or checks it.
#
# THIS FILE CONTAINS NO LIFECYCLE LOGIC. What `/implement` actually does — read the issue, run
# the strategy, stage a change, open a pull request, reply on the thread — is
# `implement/from-ticket` and `implement/propose` in .lockstep/lockstep.py, where it is Python
# that can be read, tested and run on a laptop. What is here is what only the CI system can
# express: the trigger, the job split, and which credential each job holds. Keeping the provider
# key out of the job that can write is the reason there are three jobs rather than one.
#
# An `issue_comment` event runs this on the DEFAULT branch, never a contributor's — so the
# comment selects a command, it cannot supply one. And anyone who can see the repository can
# comment, so the comment is not the authorization: `gate` is, and it runs first, alone, holding
# no credential. The gate authorizes the ASKER, not the issue — the issue body stays untrusted
# input to a model, which is why writes are staged and arrive as a pull request a person reads.
#
# GitHub fires `issue_comment` for pull-request comments too, and this deliberately does not filter
# them out. A reviewer decides another attempt is needed while reading the pull request, and making
# them go somewhere else to say so is how a tool teaches people it is awkward. Which ticket the run
# is about is then a question, and it is answered in Python (`ticket_for`) rather than in YAML: the
# number is passed through unchanged and the workflow resolves it, because GitHub draws issue and
# pull-request numbers from one sequence so a number is one or the other and never both.
#
# One consequence, named rather than hidden: `concurrency` groups on that raw number, so a round
# asked for on the issue and a round asked for on the pull request are in different groups and can
# overlap. They cannot collide — every run gets its own branch, which is the design's whole
# concurrency story — so this is wasted spend at worst, and the alternative is resolving the ticket
# in YAML, which is the thing this file exists not to do.
#
# Pinned by version and by SHA for the same reason as lockstep.yml: an unpinned install runs
# whatever the registry serves next, beside the provider key.
name: implement

on:
  issue_comment:
    types: [created]

permissions: {}

concurrency:
  # Per issue, and NOT cancel-in-progress: cancelling a run that has already called a model
  # throws away something that was paid for.
  group: implement-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  gate:
    # `startsWith` rather than `contains`: `contains` would fire on every comment that merely
    # MENTIONS `/implement`, which includes every comment explaining why not to run it.
    if: startsWith(github.event.comment.body, '/implement')
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    permissions:
      contents: read
    outputs:
      actor: ${{ steps.check.outputs.actor }}
    steps:
      # CODEOWNERS comes from this checkout — the default branch — never from anywhere a
      # contributor can write.
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - id: check
        run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep gate \\
            --actor "$ACTOR" --association "$ASSOCIATION"
          echo "actor=$ACTOR" >> "$GITHUB_OUTPUT"
        env:
          ACTOR: ${{ github.event.comment.user.login }}
          ASSOCIATION: ${{ github.event.comment.author_association }}

  implement:
    needs: gate
    runs-on: ubuntu-24.04
    # Longer than the session deadline the module declares (`deadline_seconds=1800`, 30 minutes),
    # and deliberately so. A job timeout does not run the remaining steps, so if the host wins that
    # race the history bundle and the artifact upload below never happen — the run that most needs
    # a record is exactly the one that loses it. The framework has to be the thing that stops
    # first. The margin covers what this step still does after the session ends: running the suite
    # against the staged change, and serializing the change set.
    timeout-minutes: 40
    permissions:
      contents: read
      # Read-only, and needed: the workflow resolves TicketSource to fetch the issue.
      issues: read
      # Also read-only, and also needed: the workflow reads what people said on the pull request
      # it opened last time, so a reviewer's objection is context the next attempt can act on.
      # This job still holds no write token — proposing is the next job, which holds no key.
      pull-requests: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
        with:
          fetch-depth: 0
          # The token `actions/checkout` would otherwise leave in `.git/config` is one `git config`
          # away from a test the model staged into a linked worktree; this job pushes nothing (#308).
          persist-credentials: false
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          # The framework's interpreter, not the repository's. A uv-driven `provision` below
          # builds that one inside a sandbox that passes PATH and HOME through and nothing that
          # pins an interpreter, so a repository that requires another Python gets it. The
          # requirements.txt shape is the exception: its `python -m venv` runs on the `python`
          # PATH resolves to, which under uvx is this one.
          python-version: '3.11'
      # The repository's own environment, from its own lockfile: `uv sync --locked`, `npm ci`,
      # whatever detection bound to Provision (`in-lockstep ls` shows which, and where the tool
      # came from). The framework runs from uvx's isolated interpreter; the suite the strategy
      # runs to prove a change cannot, and this is the step that gives it one. No provider extra,
      # because this calls no model. No `continue-on-error`, because an environment that could
      # not be built is this job's failure and belongs here, named, not in a red suite twenty
      # minutes later. A repository with nothing to provision prints `not bound` and goes on.
      # Only here: this checkout is the default branch, the same trust as lockstep.py. Never in
      # lockstep.yml, whose checkout is the change under review and whose install hooks must not
      # run beside a token; never in propose, whose commit would sweep in what an install wrote.
      - run: uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep provision
      - run: uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep doctor
        # Not `continue-on-error`: a verdict nothing acts on is a diagnostic nobody reads. What
        # it checks first is the provider-side spend cap you set in the console, attested here as
        # a repository variable; without it this step is red, on purpose.
        env:
          IN_LOCKSTEP_ORG_SPEND_LIMIT: ${{ vars.IN_LOCKSTEP_ORG_SPEND_LIMIT }}
      # The same command a developer runs, with `--approved-by` where they would type
      # `--approve`. The process does not change when it moves from a terminal to a trigger —
      # only who the human is and how they were verified. No `--budget`: the ceiling is
      # `lockstep.budget` in the module, and a flag here would replace it rather than merge with
      # it, so the number in force and the number in the diff would differ and nothing would say.
      - run: |
          uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' \\
            in-lockstep run implement/from-ticket \\
            --arg ticket="#${ISSUE}" \\
            --approved-by "${ACTOR}" \\
            --record
        env:
          ISSUE: ${{ github.event.issue.number }}
          # A name GitHub computed and the gate verified, not one the comment claimed.
          ACTOR: ${{ needs.gate.outputs.actor }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          IN_LOCKSTEP_SECRET: ${{ secrets.IN_LOCKSTEP_SECRET }}
          # A variable rather than a secret: a workspace id identifies, it does not authenticate.
          ANTHROPIC_WORKSPACE_ID: ${{ vars.ANTHROPIC_WORKSPACE_ID }}
      # The record this run made lives in THIS runner's .git and dies with it. It travels as a
      # bundle: the unprivileged job produces, the privileged one publishes.
      - run: uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep history --bundle history.bundle
        if: always()
        continue-on-error: true
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4
        if: always()
        with:
          name: lockstep-run
          path: |
            changeset/
            history.bundle
            # The cases harvested from the recording, in-process at the end of the run.
            # The TAPE is not here and never is: it holds every file the session opened
            # and every command's output, and dies in RUNNER_TEMP with the runner.
            .lockstep/cases/
          if-no-files-found: ignore
          # `.lockstep/` is a dotted path and upload-artifact@v4 drops hidden files by DEFAULT,
          # so without this the line above is silently ignored and the artifact arrives short.
          include-hidden-files: true
          # Said rather than inherited: the vendor default is 90 days, and this artifact holds
          # cases built from a session that read your files. Two weeks is enough to notice a run.
          retention-days: 14

  propose:
    needs: [gate, implement]
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    # Adding required reviewers to this environment in repository settings makes it an approval
    # in the system of record. With no protection rules it passes straight through, and the pull
    # request is the review.
    environment: implement
    permissions:
      contents: write
      pull-requests: write
      issues: write
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      # NOT the provider extra. This job holds a write token and must be unable to reach a model
      # at all; not installing the SDK makes that a fact about the environment.
      # Downloaded outside the workspace, deliberately: inside it, changeset/ would be swept
      # into the commit open_change makes.
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093  # v4
        with:
          name: lockstep-run
          path: ${{ runner.temp }}/implement
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep run implement/propose \\
            --arg ticket="#${ISSUE}" \\
            --arg artifact="${RUNNER_TEMP}/implement/changeset"
        env:
          ISSUE: ${{ github.event.issue.number }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      # Where the run record becomes durable: this job holds `contents: write` and no provider
      # credential, which is exactly the right half to publish evidence from.
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep history \\
            --from-bundle "${RUNNER_TEMP}/implement/history.bundle" \\
            --push
        if: always()
        continue-on-error: true
"""


_SCAFFOLD_FIX_TRAMPOLINE = """\
# The /fix chat-ops flow: gate the asker, reproduce-and-fix under the provider key with no write
# token, then open the pull request from the job that holds the token and no key. Same three-job
# credential split as implement.yml, because fix writes too. A later slice adds an `issues:
# labeled` trigger so an `ai-generated` bug routes here on its own.
#
# Pinned by version and by SHA: an unpinned install runs whatever the registry serves next, beside
# the provider key.
name: fix

on:
  issue_comment:
    types: [created]

permissions: {}

concurrency:
  group: fix-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  gate:
    if: startsWith(github.event.comment.body, '/fix')
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    permissions:
      contents: read
    outputs:
      actor: ${{ steps.check.outputs.actor }}
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - id: check
        run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep gate \\
            --actor "$ACTOR" --association "$ASSOCIATION"
          echo "actor=$ACTOR" >> "$GITHUB_OUTPUT"
        env:
          ACTOR: ${{ github.event.comment.user.login }}
          ASSOCIATION: ${{ github.event.comment.author_association }}

  fix:
    needs: gate
    runs-on: ubuntu-24.04
    # Longer than the session deadline the module declares (`deadline_seconds=1800`, 30 minutes).
    # A job timeout skips the remaining steps, so if the host wins that race the history bundle and
    # the artifact upload below never run, and the run that most needs a record is the one that
    # loses it. The framework has to be the thing that stops first.
    timeout-minutes: 35
    permissions:
      contents: read
      issues: read
      # Also read-only, and also needed: the workflow reads what people said on the pull request
      # it opened last time, so a reviewer's objection is context the next attempt can act on.
      # This job still holds no write token — proposing is the next job, which holds no key.
      pull-requests: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
        with:
          fetch-depth: 0
          # The token `actions/checkout` would otherwise leave in `.git/config` is one `git config`
          # away from a test the model staged into a linked worktree; this job pushes nothing (#308).
          persist-credentials: false
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          # The framework's interpreter, not the repository's. A uv-driven `provision` below
          # builds that one inside a sandbox that passes PATH and HOME through and nothing that
          # pins an interpreter, so a repository that requires another Python gets it. The
          # requirements.txt shape is the exception: its `python -m venv` runs on the `python`
          # PATH resolves to, which under uvx is this one.
          python-version: '3.11'
      # The repository's own environment, from its own lockfile: `uv sync --locked`, `npm ci`,
      # whatever detection bound to Provision (`in-lockstep ls` shows which, and where the tool
      # came from). The framework runs from uvx's isolated interpreter; the suite the strategy
      # runs to prove a change cannot, and this is the step that gives it one. No provider extra,
      # because this calls no model. No `continue-on-error`, because an environment that could
      # not be built is this job's failure and belongs here, named, not in a red suite twenty
      # minutes later. A repository with nothing to provision prints `not bound` and goes on.
      # Only here: this checkout is the default branch, the same trust as lockstep.py. Never in
      # lockstep.yml, whose checkout is the change under review and whose install hooks must not
      # run beside a token; never in propose, whose commit would sweep in what an install wrote.
      - run: uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep provision
      - run: uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep doctor
        # Not `continue-on-error`: a verdict nothing acts on is a diagnostic nobody reads. What
        # it checks first is the provider-side spend cap you set in the console, attested here as
        # a repository variable; without it this step is red, on purpose.
        env:
          IN_LOCKSTEP_ORG_SPEND_LIMIT: ${{ vars.IN_LOCKSTEP_ORG_SPEND_LIMIT }}
      - run: |
          uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep run fix/from-ticket \\
            --arg ticket="#${ISSUE}" \\
            --approved-by "${ACTOR}" \\
            --record
        env:
          ISSUE: ${{ github.event.issue.number }}
          ACTOR: ${{ needs.gate.outputs.actor }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          IN_LOCKSTEP_SECRET: ${{ secrets.IN_LOCKSTEP_SECRET }}
          ANTHROPIC_WORKSPACE_ID: ${{ vars.ANTHROPIC_WORKSPACE_ID }}
      - run: uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep history --bundle history.bundle
        if: always()
        continue-on-error: true
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4
        if: always()
        with:
          name: lockstep-run
          path: |
            fix-changeset/
            history.bundle
            # The cases harvested from the recording, in-process at the end of the run.
            # The TAPE is not here and never is: it holds every file the session opened
            # and every command's output, and dies in RUNNER_TEMP with the runner.
            .lockstep/cases/
          if-no-files-found: ignore
          # `.lockstep/` is a dotted path and upload-artifact@v4 drops hidden files by DEFAULT,
          # so without this the line above is silently ignored and the artifact arrives short.
          include-hidden-files: true
          # Said rather than inherited: the vendor default is 90 days, and this artifact holds
          # cases built from a session that read your files. Two weeks is enough to notice a run.
          retention-days: 14

  propose:
    needs: [gate, fix]
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    environment: fix
    permissions:
      contents: write
      pull-requests: write
      issues: write
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093  # v4
        with:
          name: lockstep-run
          path: ${{ runner.temp }}/fix
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep run fix/propose \\
            --arg ticket="#${ISSUE}" \\
            --arg artifact="${RUNNER_TEMP}/fix/fix-changeset"
        env:
          ISSUE: ${{ github.event.issue.number }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep history \\
            --from-bundle "${RUNNER_TEMP}/fix/history.bundle" \\
            --push
        if: always()
        continue-on-error: true
"""


_SCAFFOLD_AI_GENERATED_TRAMPOLINE = """\
# The self-feeding half of the loop: an issue labeled `ai-generated` — by a maintainer, or by the
# framework itself when an earlier fix failed its tests — routes straight to the fix workflow. There
# is no gate job here, and that is deliberate: adding a label needs write access, so the label IS
# the authorization the `/fix` comment path needs a gate to establish (anyone can comment; not
# anyone can label). The loop is bounded by `lockstep.max_attempts` — a failed fix opens the next
# `ai-generated` issue only until the cap — so this trigger cannot run away.
#
# Same credential split as fix.yml, and pinned by version and SHA for the same reason.
name: ai-generated

on:
  issues:
    types: [opened, labeled]

permissions: {}

concurrency:
  group: ai-generated-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  fix:
    # `labeled` carries the one label just added; `opened` carries the whole set, because an issue
    # created with the label fires `opened` and not always a separate `labeled`. Check both.
    if: >-
      github.event.label.name == 'ai-generated' ||
      contains(github.event.issue.labels.*.name, 'ai-generated')
    runs-on: ubuntu-24.04
    # Longer than the session deadline the module declares (`deadline_seconds=1800`, 30 minutes).
    # A job timeout skips the remaining steps, so if the host wins that race the history bundle and
    # the artifact upload below never run, and the run that most needs a record is the one that
    # loses it. The framework has to be the thing that stops first.
    timeout-minutes: 35
    permissions:
      contents: read
      issues: read
      # Also read-only, and also needed: the workflow reads what people said on the pull request
      # it opened last time, so a reviewer's objection is context the next attempt can act on.
      # This job still holds no write token — proposing is the next job, which holds no key.
      pull-requests: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
        with:
          fetch-depth: 0
          # The token `actions/checkout` would otherwise leave in `.git/config` is one `git config`
          # away from a test the model staged into a linked worktree; this job pushes nothing (#308).
          persist-credentials: false
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          # The framework's interpreter, not the repository's. A uv-driven `provision` below
          # builds that one inside a sandbox that passes PATH and HOME through and nothing that
          # pins an interpreter, so a repository that requires another Python gets it. The
          # requirements.txt shape is the exception: its `python -m venv` runs on the `python`
          # PATH resolves to, which under uvx is this one.
          python-version: '3.11'
      # The repository's own environment, from its own lockfile: `uv sync --locked`, `npm ci`,
      # whatever detection bound to Provision (`in-lockstep ls` shows which, and where the tool
      # came from). The framework runs from uvx's isolated interpreter; the suite the strategy
      # runs to prove a change cannot, and this is the step that gives it one. No provider extra,
      # because this calls no model. No `continue-on-error`, because an environment that could
      # not be built is this job's failure and belongs here, named, not in a red suite twenty
      # minutes later. A repository with nothing to provision prints `not bound` and goes on.
      # Only here: this checkout is the default branch, the same trust as lockstep.py. Never in
      # lockstep.yml, whose checkout is the change under review and whose install hooks must not
      # run beside a token; never in propose, whose commit would sweep in what an install wrote.
      - run: uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep provision
      - run: uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep doctor
        # Not `continue-on-error`: a verdict nothing acts on is a diagnostic nobody reads. What
        # it checks first is the provider-side spend cap you set in the console, attested here as
        # a repository variable; without it this step is red, on purpose.
        env:
          IN_LOCKSTEP_ORG_SPEND_LIMIT: ${{ vars.IN_LOCKSTEP_ORG_SPEND_LIMIT }}
      # `--approved-by` records who labelled it — a maintainer, or `github-actions[bot]` when the
      # framework opened the follow-up. The grant is the write-gated label, not a comment.
      - run: |
          uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep run fix/from-ticket \\
            --arg ticket="#${ISSUE}" \\
            --approved-by "labeled:${LABELER}" \\
            --record
        env:
          ISSUE: ${{ github.event.issue.number }}
          LABELER: ${{ github.event.sender.login }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          IN_LOCKSTEP_SECRET: ${{ secrets.IN_LOCKSTEP_SECRET }}
          ANTHROPIC_WORKSPACE_ID: ${{ vars.ANTHROPIC_WORKSPACE_ID }}
      - run: uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep history --bundle history.bundle
        if: always()
        continue-on-error: true
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4
        if: always()
        with:
          name: lockstep-run
          path: |
            fix-changeset/
            history.bundle
            # The cases harvested from the recording, in-process at the end of the run.
            # The TAPE is not here and never is: it holds every file the session opened
            # and every command's output, and dies in RUNNER_TEMP with the runner.
            .lockstep/cases/
          if-no-files-found: ignore
          # `.lockstep/` is a dotted path and upload-artifact@v4 drops hidden files by DEFAULT,
          # so without this the line above is silently ignored and the artifact arrives short.
          include-hidden-files: true
          # Said rather than inherited: the vendor default is 90 days, and this artifact holds
          # cases built from a session that read your files. Two weeks is enough to notice a run.
          retention-days: 14

  propose:
    needs: fix
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    environment: fix
    permissions:
      contents: write
      pull-requests: write
      issues: write
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093  # v4
        with:
          name: lockstep-run
          path: ${{ runner.temp }}/fix
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep run fix/propose \\
            --arg ticket="#${ISSUE}" \\
            --arg artifact="${RUNNER_TEMP}/fix/fix-changeset"
        env:
          ISSUE: ${{ github.event.issue.number }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep history \\
            --from-bundle "${RUNNER_TEMP}/fix/history.bundle" \\
            --push
        if: always()
        continue-on-error: true
"""


_SCAFFOLD_REVIEW_TRAMPOLINE = """\
# `/review <lens>` on a pull request. Hand-written and permanent; nothing generates or checks it.
#
# THIS FILE CONTAINS NO LIFECYCLE LOGIC. Which lens the comment asked for is resolved in Python
# against the lenses your module actually binds -- `in-lockstep ls` prints them, and a name that is
# not among them is refused before any credential is read. Which change the comment is about is
# resolved in Python too: an `issue_comment` event carries neither ref, so the framework asks the
# host what the pull request points at. GitHub's expression language has twelve functions and none
# of them splits a string, so this file could not parse the argument even if it wanted to.
#
# WHY A SEPARATE FILE FROM lockstep.yml. That one runs on `pull_request` and reviews every change
# once, unattended, through the lens it names. This is somebody asking, on the thread, for a
# specific lens -- usually one that did not run automatically, or the same one again after a push.
# Different trigger, different authorization, different job split.
#
# WHO MAY FIRE IT. Anyone who can see a repository can comment on it, so the comment is not the
# authorization. `gate` is, and it runs first and alone: an unauthorized comment costs one job that
# calls nothing and reaches no host. It passes an org member or owner, or anyone named in
# CODEOWNERS, read from this checkout -- the default branch -- never from anywhere a contributor
# can write.
#
# THE THREE JOBS ARE A CREDENTIAL SPLIT, NOT A PIPELINE. `review` holds the provider credential
# and read access. `post` holds `pull-requests: write` and no provider SDK at all -- it cannot call
# a model because the extra is not installed, which is a fact about the environment rather than a
# property of the code path taken. No amount of Python makes one process hold two token scopes,
# which is why the findings travel between them as a file.
#
# WHAT BOUNDS THE SPEND. One lens, one run, and the ceiling your module declares. NO `--budget`
# here, and its absence is deliberate: `review --budget` REPLACES the module's ceiling rather than
# merging with it, so a literal in this file would outrank the module silently and permanently.
# lockstep.yml does state one, and that is not an inconsistency: it runs on `pull_request`, which
# is the workflow a repository gets before it has a lockstep.py at all. This trigger cannot be in
# that position -- it resolves the lens against the adapter the module binds, so the module exists
# by the time this runs, and the ceiling it declares is the one that applies.
#
# NO `doctor` STEP, and that is a decision rather than an omission. lockstep.yml runs it on every
# pull request, so the controls were checked for this change when it was opened; re-running a lens
# on request is not a second occasion to ask.
#
# WHAT THIS DOES NOT DO. A fork's pull request has no head commit in this checkout, so `/review` on
# one refuses rather than reviewing the wrong thing. That refusal is the honest outcome.
#
# Pinned by version and by SHA for the same reason as lockstep.yml: an unpinned install runs
# whatever the registry serves next, beside the provider key.
name: review

on:
  issue_comment:
    types: [created]

permissions: {}

concurrency:
  # Per issue, and NOT cancel-in-progress: a cancelled model call may still be billed, and throws
  # away what was paid for.
  group: review-${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  gate:
    # `startsWith` rather than `contains`, which would fire on every comment that merely mentions
    # `/review` -- including one explaining why not to run it. A pull-request comment fires
    # `issue_comment` too and is deliberately not filtered out: a reviewer asking for a lens is
    # standing on the pull request when they ask.
    if: startsWith(github.event.comment.body, '/review')
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    permissions:
      contents: read
    outputs:
      actor: ${{ steps.check.outputs.actor }}
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - id: check
        run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep gate \\
            --actor "$ACTOR" --association "$ASSOCIATION"
          echo "actor=$ACTOR" >> "$GITHUB_OUTPUT"
        env:
          ACTOR: ${{ github.event.comment.user.login }}
          ASSOCIATION: ${{ github.event.comment.author_association }}

  review:
    needs: gate
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    permissions:
      contents: read
      # Read-only, and needed: the framework asks the host what the pull request points at,
      # because an `issue_comment` event carries neither ref -- GITHUB_BASE_REF is empty and
      # GITHUB_SHA is the default branch's tip. A run built from those would diff the default
      # branch against itself and report a clean bill of health for a change it never read.
      pull-requests: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
        with:
          # The base ref has to be a commit this checkout has. Without it, configuration cannot be
          # loaded from the trusted ref and the diff cannot be taken.
          fetch-depth: 0
          # The token `actions/checkout` would otherwise leave in `.git/config` is one `git config`
          # away from a test the model staged into a linked worktree; this job pushes nothing (#308).
          persist-credentials: false
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      # BODY travels as an environment variable and is never interpolated into this script. A
      # `${{ github.event.comment.body }}` inside a `run:` is shell injection into the one job
      # holding a provider credential, written by anyone who can comment. The cassette name is
      # built from $ISSUE, a number, for the same reason.
      #
      # `--comment-out` writes the comment body for the NEXT job to post: the job that called the
      # model must not also hold the token that writes the repository.
      - run: |
          uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep review \\
            --ask "$BODY" --pr "$ISSUE" --comment-out findings.md \\
            --record --cassette "${RUNNER_TEMP}/review-${ISSUE}.json"
        env:
          BODY: ${{ github.event.comment.body }}
          ISSUE: ${{ github.event.issue.number }}
          # `gh` reads this to ask the host for the pull request's refs. The read token, not a
          # write one: this job's `permissions` above are what bound it.
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          IN_LOCKSTEP_SECRET: ${{ secrets.IN_LOCKSTEP_SECRET }}
          # A variable rather than a secret: a workspace id identifies, it does not authenticate.
          ANTHROPIC_WORKSPACE_ID: ${{ vars.ANTHROPIC_WORKSPACE_ID }}
      # Recording is ON. The tape stays in RUNNER_TEMP and dies with the runner; what survives is
      # the cases harvested from it, which ride the artifact below. Delete both steps if you would
      # rather keep nothing; nothing else depends on them.
      - name: Harvest what the review recorded
        # Harvest refuses rather than inventing, so a recording it cannot build a case from exits
        # non-zero. That is right for harvest and the wrong reason to fail somebody's review.
        continue-on-error: true
        run: |
          uvx --from 'in-lockstep[IN_LOCKSTEP_EXTRA]==IN_LOCKSTEP_VERSION' in-lockstep eval harvest \\
            --from "${RUNNER_TEMP}/review-${ISSUE}.json" \\
            --into .lockstep/cases \\
            --family review
        env:
          ISSUE: ${{ github.event.issue.number }}
      - run: uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep history --bundle history.bundle
        if: always()
        continue-on-error: true
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4
        if: always()
        with:
          name: lockstep-run
          path: |
            findings.md
            history.bundle
            .lockstep/cases/
          if-no-files-found: ignore
          # `.lockstep/` is a dotted path and upload-artifact@v4 drops hidden files by DEFAULT,
          # so without this the line above is silently ignored and the artifact arrives short.
          include-hidden-files: true
          # Said rather than inherited: the vendor default is 90 days, and this artifact holds
          # cases built from a prompt and a diff. Two weeks is enough to notice a run.
          retention-days: 14

  post:
    needs: review
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    permissions:
      # The write token, alone. `uvx` below installs no provider extra, so this process cannot
      # call a model even if something asked it to.
      pull-requests: write
      contents: read
      # Reading this run's own artifact.
      actions: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093  # v4
        with:
          name: lockstep-run
      # The marker that anchors the sticky comment is INSIDE the body `review` composed, so
      # nothing here names a lens. That is not economy: the lens came out of an untrusted comment
      # and was resolved in the other job, and naming it here would put it back in YAML.
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep comment \\
            --pr "$ISSUE" --body-file findings.md
        env:
          ISSUE: ${{ github.event.issue.number }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
"""


if __name__ == "__main__":  # pragma: no cover
    main()
