"""The `in-lockstep` command."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import click
from click.core import ParameterSource

from . import __version__
from .adapters.pytest_adapter import Test
from .adapters.ruff_adapter import Validate
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
        # which is now a refusal rather than this.
        click.echo(f"config    none ({e})", err=True)

    lockstep = Lockstep.detect()
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


def _context(lockstep: Lockstep, run_id: str, approval: Any = None) -> Any:
    """Start a run, turning a startup refusal into a message rather than a traceback.

    `UndeclaredBudget` lives in `core` and cannot inherit from `ClickException` — `core` imports
    nothing outward. Translating at this boundary is the same shape as every other refusal here:
    the exception carries the reason, the CLI decides how a person should see it.
    """
    from .core.spend import UndeclaredBudget
    from .core.verbs import UngatedAgency

    try:
        return lockstep.context(run_id=run_id, approval=approval)
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

    ctx = _context(
        lockstep,
        f"{entry.id.replace('/', '-')}-{os.environ.get('GITHUB_RUN_ID', 'local')}",
        approval,
    )
    try:
        result = asyncio.run(entry.fn(ctx, **parsed))
    except TypeError as e:
        # A signature mismatch is the common mistake here, and the traceback for it points at
        # asyncio rather than at the workflow the user named.
        raise click.ClickException(
            f"{entry.id} did not accept those arguments: {e}. It takes "
            f"{', '.join(_parameters(entry.fn)) or '(no parameters)'}."
        ) from None
    except _SETUP_ERRORS as e:
        # The same translation `review` and `implement` already do. Without it the path this
        # framework recommends — put the process in `lockstep.py`, invoke it with `run` — gives a
        # forty-line traceback where a bespoke command gives one sentence, which is a reason not
        # to take the recommendation.
        raise click.ClickException(str(e)) from None

    click.echo(f"{entry.id}  {_describe(result)}")
    # Findings, not just the verdict. For a workflow whose whole product is a judgement, the
    # status line is the least interesting part of it.
    for finding in getattr(result, "findings", ())[:20]:
        where = f"{finding.path}:{finding.line} " if getattr(finding, "path", "") else ""
        click.echo(f"  {where}{finding.id}: {finding.message}")
    click.echo("")
    _echo_telemetry(recorder)
    click.echo(f"spend     ${ctx.spend.charged.usd:.4f}, {ctx.spend.charged.wall_seconds:.2f}s")
    _write_workflow_ledger(ctx, entry.id, result, parsed)
    _exit_for(result)


def _ledger(lockstep: Any = None) -> Any:
    """Where a run record goes. One decision, made once.

    Order: a store the repository bound, then the orphan branch, then a file in the working tree.

    The last is a fallback and not a default. `.lockstep/` is gitignored, so a record written
    there is written and then lost — which is what shipped, and why the ledger held no evidence
    while the crosswalk cited it as the project's evidence. It stays for directories that are not
    git repositories at all, where the branch cannot exist.
    """
    import subprocess

    from .platform.ledger import GitLedger, InRepoLedger

    if lockstep is not None:
        from .core.ports import LedgerStore

        if lockstep.container.has(LedgerStore):
            return lockstep.container.resolve(LedgerStore)

    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return InRepoLedger()
    return GitLedger() if inside.stdout.strip() == "true" else InRepoLedger()


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


def _write_workflow_ledger(ctx: Any, workflow_id: str, result: Any, args: dict[str, str]) -> None:
    """Every dispatched run leaves a record, not only the ones with a bespoke command.

    Without this, moving a process out of `review`/`implement` and into a `@workflow` — which is
    what this framework asks you to do — silently costs the run its evidence. A record that exists
    for the built-in path and not the recommended one is an argument for not taking the
    recommendation.

    The `--arg` values are recorded because they are the provenance: which issue, which actor.
    They pass through the same redacting writer as everything else.
    """
    cost = getattr(result, "cost", None)
    findings = getattr(result, "findings", ()) or ()
    _record(
        _ledger(),
        ctx.run_id,
        {
            "kind": "workflow",
            "workflow": workflow_id,
            "args": dict(args),
            # Who asked, and whether they watched. Absent when nobody did, which is the
            # ordinary case for a workflow needing no grant.
            **({"approval": ctx.approval.as_record()} if ctx.approval.granted else {}),
            "status": getattr(getattr(result, "status", None), "value", "completed"),
            "reason": getattr(result, "reason", None),
            "decided": getattr(result, "decided", True),
            "tokens": ctx.spend.charged.total_tokens,
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


def _parameters(fn: Any) -> list[str]:
    import inspect

    return [p for p in inspect.signature(fn).parameters if p != "ctx"]


def _describe(result: Any) -> str:
    """A workflow returns whatever it likes; say something true about it either way."""
    status = getattr(result, "status", None)
    if status is None:
        return "completed"
    reason = getattr(result, "reason", None)
    decided = getattr(result, "decided", True)
    return (
        f"{status.value}" + (f"  ({reason})" if reason else "") + ("" if decided else "  (decided nothing)")
    )


def _exit_for(result: Any) -> None:
    status = getattr(result, "status", None)
    if status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    if status is not None and status is not Status.SUCCEEDED:
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
async def selfcheck(ctx: Any, paths: tuple[str, ...]) -> dict[str, Any]:
    """Validate, then test. The smallest thing that proves the core actually dispatches.

    Skips a verb nothing is bound to rather than raising: detection binds only what it found, so a
    repository whose stack it could not place has no Test or Validate, and a `ResolutionError`
    traceback there would read as a broken tool rather than an unconfigured one.
    """
    bound = ctx.container.has
    validate = await ctx.do(Validate, ValidateSpec(paths=paths)) if bound(Validate) else None
    if validate is not None and validate.status is Status.BLOCKED:
        return {"validate": validate, "tests": None}
    tests = await ctx.do(Test, TestSpec(paths=paths)) if bound(Test) else None
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
    """
    lockstep, recorder = _default_lockstep()

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
        _run_registered(
            lockstep, recorder, known[target], args, no_middleware, _approval(approve, approved_by)
        )
        return
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
    detected = lockstep.repo.facts.summary()
    if detected:
        click.echo(f"detected  {'; '.join(detected)}")
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

    routes = lockstep.models.routes
    if routes:
        click.echo("")
        click.echo("models  (verb -> model; resolved per verb, overridable at the call)")
        known = {v.value for v in Verb.known()}
        for routed_verb, model_id in sorted(routes.items()):
            flag = "" if routed_verb in known else "  <- no such verb (typo?)"
            click.echo(f"  {routed_verb:22} {model_id}{flag}")

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


