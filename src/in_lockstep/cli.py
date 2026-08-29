"""The `in-lockstep` command."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import click
from click.core import ParameterSource

from . import __version__
from .adapters.pytest_adapter import PytestTest, Test
from .adapters.ruff_adapter import RuffValidate, Validate
from .core.context import DISABLE_ENV
from .core.outcome import Status
from .core.types import TestSpec, ValidateSpec
from .core.verbs import SHIPPED_VERBS, Verb, verb_of
from .core.workflow import registered, workflow
from .lockstep import Lockstep
from .middleware.otel import Recorder, otel
from .privileged import sink
from .privileged.redact import Redact, redact_registry

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 3


def _default_lockstep() -> tuple[Lockstep, Recorder | None]:
    """A repository's own module if it has one, else detected defaults.

    The module is loaded from a trusted ref, never from the ref under review.

    The recorder comes back as `None` when the module declared its own middleware. The CLI cannot
    see through a chain it did not build, and printing `spans 0` for a run that emitted spans into
    somebody else's exporter is a wrong number rather than a missing one — which is the same
    reason `Outcome` carries `decided` instead of reporting an unjudged suite as passing.
    """
    from .loader import NoLifecycle, load, lockstep_from

    try:
        module, ref = load(
            ".",
            base=os.environ.get("GITHUB_BASE_REF", ""),
            reviewing=os.environ.get("GITHUB_EVENT_NAME", "") in ("pull_request", "pull_request_target"),
        )
        configured = lockstep_from(module)
        if configured.middleware:
            return configured, None
        recorder = Recorder()
        # Telemetry only. The CLI does not invent a ceiling: a `CostBudget` supplied here would be
        # a budget nobody chose, and it would make GATE-BUDGET-1 unsatisfiable — every run would
        # arrive bounded by a number the repository never decided on. `init` scaffolds one, and
        # `--budget` states one; both of those are somebody meaning it.
        configured.middleware = [otel(recorder)]
        return configured, recorder
    except NoLifecycle:
        pass

    lockstep = Lockstep.detect()
    lockstep.bind(Validate, RuffValidate(cwd=lockstep.repo.root))
    lockstep.bind(Test, PytestTest(args=["-q", "--no-header"], cwd=lockstep.repo.root))
    recorder = Recorder()
    lockstep.middleware = [otel(recorder)]
    return lockstep, recorder


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


def _context(lockstep: Lockstep, run_id: str) -> Any:
    """Start a run, turning a startup refusal into a message rather than a traceback.

    `UndeclaredBudget` lives in `core` and cannot inherit from `ClickException` — `core` imports
    nothing outward. Translating at this boundary is the same shape as every other refusal here:
    the exception carries the reason, the CLI decides how a person should see it.
    """
    from .core.spend import UndeclaredBudget
    from .core.verbs import UngatedAgency

    try:
        return lockstep.context(run_id=run_id)
    except (UndeclaredBudget, UngatedAgency) as e:
        raise click.ClickException(str(e)) from None


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
        cassette, diff, manifest = (
            root / "review-security.json",
            root / "example.diff",
            root / "fixture.json",
        )
        if not all(f.is_file() for f in (cassette, diff, manifest)):
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
            }
    except (ModuleNotFoundError, FileNotFoundError, KeyError):  # pragma: no cover - packaging
        return None


def _load_changeset(artifact: str) -> Any:
    """Read a ChangeSet from a file or an artifact directory."""
    import json
    from pathlib import Path as _Path

    from .core.types import ChangeAuthor, ChangeSet, FileChange

    path = _Path(artifact)
    payload = path / "changeset.json" if path.is_dir() else path
    if not payload.exists():
        raise click.ClickException(f"no changeset at {payload}")

    data = json.loads(payload.read_text())
    return ChangeSet(
        changes=tuple(
            FileChange(
                path=str(c["path"]),
                contents=c.get("contents"),
                author=ChangeAuthor(c.get("author", "agent")),
                symlink_target=c.get("symlink_target"),
            )
            for c in data.get("changes", [])
        ),
        summary=str(data.get("summary", "")),
        ticket=str(data.get("ticket", "")),
    )


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


def _echo_telemetry(recorder: Recorder | None) -> None:
    """What the CLI observed — or that it could not observe, which is a different statement."""
    if recorder is None:
        click.echo("spans     (lockstep.py declares its own middleware; the CLI is not in that chain)")
        return
    click.echo(f"spans     {len(recorder.spans)}")
    click.echo(f"metrics   {len(recorder.metrics)}")


@workflow(id="selfcheck")
async def selfcheck(ctx: Any, paths: tuple[str, ...]) -> dict[str, Any]:
    """Validate, then test. The smallest thing that proves the core actually dispatches."""
    validate = await ctx.do(Validate, ValidateSpec(paths=paths))
    if validate.status is Status.BLOCKED:
        return {"validate": validate, "tests": None}
    tests = await ctx.do(Test, TestSpec(paths=paths))
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


@main.command(name="run")
@click.argument("target")
@click.option("--paths", multiple=True, help="Restrict to these paths.")
@click.option("--no-middleware", is_flag=True, help="Bisect behaviour. Cannot disable the privileged tier.")
@click.option("--recover", "recover_id", default="", help="Resume an interrupted run by id.")
@click.option("--checkpoint/--no-checkpoint", default=False, help="Checkpoint completed steps.")
def run_cmd(
    target: str, paths: tuple[str, ...], no_middleware: bool, recover_id: str, checkpoint: bool
) -> None:
    """Run a workflow.

    `--recover` restarts an interrupted run from its checkpoints. It covers machine failure only:
    a run never waits on a person, so resuming after a human is a different mechanism entirely.
    """
    if target != "selfcheck":
        known = ", ".join(r.id for r in registered())
        raise click.ClickException(f"unknown workflow {target!r}; registered: {known}")

    lockstep, recorder = _default_lockstep()
    if no_middleware:
        # Note what this does NOT switch off: the kill switch is checked before the chain, and
        # redaction, egress and residency are privileged rather than middleware.
        lockstep.middleware = []

    run_id = recover_id or "selfcheck-local"
    ctx = _context(lockstep, run_id)
    if checkpoint or recover_id:
        from .platform.state import StateStore

        ctx.state = StateStore()
        ctx.recovering = bool(recover_id)
        if recover_id:
            done = ctx.state.completed(recover_id)
            click.echo(f"recovering {recover_id}: {len(done)} completed step(s)")
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

    if validate is not None and validate.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    statuses = [o.status for o in (validate, tests) if o is not None]
    if any(s is not Status.SUCCEEDED for s in statuses):
        raise SystemExit(EXIT_FAILED)


@main.command(name="ls")
def ls_cmd() -> None:
    """Print the resolved container and policy stack.

    Config as code can hide the effective setup in a way a YAML file cannot — you can read a
    manifest, but you cannot read a container. This is the answer to "what will actually run".
    """
    lockstep, _ = _default_lockstep()

    click.echo(f"repo      {lockstep.repo.root}")
    click.echo(
        f"head      {lockstep.repo.head[:12] or '(none)'}"
        f"  branch {lockstep.repo.branch or '(none)'}"
        f"{'  dirty' if lockstep.repo.dirty else ''}"
    )
    if os.environ.get(DISABLE_ENV):
        click.echo(f"DISABLED  {DISABLE_ENV} is set; no adapter will execute")

    # Verbs are open, so a verb can exist that the `bindings` block below will never mention.
    # That is exactly the shape a typo takes: `Verb("reviwe")` is a perfectly legitimate verb
    # that nothing serves. Shipped verbs with no binding are ordinary and would only be noise
    # here — seven of nine are unbound in a default install — so only the anomaly is printed.
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
        click.echo(
            f"  {label:<22} -> {impl.__name__:<16}({binding.scope.value}, {binding.tier.name.lower()})"
        )

    click.echo("")
    click.echo("middleware  (privileged tier runs outside this chain and is not listed)")
    for mw in lockstep.middleware:
        click.echo(f"  {type(mw).__name__}")

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
            f"  = network={resolved.network or '(unset)'}"
            f" scan={resolved.scan_input or '(unset)'}"
            f" deny_tools={len(resolved.deny_tools)}"
        )

    click.echo("")
    click.echo("workflows")
    for entry in registered():
        click.echo(f"  {entry.id}  ({entry.module})")


@main.command(name="eval")
@click.argument("action", default="report")
@click.option("--corpus", default="", help="Where the cases live.")
def eval_cmd(action: str, corpus: str) -> None:
    """Run the eval corpus offline.

    Deterministic expectations are settled here. Rubric expectations are reported as OUTSTANDING,
    because a judge has not answered them — recording them as passes would put a perfect score
    computed from no evidence into a baseline that is then compared against forever.
    """
    from pathlib import Path as _Path

    from .evaluation import load_cases, summarize
    from .evaluation.cases import grade

    root = _Path(corpus) if corpus else _Path(__file__).parent / "corpus"
    if not root.exists():
        raise click.ClickException(f"no corpus at {root}")

    cases = load_cases(root)
    if action == "list":
        for case in cases:
            rubric = " (rubric)" if case.rubric else ""
            click.echo(f"{case.name}{rubric}")
        click.echo(f"\n{len(cases)} case(s)")
        return

    # No model runs here: this reports what the corpus asks of one.
    results = [grade(case, None) for case in cases]
    summary = summarize(results)

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


@main.command(name="doctor")
@click.option("--strict", is_flag=True, help="What an organisation puts in a required check.")
def doctor_cmd(strict: bool) -> None:
    """Will the target accept this, and are the controls actually in place?"""
    from . import doctor as doctor_module

    report = doctor_module.run(".", strict=strict)
    click.echo(doctor_module.render(report))
    if not report.ok:
        raise SystemExit(EXIT_FAILED)


@main.command(name="apply")
@click.option("--from-artifact", "artifact", required=True, type=click.Path())
@click.option("--dry-run", is_flag=True, help="Check the changeset against the guard; write nothing.")
def apply_cmd(artifact: str, dry_run: bool) -> None:
    """Apply a ChangeSet produced by an earlier, unprivileged run.

    This is the privileged half of the two-job split. It holds a write token and never sees a
    provider credential — constructing it without a ProviderRegistry is asserted here rather than
    left as a convention.

    The artifact crossed a trust boundary to get here, so the path guard runs again over it. A
    previous job having produced it is not a reason to trust it.

    The write itself is not implemented: `Scm.open_change` exists and nothing calls it from here.
    So `--dry-run` checks the guard and succeeds, and a bare `apply` refuses rather than exiting 0
    on a job that wrote nothing.
    """
    changeset = _load_changeset(artifact)

    _guard_or_exit(changeset)

    click.echo(f"{len(changeset.changes)} change(s) pass the guard")
    for change in changeset.changes:
        click.echo(f"  {'delete' if change.deleted else 'write '} {change.path}")
    click.echo("")
    if dry_run:
        click.echo("guard only; nothing was written")
        return

    # Exiting 0 here made a CI job that wrote nothing report success, which is worse than the
    # missing feature: a green `apply` is read as "the change landed". The guard genuinely ran
    # and genuinely passed — that part is real — so the message says which half is missing.
    raise click.ClickException(
        "the guard passed, but `apply` does not write yet: Scm.open_change is implemented and "
        "nothing calls it from here. Nothing was applied. Pass --dry-run to check a changeset "
        "against the guard without implying a write."
    )


@main.command(name="review")
@click.option("--base", default="origin/main", help="What to diff against.")
@click.option("--head", default="HEAD")
@click.option("--aspect", default="security", help="Which lens.")
@click.option("--model", default="anthropic:claude-sonnet-4-6")
@click.option("--offline", is_flag=True, help="Serve model calls from a cassette. No keys, no spend.")
@click.option("--record", is_flag=True, help="Call the provider and write a cassette.")
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
@click.option("--dry-run", is_flag=True, help="Canned answer; proves the wiring, not the prompt.")
def review_cmd(
    base: str,
    head: str,
    aspect: str,
    model: str,
    offline: bool,
    record: bool,
    cassette: str,
    budget: float | None,
    dry_run: bool,
) -> None:
    """Review a change with one lens, in-process."""

    from .adapters.ai.review import AiReview, Review, ReviewSpec
    from .ai.auth import Auth
    from .ai.bootstrap import (
        LLMProvider,
        MissingCredential,
        Model,
        credentials_for,
        default_registry,
    )
    from .ai.invoker import AiInvoker, InvokePolicy
    from .ai.pricing import default_table
    from .ai.replay import Cassette, DryRunProvider, RecordingProvider, ReplayProvider
    from .core.spend import Budget
    from .privileged.egress import EgressPolicy

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
    if source("model") is ParameterSource.DEFAULT:
        model = lockstep.models.routes.get("review", model)

    # `--offline` with nothing else works out of the box, because both halves of a replay ship:
    # the recording, and the diff it was recorded against. A cassette is keyed on the whole
    # composed prompt and therefore on the diff inside it, so a fixture without its diff would
    # replay for nobody — the key would never match anything a user actually has.
    fixture = _shipped_fixture() if offline else None
    if fixture is not None:
        cassette = cassette or str(fixture["cassette"])
        source = click.get_current_context().get_parameter_source
        if source("base") is ParameterSource.DEFAULT and source("head") is ParameterSource.DEFAULT:
            demo_diff = str(fixture["diff"])
            base, head, aspect = fixture["base"], fixture["head"], fixture["aspect"]
            # Including the model. A recording is portable across provider implementations —
            # that is what the LLMInput/LLMOutput seam buys — but not across model ids, because
            # the id is part of the request being replayed. A repository's own route would
            # otherwise make the shipped fixture unreplayable for the person who most needs it.
            model = str(fixture["model"])
            click.echo(f"replaying the shipped fixture: {fixture['label']}")
        else:
            demo_diff = ""
    else:
        demo_diff = ""
    cassette = cassette or ".in-lockstep/cassettes/review.json"

    table = default_table()
    auth = Auth()
    try:
        registry = default_registry(auth)
    except MissingCredential as e:
        # Provider registration reads configuration, so a malformed value is caught here rather
        # than at the call. Same treatment either way: a setup problem is a message.
        raise click.ClickException(str(e)) from None
    selected = Model(model)
    tape = Cassette.load(cassette)

    def build_invoker(_ctx: Any) -> AiInvoker:
        provider: LLMProvider
        if dry_run:
            provider = DryRunProvider()
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
    # said something more specific than this default, and the CLI must not overrule it.
    if not lockstep.container.has(Review):
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

    ctx = _context(lockstep, f"review-{aspect}")
    try:
            outcome = asyncio.run(
            ctx.do(Review, ReviewSpec(base=base, head=head, aspect=aspect, diff=demo_diff))
        )
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
    click.echo(f"cost      ${cost.usd:.4f}")
    _echo_telemetry(recorder)

    # The ledger line the first-value assertion checks. Written even on failure: a run that cost
    # money and produced nothing is exactly the run worth having a record of.
    _write_ledger(ctx, outcome, aspect, selected.id)

    if outcome.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    if outcome.status is not Status.SUCCEEDED:
        raise SystemExit(EXIT_FAILED)


def _write_ledger(ctx: Any, outcome: Any, aspect: str, model_id: str) -> None:
    """One writer, through the store that owns the format.

    This hand-rolled the record and stamped `"schema": 2` and `"epoch": "in-process"` as literals
    beside `InRepoLedger`, which owns those constants and exists for exactly this. Two writers of
    one format is not an organisation problem: an epoch bump would have moved one of them, and the
    reader refuses to compare across epochs precisely so that a silent mismatch cannot average a
    credits-era number with a tokens-era one.
    """
    from .platform.ledger.store import InRepoLedger

    ledger = InRepoLedger()
    asyncio.run(
        ledger.append(
            ctx.run_id,
            {
                "kind": "review",
                "aspect": aspect,
                "model": model_id,
                "status": outcome.status.value,
                # The machine-readable refinement, which the terminal printed and the record
                # dropped. `reason` is what a failure is grouped by — provider.authentication is a
                # different problem from cost.budget_exceeded, and `status` calls both "errored".
                "reason": outcome.reason,
                "decided": outcome.decided,
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
            },
        )
    )
    click.echo(f"ledger    {ledger.path_for(ctx.run_id)}")


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


@main.command(name="show-prompt")
@click.argument("aspect", default="security")
@click.option("--projection", is_flag=True, help="Print the section-identity list only.")
def show_prompt_cmd(aspect: str, projection: bool) -> None:
    """Render a composed prompt offline, with per-fragment provenance.

    The successor to a committed flattened prompt tree. "What was the model actually told?" needs
    an answer that costs no run and no key — a cassette requires having already paid, and `ls`
    prints the container rather than the prompt.

    The projection it prints is the same one the characterization corpus asserts on, so one
    artifact serves both offline inspection and migration equivalence.
    """
    from .prompts.review import LENSES, review_layers

    lens = LENSES.get(aspect)
    if lens is None:
        raise click.ClickException(f"no lens named {aspect!r}; have {sorted(LENSES)}")

    prompt = lens()
    layers = review_layers()

    if projection:
        for section in layers.projection(f"review/{aspect}-reviewer"):
            click.echo(section)
        return

    click.echo(f"# composed prompt: review/{aspect}  (version {prompt.version})")
    click.echo("#")
    for section in layers.projection(f"review/{aspect}-reviewer"):
        click.echo(f"#   {section}")
    click.echo("")
    click.echo(prompt.system(layers))


@main.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite lockstep.py (never the trampoline).")
def init_cmd(force: bool) -> None:
    """Scaffold a lifecycle definition and a CI trampoline.

    The trampoline is written once and never read back: there is no drift check on it, and no
    --force for it. The day something compares it against a freshly generated one, it has become
    generated output rather than a scaffold, which is the line this framework exists on the other
    side of.
    """
    from pathlib import Path

    module = Path("lockstep.py")
    if module.exists() and not force:
        click.echo("lockstep.py exists (use --force to overwrite)")
    else:
        module.write_text(_SCAFFOLD_MODULE)
        click.echo("wrote lockstep.py")

    workflow = Path(".github/workflows/lockstep.yml")
    if workflow.exists():
        click.echo(f"{workflow} exists — left alone, deliberately")
    else:
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(_SCAFFOLD_TRAMPOLINE)
        click.echo(f"wrote {workflow}")
        click.echo("")
        click.echo("One job, because reviewing is read-only. Add the privileged `apply` job the")
        click.echo("day a verb of yours produces a change to write; the file says where.")


_SCAFFOLD_MODULE = '''"""The lifecycle for this repository.

This file IS the configuration: it is executed, not parsed. Anything you can express in Python
you can express here — but keep it pure, because it is imported to be inspected as well as run.
"""

from in_lockstep import Lockstep
from in_lockstep.adapters import PytestTest, RuffValidate
from in_lockstep.adapters.pytest_adapter import Test
from in_lockstep.adapters.ruff_adapter import Validate
from in_lockstep.middleware import CostBudget, otel

lockstep = Lockstep.detect()

# Deterministic verbs bind adapters over real tools.
lockstep.bind(Test, PytestTest(args=["-q"]))
lockstep.bind(Validate, RuffValidate())

# Cross-cutting behaviour is middleware. Redaction, egress and the kill switch are NOT here:
# they are privileged, and `--no-middleware` cannot reach them.
lockstep.middleware += [otel(), CostBudget(usd=2.00)]
'''

_SCAFFOLD_TRAMPOLINE = """# Invokes the CLI. Contains no lifecycle logic, and is never regenerated.
#
# One job, because reviewing is read-only: it needs a provider credential and `contents: read`,
# and nothing else. The two-job split — an unprivileged job that talks to a model, a privileged
# one that writes — is what keeps an API key and a write token out of the same process, and it
# is what you add here the day a verb of yours produces a change to apply. Adding it now would
# scaffold a job with nothing to do.
#
# The base ref is passed explicitly because configuration is loaded from it: lockstep.py comes
# from the base branch, never from the ref under review, or a pull request could supply the file
# defining the bindings and policy that constrain reviewing it.
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
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v6
        with:
          python-version: '3.11'
      - name: Are the controls in place?
        run: uvx --from 'in-lockstep[anthropic]' in-lockstep doctor
        continue-on-error: true
      - name: Review
        run: |
          uvx --from 'in-lockstep[anthropic]' in-lockstep review \
            --base "origin/${GITHUB_BASE_REF}" \
            --head "${GITHUB_SHA}" \
            --aspect security
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          IN_LOCKSTEP_EGRESS: enforced
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: lockstep-run
          path: .in-lockstep/
          if-no-files-found: ignore
"""


if __name__ == "__main__":  # pragma: no cover
    main()