@main.command(name="history")
@click.option("--push", is_flag=True, help="Publish the branch. Needs push access; never automatic.")
@click.option(
    "--bundle", default="", type=click.Path(), help="Write the branch to a file to travel as an artifact."
)
@click.option("--from-bundle", default="", type=click.Path(), help="Take in history another job recorded.")
@click.option("--limit", type=int, default=20, show_default=True)
def history_cmd(push: bool, bundle: str, from_bundle: str, limit: int) -> None:
    """Run records, on an orphan branch that touches nothing anybody works on.

    Records are committed locally as each run finishes. Publishing is a separate act, because
    reaching a remote needs credentials and is a side effect nobody asked for by typing a command
    in a terminal — so a laptop accumulates history and CI, or a person, pushes it.
    """
    from .platform.ledger import GitLedger, HistoryError

    ledger = GitLedger()

    # Absorb first, so `--from-bundle --push` in one invocation does what it reads like.
    if from_bundle:
        try:
            ledger.absorb(from_bundle)
        except HistoryError as e:
            raise click.ClickException(str(e)) from None
        click.echo(f"absorbed  {from_bundle}")

    head = ledger.head()
    if head is None:
        click.echo(f"no history yet on {ledger.branch}; it is created by the first run that records one")
        return

    records = ledger.records()
    click.echo(f"branch    {ledger.branch}  ({head[:12]}, {len(records)} record(s))")
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
@click.option("--title", default="", help="Change title. Defaults to the changeset's summary.")
@click.option("--body", default="", help="Change description.")
@click.option("--workflow", "workflow_id", default="implement", help="Names the run-scoped branch.")
@click.option("--run-id", default="", help="Names the run-scoped branch. Defaults to the CI run id.")
def apply_cmd(artifact: str, dry_run: bool, title: str, body: str, workflow_id: str, run_id: str) -> None:
    """Apply a ChangeSet produced by an earlier, unprivileged run.

    This is the privileged half of the two-job split. It holds a write token and never sees a
    provider credential — asserted below rather than left as a convention.

    The artifact crossed a trust boundary to get here, so the path guard runs again over it. A
    previous job having produced it is not a reason to trust it.

    Where it goes is `Scm.open_change`, and the branch it writes to is refused by the framework
    unless it is run-scoped. That refusal matters because the token here is ambient and can write
    any branch: branch protection is the host's half, and this is ours.
    """
    from .core.spend import Budget
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
    # A budget is not needed here: nothing in this command spends. Stating it keeps GATE-BUDGET-1
    # from refusing a run that genuinely cannot spend a cent.
    if not lockstep.declared_ceiling().declared:
        lockstep.budget = Budget(usd=0.0)

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
    "--pr", "pr_number", type=int, default=None, help="The PR to comment on (else detected from CI)."
)
def review_cmd(
    base: str,
    head: str,
    aspect: str,
    model: str,
    offline: bool,
    record: bool,
    cassette: str,
    budget: float | None,
    diff_file: str,
    dry_run: bool,
    post_comment: bool,
    pr_number: int | None,
) -> None:
    """Review a change with one lens, in-process.

    `--diff` reads a patch from a file rather than asking git for one. That is a real use — a
    patch that is not a commit yet, a diff produced somewhere else — and it is also what makes
    this command testable without constructing a repository with a history in it.
    """

    from .adapters.ai.review import AiReview, Review, ReviewSpec
    from .ai.auth import Auth
    from .ai.bootstrap import (
        LLMProvider,
        MissingCredential,
        Model,
        credentials_for,
        default_registry,
        table_for,
    )
    from .ai.invoker import AiInvoker, InvokePolicy
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
    cassette = cassette or ".lockstep/cassettes/review.json"

    auth = Auth()
    try:
        registry = default_registry(auth)
    except MissingCredential as e:
        # Provider registration reads configuration, so a malformed value is caught here rather
        # than at the call. Same treatment either way: a setup problem is a message.
        raise click.ClickException(str(e)) from None
    selected = Model(model)
    table = table_for(registry, selected, _bound_cost_table(lockstep))
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

    ctx = _context(lockstep, f"review-{aspect}")
    try:
        outcome = asyncio.run(
            ctx.do(
                Review,
                ReviewSpec(base=base, head=head, aspect=aspect, diff=supplied or demo_diff),
            )
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
    click.echo(f"cost      ${cost.usd:.4f}{_billing_note(cost)}")
    _echo_telemetry(recorder)

    # The ledger line the first-value assertion checks. Written even on failure: a run that cost
    # money and produced nothing is exactly the run worth having a record of.
    _write_ledger(ctx, outcome, aspect, selected.id if review_model_is_ours else "")

    if post_comment:
        _post_review_comment(lockstep, aspect, outcome, pr_number)

    if outcome.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    if outcome.status is not Status.SUCCEEDED:
        raise SystemExit(EXIT_FAILED)


_TRIAGE_DRY_RUN = (
    '{"kind": "bug", "priority": "normal", "reason": "canned dry-run answer; proves the wiring, '
    'not the model.", "missing": [], "acceptance_criteria": [], "labels": [], "comment": ""}'
)


@main.command(name="triage")
@click.option("--ticket", default="", help="An issue key to read from the tracker, e.g. '#42'.")
@click.option(
    "--ticket-file",
    default="",
    type=click.Path(),
    help="A ticket read off disk — JSON (the eval-corpus shape) or markdown. No tracker, no network.",
)
@click.option("--model", default="anthropic:claude-haiku-4-5", help="Triage is a cheap reading task.")
@click.option("--offline", is_flag=True, help="Serve model calls from a cassette. No keys, no spend.")
@click.option("--record", is_flag=True, help="Call the provider and write a cassette.")
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
    record: bool,
    cassette: str,
    budget: float | None,
    dry_run: bool,
) -> None:
    """Read one issue and place it: what kind of work, how urgent, what it is missing.

    Read-only and label-only. The analyst reports a decision; acting on it — a comment, a label,
    a duplicate link — is a separate step the caller takes through `TicketSource`, which is why
    the `triage` guardrail denies the issue-writing tools.
    """
    from .adapters.ai.triage import AiTriage, Triage
    from .ai.auth import Auth
    from .ai.bootstrap import (
        LLMProvider,
        MissingCredential,
        Model,
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
    tape = Cassette.load(cassette or ".lockstep/cassettes/triage.json")

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

    ctx = _context(lockstep, f"triage-{spec.key.lstrip('#') or 'issue'}")
    try:
        outcome = asyncio.run(ctx.do(Triage, spec))
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
    _echo_telemetry(recorder)

    _write_ledger(ctx, outcome, "", selected.id if triage_model_is_ours else "", kind="triage")

    if outcome.status is Status.BLOCKED:
        raise SystemExit(EXIT_BLOCKED)
    if outcome.status is not Status.SUCCEEDED:
        raise SystemExit(EXIT_FAILED)


def _triage_spec(ticket: str, ticket_file: str, root: str, source: Any = None) -> Any:
    """A `TriageSpec` from a tracker key or a file. A JSON file may carry the richer eval-corpus
    shape (discussion, criteria_source); a markdown file or a real issue goes through the Ticket
    mapping, so what triage sees offline matches what it sees against a live tracker."""
    import json
    from pathlib import Path as _Path

    from .adapters.ai.triage import TriageSpec

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
    return TriageSpec.from_ticket(_load_ticket(ticket, ticket_file, root, source=source))


def _triage_spec_from_dict(data: dict[str, Any], *, fallback_key: str) -> Any:
    from .adapters.ai.triage import TriageSpec
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
    # matching `TriageSpec.from_ticket`. A filled criteria list under `criteria_source: none` would
    # tell the analyst to treat present criteria as missing.
    criteria_source = str(data.get("criteria_source") or ("description" if criteria else "none"))
    return TriageSpec(
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
    from .platform.report import marker, review_comment
    from .platform.scm import GitHubScm, Scm

    ci_env = detect_ci()
    number = pr_number if pr_number is not None else (ci_env.pr_number if ci_env else None)
    if not number:
        click.echo(
            "comment   no PR number (pass --pr, or run in a pull-request pipeline); not posted", err=True
        )
        return

    scm: Any = lockstep.container.resolve(Scm) if lockstep.container.has(Scm) else None  # type: ignore[type-abstract]
    if not hasattr(scm, "upsert_comment"):
        scm = GitHubScm(root=lockstep.repo.root)
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


def _write_ledger(ctx: Any, outcome: Any, aspect: str, model_id: str, *, kind: str = "review") -> None:
    """One writer, through the store that owns the format.

    This hand-rolled the record and stamped `"schema": 2` and `"epoch": "in-process"` as literals
    beside `InRepoLedger`, which owns those constants and exists for exactly this. Two writers of
    one format is not an organisation problem: an epoch bump would have moved one of them, and the
    reader refuses to compare across epochs precisely so that a silent mismatch cannot average a
    credits-era number with a tokens-era one.

    `kind` names the verb; `aspect` is the review lens and is omitted for verbs that have none, so
    a triage record does not carry an empty lens field that reads as a missing one.
    """
    _record(
        _ledger(),
        ctx.run_id,
        {
            "kind": kind,
            **({"aspect": aspect} if aspect else {}),
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
def gate_cmd(actor: str, association: str, codeowners: str) -> None:
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
@click.option("--ticket", default="", help="Issue key to fetch, e.g. '#42'.")
@click.option("--ticket-file", default="", type=click.Path(), help="Read the ticket from a file instead.")
@click.option("--strategy", default="", help="Which approach. Defaults to the registry's.")
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
@click.option("--record", is_flag=True, help="Call the provider and write a cassette.")
@click.option("--cassette", default="", help="Where to read or write a recording.")
@click.option("--budget", type=float, default=None, help="Hard ceiling, in USD.")
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
    record: bool,
    cassette: str,
    budget: float | None,
    dry_run: bool,
) -> None:
    """Implement one ticket, in-process, staging the change rather than writing it.

    Nothing here touches the working tree. The session stages writes into a `ChangeSet`, which
    `--out` serializes — and `apply-inline` or `apply --from-artifact` is what writes it, through
    the same guard a second time. That separation is the point: a model that has just read a
    ticket written by anybody is not the thing that should also hold the ability to write.
    """

    from .adapters.ai.implement import AiImplement, Implement, ImplementSpec
    from .adapters.sandbox import Sandbox
    from .ai.auth import Auth
    from .ai.bootstrap import (
        LLMProvider,
        MissingCredential,
        Model,
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
    from .strategies import default_registry as default_strategies

    if bool(ticket) == bool(ticket_file):
        raise click.ClickException("pass exactly one of --ticket or --ticket-file")

    lockstep, recorder = _default_lockstep()

    source = click.get_current_context().get_parameter_source
    if budget is not None:
        lockstep.budget = Budget(usd=budget)
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

    resolved = _load_ticket(ticket, ticket_file, lockstep.repo.root, source=_bound_ticket_source(lockstep))

    auth = Auth()
    try:
        providers = default_providers(auth)
    except MissingCredential as e:
        raise click.ClickException(str(e)) from None
    selected = Model(model)
    table = table_for(providers, selected, _bound_cost_table(lockstep))
    tape = Cassette.load(cassette or ".lockstep/cassettes/implement.json")

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
        lockstep.bind(
            Implement,
            AiImplement(
                build_invoker,
                registry=default_strategies(),
                repo_root=lockstep.repo.root,
                # A sandbox, or nothing. `--no-execute` withholds the runner rather than the
                # tool, so the capability the tool set declares — and therefore what egress and
                # approval see — does not change with the flag. A run that could execute on some
                # other configuration must not read as harmless on this one.
                # `require_container` follows the image, so naming one and not getting one is a
                # refusal rather than a quiet downgrade to running on the host. Passing
                # `--sandbox-image ''` is the deliberate way to ask for the host, and it reads
                # like a decision because it is one.
                commands=(
                    Sandbox(image=sandbox_image, require_container=bool(sandbox_image)) if execute else None
                ),
                policy=InvokePolicy.under(
                    lockstep.policy.resolve(),
                    max_turns=max_turns,
                    max_tokens=_IMPLEMENT_MAX_TOKENS,
                    deadline_seconds=1800,
                ),
            ),
        )

    ctx = _context(lockstep, f"implement-{resolved.key.lstrip('#')}", approval)
    try:
        outcome = asyncio.run(ctx.do(Implement, ImplementSpec(ticket=resolved, strategy=strategy)))
    except LookupError as e:
        raise click.ClickException(f"{e} (a cassette replays only the prompt it was recorded on)") from None
    except (ImportError, MissingCredential) as e:
        raise click.ClickException(str(e)) from None

    report = outcome.value
    label = report.strategy if report is not None and report.strategy else (strategy or "implement")
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
    click.echo(f"turns     {report.turns if report is not None else 0}")
    click.echo(f"tokens    {cost.input_tokens} in, {cost.output_tokens} out")
    click.echo(f"cost      ${cost.usd:.4f}{_billing_note(cost)}")
    _echo_telemetry(recorder)

    if out and report is not None and report.changeset.changes:
        _write_artifact(out, report.changeset)
        click.echo(f"changeset {out}")
        click.echo("")
        click.echo(f"Nothing was written. Apply it with:  in-lockstep apply-inline --from-artifact {out}")

    _write_implement_ledger(
        ctx, outcome, label, selected.id if cli_chose_the_model else "", approval, ticket=resolved
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


def _load_ticket(key: str, path: str, root: str, source: Any = None) -> Any:
    """A ticket from a file, a bound `TicketSource`, or the GitHub default.

    A repository that binds `JiraSource` (or any other tracker) in its module has `source` passed
    in, so `implement --ticket PROJ-123` reaches Jira; with nothing bound, GitHub Issues is the
    zero-config default, matching what `_default_lockstep` assumes elsewhere.
    """
    if path:
        return _ticket_from_file(path)
    from pathlib import Path as _Path

    from .platform.tickets import GitHubIssues

    tracker = source if source is not None else GitHubIssues(root=_Path(root))
    try:
        return asyncio.run(tracker.get(key))
    except (RuntimeError, OSError) as e:
        raise click.ClickException(f"could not read ticket {key!r}: {e}") from None


def _write_artifact(path: str, changeset: Any) -> None:
    from .platform.artifacts import write_changeset

    write_changeset(path, changeset)


def _write_implement_ledger(
    ctx: Any, outcome: Any, strategy: str, model_id: str, approval: Any = None, ticket: Any = None
) -> None:
    """The same store `review` writes through, with the fields that differ for this verb."""
    report = outcome.value
    _record(
        _ledger(),
        ctx.run_id,
        {
            "kind": "implement",
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
            "turns": report.turns if report is not None else 0,
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


@main.command(name="show-prompt")
@click.argument("name", default="security")
@click.option("--projection", is_flag=True, help="Print the section-identity list only.")
def show_prompt_cmd(name: str, projection: bool) -> None:
    """Render a composed prompt offline, with per-fragment provenance.

    The successor to a committed flattened prompt tree. "What was the model actually told?" needs
    an answer that costs no run and no key — a cassette requires having already paid, and `ls`
    prints the container rather than the prompt.

    The projection it prints is the same one the characterization corpus asserts on, so one
    artifact serves both offline inspection and migration equivalence.

    NAME is a review aspect (`security`), a strategy id (`implement/oneshot`), or a triage prompt
    (`triage/analyst`). Each, because an agentic prompt is the one most worth reading before it is
    run: it composes a different guardrail and a different skill, and — for implement — is attached
    to a tool set that can write.
    """
    from .prompts.implement import PROMPTS, implement_layers
    from .prompts.review import LENSES, review_layers
    from .prompts.triage import TRIAGE_PROMPTS, triage_layers

    if name in PROMPTS:
        prompt: Any = PROMPTS[name]()
        layers = implement_layers()
        label, body_name = name, "implement/oneshot-implementer"
    elif name in LENSES:
        prompt = LENSES[name]()
        layers = review_layers()
        label, body_name = f"review/{name}", f"review/{name}-reviewer"
    elif name in TRIAGE_PROMPTS:
        prompt = TRIAGE_PROMPTS[name]()
        layers = triage_layers()
        label, body_name = name, "triage/triage-analyst"
    else:
        raise click.ClickException(
            f"no prompt named {name!r}; have {sorted(LENSES)}, {sorted(PROMPTS)} and {sorted(TRIAGE_PROMPTS)}"
        )

    if projection:
        for section in layers.projection(body_name):
            click.echo(section)
        return

    click.echo(f"# composed prompt: {label}  (version {prompt.version})")
    click.echo("#")
    for section in layers.projection(body_name):
        click.echo(f"#   {section}")
    click.echo("")
    click.echo(prompt.system(layers))


@main.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite lockstep.py (never the trampoline).")
@click.option(
    "--implement",
    "with_implement",
    is_flag=True,
    help="Also scaffold the /implement chat-ops trampoline and its two workflows.",
)
def init_cmd(force: bool, with_implement: bool) -> None:
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

    module = Path(MODULE_FILE)
    if module.exists() and not force:
        click.echo(f"{MODULE_FILE} exists (use --force to overwrite)")
    else:
        facts = Lockstep.detect().repo.facts
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(_scaffold_module(facts))
        click.echo(f"wrote {MODULE_FILE}")
        found = facts.summary()
        if found:
            click.echo(f"  detected {'; '.join(found)}")

    if _write_trampoline(Path(".github/workflows/lockstep.yml"), _SCAFFOLD_TRAMPOLINE):
        click.echo("")
        click.echo("One job, because reviewing is read-only. Add the privileged `apply` job the")
        click.echo("day a verb of yours produces a change to write; the file says where.")

    if with_implement:
        _scaffold_implement(module)


def _write_trampoline(path: Path, template: str) -> bool:
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
    path.write_text(template.replace("IN_LOCKSTEP_VERSION", __version__))
    click.echo(f"wrote {path}")
    return True


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


def _scaffold_implement(module: Path) -> None:
    """The `/implement` chat-ops flow: a three-job trampoline, and the two workflows it fires.

    The headline feature used to require reverse-engineering this repository's own trampoline.
    The YAML holds only what CI owns — trigger, job split, credentials — and everything the
    comment actually does is appended to lockstep.py as Python.
    """
    _write_trampoline(Path(".github/workflows/implement.yml"), _SCAFFOLD_IMPLEMENT_TRAMPOLINE)

    text = module.read_text() if module.exists() else ""
    if "implement/from-issue" in text:
        click.echo(f"{module} already defines implement/from-issue — left alone")
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
        merged = text + _SCAFFOLD_IMPLEMENT_MODULE
        try:
            # Never leave a module that will not import: a bad append breaks every later command,
            # not just this one. Refuse before writing rather than after loading.
            compile(merged, str(module), "exec")
        except SyntaxError as e:
            click.echo(f"{module} would not parse after adding the implement block ({e}); left alone.")
        else:
            module.write_text(merged)
            click.echo(f"extended {module} with implement/from-issue and implement/propose")

    click.echo("")
    click.echo("Three things make it real:")
    click.echo("  1. Set the ANTHROPIC_API_KEY repository secret.")
    click.echo("  2. Optionally add required reviewers to the `implement` environment in repository")
    click.echo("     settings — that makes the propose job an approval in the system of record.")
    click.echo("  3. Read the EGRESS note in the appended block: the review scaffold's")
    click.echo("     UnsandboxedEgress binding is global, so this write-capable verb inherits it.")
    click.echo("     The comment names what still bounds a session, and how to enforce egress.")


def _scaffold_module(facts: Any) -> str:
    """The lifecycle scaffold, reflecting what detection found in the tree.

    The deterministic-verb binds are generated from the facts, the same way `detected_bindings`
    binds the drop-in defaults: a Node repository gets `CommandTest(["npm", "test"])`, and a part
    detection could not place is a commented stub — with its own import — rather than a wrong
    default that runs. Only the adapters actually bound are imported, so a generated module never
    ships an unused import. Everything else — the egress opt-out and the middleware — is identical
    in every scaffold; the trampoline is byte-identical across repos, and this file is the one
    `init` fits to the stack.
    """
    imports: list[str] = ["Test", "Validate"]
    test_bind = _bind_line(facts, "Test", imports)
    validate_bind = _bind_line(facts, "Validate", imports)
    # Nothing detected for a verb → its interface is referenced only in a comment, so drop it from
    # the import to avoid an unused name; the stub carries its own commented import instead. If
    # nothing was placed at all, there is no adapter import line — an empty one is a syntax error.
    used = sorted(set(imports))
    adapter_import = (
        f"from in_lockstep.adapters import {', '.join(used)}"
        if used
        else "# No adapters detected yet — bind them in the stubs below."
    )
    return _SCAFFOLD_MODULE.format(
        adapter_import=adapter_import, test_bind=test_bind, validate_bind=validate_bind
    )


def _bind_line(facts: Any, verb: str, imports: list[str]) -> str:
    """One deterministic-verb bind for the scaffold, plus the imports it needs (appended in place).
    A commented, self-contained stub when detection placed nothing, so the generated module has no
    default that runs unbidden and no name imported but unused."""
    if verb == "Test":
        if getattr(facts, "pytest", False):
            imports.append("PytestTest")
            return 'lockstep.bind(Test, PytestTest(args=["-q"]))'
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
{adapter_import}
from in_lockstep.middleware import CostBudget, otel
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress

lockstep = Lockstep.detect()

# Deterministic verbs bind adapters over real tools. `in-lockstep ls` prints what detection found.
{test_bind}
{validate_bind}

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
# One job, because reviewing is read-only: it needs a provider credential and `contents: read`,
# and nothing else. The two-job split — an unprivileged job that talks to a model, a privileged
# one that writes — is what keeps an API key and a write token out of the same process, and it
# is what you add here the day a verb of yours produces a change to apply. Adding it now would
# scaffold a job with nothing to do.
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
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - name: Are the controls in place?
        # No credential here: doctor reads config from the trusted base ref, so it never executes
        # the change under review, and giving the key to a step that does not call a model just
        # widens where it can leak.
        run: uvx --from 'in-lockstep[anthropic]==IN_LOCKSTEP_VERSION' in-lockstep doctor
        continue-on-error: true
      - name: Review
        # Skipped without a credential rather than failed: a pull request from a fork gets no
        # secrets, and a red check the contributor cannot fix teaches everyone to ignore red.
        # The `secrets` context reads in a step `if`, which keeps the key scoped to this one step
        # instead of every step in the job.
        if: ${{ secrets.ANTHROPIC_API_KEY != '' }}
        run: |
          uvx --from 'in-lockstep[anthropic]==IN_LOCKSTEP_VERSION' in-lockstep review \
            --base "origin/${GITHUB_BASE_REF}" \
            --head "${GITHUB_SHA}" \
            --aspect security \
            --budget 0.75
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          # A variable rather than a secret: a workspace id identifies, it does not authenticate.
          # Leave it unset unless your key is identity-linked; empty sends no header.
          ANTHROPIC_WORKSPACE_ID: ${{ vars.ANTHROPIC_WORKSPACE_ID }}
      - name: No provider credential
        if: ${{ secrets.ANTHROPIC_API_KEY == '' }}
        run: echo "no ANTHROPIC_API_KEY (fork pull request?) — review skipped, nothing failed"
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4
        if: always()
        with:
          name: lockstep-run
          path: .lockstep/
          if-no-files-found: ignore
"""

_SCAFFOLD_IMPLEMENT_TRAMPOLINE = """\
# `/implement` on an issue. Hand-written and permanent; nothing generates or checks it.
#
# THIS FILE CONTAINS NO LIFECYCLE LOGIC. What `/implement` actually does — read the issue, run
# the strategy, stage a change, open a pull request, reply on the thread — is
# `implement/from-issue` and `implement/propose` in .lockstep/lockstep.py, where it is Python
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
    if: >-
      !github.event.issue.pull_request &&
      startsWith(github.event.comment.body, '/implement')
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
    timeout-minutes: 30
    permissions:
      contents: read
      # Read-only, and needed: the workflow resolves TicketSource to fetch the issue.
      issues: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e  # v6
        with:
          python-version: '3.11'
      - run: uvx --from 'in-lockstep[anthropic]==IN_LOCKSTEP_VERSION' in-lockstep doctor
        continue-on-error: true
      # The same command a developer runs, with `--approved-by` where they would type
      # `--approve`. The process does not change when it moves from a terminal to a trigger —
      # only who the human is and how they were verified.
      - run: |
          uvx --from 'in-lockstep[anthropic]==IN_LOCKSTEP_VERSION' in-lockstep run implement/from-issue \\
            --arg issue="#${ISSUE}" \\
            --approved-by "${ACTOR}" \\
            --budget 2.00
        env:
          ISSUE: ${{ github.event.issue.number }}
          # A name GitHub computed and the gate verified, not one the comment claimed.
          ACTOR: ${{ needs.gate.outputs.actor }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
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
          name: implement-${{ github.event.issue.number }}
          path: |
            changeset/
            history.bundle
          if-no-files-found: ignore

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
          name: implement-${{ github.event.issue.number }}
          path: ${{ runner.temp }}/implement
      - run: |
          uvx --from 'in-lockstep==IN_LOCKSTEP_VERSION' in-lockstep run implement/propose \\
            --arg issue="#${ISSUE}" \\
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

_SCAFFOLD_IMPLEMENT_MODULE = '''

# -- /implement: the chat-ops implementing verb -------------------------------------------------
#
# Two workflows rather than one, because they must not be one process. `implement/from-issue`
# runs unprivileged with the provider key and stages a change into an artifact;
# `implement/propose` runs privileged with a write token and no provider key. The trampoline in
# .github/workflows/implement.yml holds the trigger, the job split and the credentials — and
# nothing else. Run the first half locally with:
#
#     in-lockstep run implement/from-issue --arg issue='#42' --approve --budget 2.00

from typing import Any

from in_lockstep.adapters.ai.implement import AiImplement, Implement, ImplementSpec
from in_lockstep.adapters.pytest_adapter import PytestTest, Test
from in_lockstep.adapters.sandbox import Sandbox
from in_lockstep.adapters.worktree import verdict_over_staged
from in_lockstep.ai.bootstrap import invoker_factory
from in_lockstep.ai.invoker import InvokePolicy
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.workflow import workflow
from in_lockstep.middleware.approval import ApprovalGate
from in_lockstep.platform.artifacts import read_changeset, read_verdict, write_changeset
from in_lockstep.platform.report import implement_body
from in_lockstep.platform.scm import GitHubScm, Scm
from in_lockstep.platform.tickets import GitHubIssues, TicketSource
from in_lockstep.strategies import default_registry

# Bound here rather than constructed inside the workflows, so `in-lockstep ls` can print what
# will actually run and a test can substitute either one.
lockstep.bind(TicketSource, GitHubIssues())
lockstep.bind(Scm, GitHubScm())

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
lockstep.bind(
    Implement,
    AiImplement(
        invoker_factory(lockstep.models.routes.get("implement", "")),
        registry=default_registry(),
        repo_root=lockstep.repo.root,
        commands=Sandbox(image="docker.io/library/python:3.12-slim", require_container=True),
        policy=InvokePolicy.under(
            lockstep.policy.resolve(), max_turns=30, max_tokens=8192, deadline_seconds=1800
        ),
    ),
)

# Test runs after the change is staged — against a throwaway worktree of HEAD plus the change — and
# its verdict rides the artifact into the PR body, so a reviewer sees whether the change passed
# before opening it. The default Sandbox runs the suite in a subprocess with credentials dropped,
# enough that repository (and staged) test code cannot read the provider key out of this job. It
# does not cut network the way run_script's container does; a host that can enforce egress should
# pass `Sandbox(image=..., require_container=True)` here too — the same trade the note below draws
# for run_script. On a non-Python repo, swap `PytestTest()` for `CommandTest(["npm", "test"])`.
lockstep.bind(Test, PytestTest())

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

#: Where the unprivileged half leaves its answer for the privileged half to pick up.
CHANGESET = "changeset"


@workflow(id="implement/from-issue")
async def implement_from_issue(ctx: Any, issue: str) -> Outcome:
    """Read the issue, implement it, test the staged change, leave it in an artifact.

    Writes nothing to the tree. The change set — and the verdict of running the suite against it —
    travel to the job that holds a write token, and cross the guard again when they get there.
    """
    tickets: TicketSource = ctx.container.resolve(TicketSource)
    ticket = await tickets.get(issue)
    outcome = await ctx.do(Implement, ImplementSpec(ticket=ticket))

    report = outcome.value
    if report is not None and report.changeset.changes:
        # Run the suite against the staged change (in a throwaway worktree) before it travels, so
        # the reviewer sees a verdict on the PR rather than opening an untested change.
        verdict = await verdict_over_staged(ctx, lockstep.repo.root, report.changeset)
        written = write_changeset(CHANGESET, report.changeset, verdict=verdict)
        print(f"staged    {len(report.changeset.changes)} change(s) -> {written}")
    return outcome


@workflow(id="implement/propose")
async def implement_propose(ctx: Any, issue: str, artifact: str = CHANGESET) -> Outcome:
    """Open a change from a staged artifact, and say on the issue what happened.

    Runs in the job that holds a write token and no provider credential. Everything it reads
    came from another job, so none of it is trusted: `Scm.open_change` runs ChangeGuard over the
    set before it writes a byte, and refuses any branch outside the run-scoped prefix.
    """
    tickets: TicketSource = ctx.container.resolve(TicketSource)
    scm: Scm = ctx.container.resolve(Scm)
    changeset = read_changeset(artifact)
    verdict = read_verdict(artifact)

    if not changeset.changes:
        # Still a comment. "It found nothing to change" is an answer, and a trigger that answers
        # only on success leaves somebody watching a thread that never replies.
        await tickets.comment(await tickets.get(issue), "`/implement` staged no change.")
        return Outcome(status=Status.FAILED, reason="implement.no_changes")

    change = await scm.open_change(
        changeset,
        title=changeset.summary or f"Implement {issue}",
        body=implement_body(changeset, verdict),
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
'''


if __name__ == "__main__":  # pragma: no cover
    main()
