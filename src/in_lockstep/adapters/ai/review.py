"""Review, backed by a model.

Thin by design: it assembles context, runs one lens through the invoker, and maps the structured
answer back onto the verb's type. Everything interesting — the loop bounds, the tool allowlist,
the spend ceiling — belongs to the invoker, so a second AI verb does not re-implement any of it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from ...ai.context import ContextCurator, ContextItem, ContextNeed, ContextPackage, Provenance
from ...ai.invoker import AiInvoker, InvocationBlocked, InvocationFailed, InvokePolicy, ToolRunner
from ...ai.prompt import Composition, PromptLayers, compositions
from ...ai.structured import SchemaError, parse, schema_instruction, validate
from ...ai.tools import ToolSet
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.verbs import Capability, Verb
from ...privileged.egress import EgressRefused
from ...prompts.review import LENSES, REVIEW_SCHEMA, ReviewParams, ReviewPrompt, review_layers


@dataclass(frozen=True)
class ReviewFinding:
    path: str
    summary: str
    detail: str = ""
    line: int | None = None
    severity: str = "note"
    aspect: str = ""


@dataclass(frozen=True)
class ReviewReport:
    findings: tuple[ReviewFinding, ...] = ()
    verdict: str = ""
    aspect: str = ""

    @property
    def clean(self) -> bool:
        return not self.findings


@dataclass(frozen=True)
class Review:
    """The Review request. Workflows do `ctx.do(Review(...))`; a binding decides what runs it.

    Frozen like every request type: it is hashed for step identity and serialized into
    checkpoints, so a mutation after dispatch would change a key that had already been
    written down.
    """

    base: str
    head: str
    aspect: str = "security"
    paths: tuple[str, ...] = ()
    token_budget: int = 60_000
    # A diff supplied directly rather than read from git. The shipped cassette fixture needs one,
    # because a cassette is keyed on the whole composed prompt and therefore on the diff inside
    # it: a fixture that replayed against *any* diff would not be a replay of anything.
    diff: str = ""


class AiReview:
    verb: ClassVar[Verb] = Verb.REVIEW
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READS_REPO, Capability.SPENDS_BUDGET}
    )

    def __init__(
        self,
        invoker_factory: Callable[[Any], AiInvoker] | None = None,
        *,
        repo_root: str = "",
        policy: InvokePolicy | None = None,
        curator: ContextCurator | None = None,
        lenses: Mapping[str, type[ReviewPrompt]] | None = None,
        tools: ToolSet | None = None,
        run_tool: ToolRunner | None = None,
        layers: PromptLayers | None = None,
    ) -> None:
        # No invoker by default: the model comes from `lockstep.models.route(<verb>, ...)`,
        # resolved per run off the context. Passing one is the seam for a custom registry,
        # gateway, or cassette provider.
        self.invoker_factory = invoker_factory
        #: Empty defaults to the run's own repository (`ctx.repo.root`) at invoke time.
        self.repo_root = repo_root
        self.policy = policy or InvokePolicy(max_turns=1)
        self.curator = curator or ContextCurator()
        # The layer stack around every lens this adapter runs — a repository's own guardrails go
        # here, usually as `review_layers().plus(guardrails=...)` so the shipped baseline stays
        # underneath. Injected like `lenses=`: prompt text is data, and the binding site in
        # lockstep.py is where data enters, visibly.
        self.layers = layers
        # No tools by default, and that is the honest default rather than a gap: one turn with the
        # diff in the prompt is what this lens needs, and a tool set would make every review
        # multi-turn and multiply its cost. A repository that wants the reviewer to read files it
        # was not handed passes `builtins.read_only(Workspace(...))` — and must raise `max_turns`
        # with it, because a tool result the loop has no turn left to read is only expense.
        self.tools = tools
        self.run_tool = run_tool
        # `docs/extending.md` shows how to write a house prompt and, until this parameter, no way
        # to install one: there is no `bind_prompt`, and `invoke` read the module-global `LENSES`.
        # The only routes were mutating that global from a config file — a side effect on import,
        # in the file whose whole point is being inspectable — or overriding `invoke` wholesale.
        # Copied rather than aliased, so a later mutation of the global cannot reach a bound
        # adapter, and an adapter's lens map cannot leak back into the shipped one.
        self.lenses: Mapping[str, type[ReviewPrompt]] = dict(lenses) if lenses is not None else dict(LENSES)

    def compositions(self) -> dict[str, Composition]:
        """What `show-prompt` and `ls` read: this adapter's lenses, under their qualified labels.

        Declared here rather than discovered by the CLI, which would have to know that this
        adapter keeps its map in `lenses` and every other keeps one in `prompts`. Attribute
        sniffing across six classes is inference, and the failure mode is silence: a renamed
        attribute would make an override invisible again, which is the defect this method exists
        to close.
        """
        return compositions(
            self.lenses,
            self.layers if self.layers is not None else review_layers(),
            verb=str(type(self).verb),
            source=type(self).__name__,
        )

    async def invoke(self, ctx: Any, inp: Review) -> Outcome[ReviewReport]:
        lens = self.lenses.get(inp.aspect)
        if lens is None:
            return Outcome.blocked_by(
                "review.unknown_aspect",
                findings=(
                    Finding(
                        id="review.unknown_aspect",
                        message=f"no lens named {inp.aspect!r}; have {sorted(self.lenses)}",
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )

        prompt: ReviewPrompt = lens()
        layers: PromptLayers = self.layers if self.layers is not None else review_layers()
        root = self.repo_root or str(getattr(getattr(ctx, "repo", None), "root", "") or ".")
        package = self._gather(inp, root)

        if not package.items:
            # Refused rather than asked. A model handed no diff answers anyway, and whether that
            # answer parses decides between `review.unparseable` — which reads as a model problem
            # — and `{"findings": []}`, which reads as a clean review. Neither is true, and the
            # second is the one that would be believed.
            return Outcome.blocked_by(
                "review.no_content",
                findings=(
                    Finding(
                        id="review.no_content",
                        message=(
                            f"nothing to review between {inp.base} and {inp.head}: "
                            f"{', '.join(package.dropped) or 'the diff was empty'}. Nothing was "
                            f"sent and nothing was charged."
                        ),
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )

        system = prompt.system(layers) + "\n\n" + schema_instruction(REVIEW_SCHEMA)
        messages = prompt.render(ReviewParams(base=inp.base, head=inp.head, aspect=inp.aspect), package)

        invoker: AiInvoker = self._invoker(ctx)
        try:
            invocation = await invoker.run(
                system=system,
                messages=messages,
                context=package,
                tools=self.tools,
                run_tool=self.run_tool,
                policy=self.policy,
            )
        except InvocationBlocked as e:
            return Outcome.blocked_by(
                e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )
        except EgressRefused as e:
            # A control refusing is precisely what BLOCKED means, and routing it through an
            # Outcome rather than letting it escape is what gets it a ledger record: a run that
            # was refused for a real reason should leave the same trace as one that was allowed.
            return Outcome.blocked_by(
                e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )
        except InvocationFailed as e:
            # ERRORED, not BLOCKED: §4.3 reserves BLOCKED for a policy or gate refusing, and a
            # provider that could not be made to answer is infrastructure. Filing a broken
            # credential under the same heading as a budget ceiling would make both unreadable in
            # the ledger. The message arrives already redacted from the invoker.
            return Outcome(
                status=Status.ERRORED,
                reason=e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )

        if invocation.truncated:
            # Diagnosed before parsing, because the parse failure it causes is a misdiagnosis:
            # the JSON is not malformed, it is unfinished, and "the model returned bad JSON" sends
            # someone to look at the prompt when the answer is one number in the policy.
            return Outcome(
                status=Status.ERRORED,
                reason="review.truncated",
                cost=invocation.cost,
                findings=(
                    Finding(
                        id="review.truncated",
                        message=(
                            f"the model stopped at the {self.policy.max_tokens}-token output cap "
                            f"with its answer unfinished. Raise `InvokePolicy.max_tokens` for this "
                            f"lens; the cost estimate rises with it, so the budget may need to too."
                        ),
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )

        try:
            parsed = parse(invocation.content)
        except SchemaError as e:
            return Outcome(
                status=Status.ERRORED,
                reason="review.unparseable",
                cost=invocation.cost,
                findings=(
                    Finding(id="review.unparseable", message=str(e), severity=Severity.ERROR, blocking=True),
                ),
            )

        problems = validate(parsed.value, REVIEW_SCHEMA)
        if problems:
            return Outcome(
                status=Status.ERRORED,
                reason="review.schema_mismatch",
                cost=invocation.cost,
                findings=tuple(
                    Finding(id="review.schema_mismatch", message=p, severity=Severity.ERROR, blocking=True)
                    for p in problems
                ),
            )

        report = _to_report(parsed.value, inp.aspect)
        report, unreachable, unplaced = _in_the_change(report, package)
        findings = tuple(
            Finding(
                id=f"review.{inp.aspect}",
                message=f.summary,
                severity=Severity.WARNING,
                path=f.path,
                line=f.line,
                blocking=False,
            )
            for f in report.findings
        )
        # Counted, never silent. A lens that quietly drops half its output reads as a lens that
        # found half as much, and "the reviewer said little" is the reading a person acts on.
        if unreachable:
            shown = ", ".join(sorted(set(unreachable))[:_PATHS_NAMED])
            more = len(set(unreachable)) - _PATHS_NAMED
            findings += (
                Finding(
                    id="review.path_not_in_diff",
                    message=(
                        f"{len(unreachable)} finding(s) dropped: named {shown}"
                        f"{f' and {more} more' if more > 0 else ''}, which this change does not "
                        f"touch. A finding's path is the location a reviewer is sent to, and one "
                        f"the diff does not contain sends them nowhere."
                    ),
                    severity=Severity.WARNING,
                    blocking=False,
                ),
            )
        if unplaced:
            shown = ", ".join(sorted(set(unplaced))[:_PATHS_NAMED])
            findings += (
                Finding(
                    id="review.line_not_in_hunk",
                    message=(
                        f"{len(unplaced)} finding(s) kept without a line: {shown} points outside "
                        f"every hunk this change produced in that file. The claim may still be "
                        f"right, so it is reported; the coordinate is not, so it is not."
                    ),
                    severity=Severity.WARNING,
                    blocking=False,
                ),
            )
        # Anything the injection scanner saw in the diff travels with the outcome: a review of a
        # change that tried to talk to the reviewer is a fact about the change.
        injection_findings = tuple(
            Finding(
                id=f"injection.{f.name}",
                message=f"{f.severity}: {f.excerpt}",
                severity=Severity.ERROR if f.severity == "critical" else Severity.WARNING,
                blocking=False,
            )
            for f in invocation.findings
        )

        # What the reviewer was NOT shown, reported as a finding rather than left in a field
        # nobody reads. A review of part of a change is a real review of that part; a review that
        # does not say which part is one somebody will read as covering all of it — and until the
        # curator learned to shrink, an oversized diff was dropped whole and this verb asked a
        # model to review a change it had not been given.
        omitted = tuple(
            Finding(
                id="review.not_reviewed",
                message=f"not included in this review: {name}",
                severity=Severity.WARNING,
                path=name,
            )
            for name in package.dropped
        )

        return Outcome(
            status=Status.SUCCEEDED,
            value=report,
            findings=findings + injection_findings + omitted,
            cost=invocation.cost,
            decided=not invocation.exhausted,
            reason="exhausted" if invocation.exhausted else None,
        )

    def _invoker(self, ctx: Any) -> AiInvoker:
        from ...ai.bootstrap import routed_invoker

        factory = self.invoker_factory or routed_invoker(type(self).verb)
        return factory(ctx)

    def _gather(self, inp: Review, root: str) -> ContextPackage:
        diff = inp.diff or _git_diff(root, inp.base, inp.head, inp.paths)
        if not diff.strip():
            # An empty diff is not a clean review, and running one would produce a confident
            # answer about nothing. Distinguished here rather than at parse time, where it arrives
            # as "the model returned bad JSON" and sends somebody to read the prompt.
            return ContextPackage(items=(), dropped=("diff:(empty)",))
        items = [
            ContextItem(
                kind="diff",
                content=diff,
                # A diff is authored by whoever opened the change. Under review, that is exactly
                # the party the reviewer is checking.
                provenance=Provenance.UNTRUSTED_EXTERNAL,
                path=f"{inp.base}..{inp.head}",
            )
        ]
        return self.curator.curate(
            items, ContextNeed(base=inp.base, head=inp.head, token_budget=inp.token_budget)
        )


def _git_diff(root: str, base: str, head: str, paths: tuple[str, ...]) -> str:
    cmd = ["git", "diff", f"{base}...{head}"]
    if paths:
        cmd += ["--", *paths]
    try:
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return ""
    return result.stdout


#: How many unreachable paths the refusal names before saying "and N more". A refusal is a
#: pointer to a problem, not a transcript of it.
_PATHS_NAMED = 3


def _in_the_change(
    report: ReviewReport, package: ContextPackage
) -> tuple[ReviewReport, tuple[str, ...], tuple[str, ...]]:
    """Check each finding's coordinates against the change. Returns the report, the findings
    dropped for naming an untouched file, and the lines dropped for pointing outside every hunk.

    O7 asks which part of a verb is arithmetic wearing a prompt. This is that part: `path` arrived
    from the model and was compared against nothing, while the diff answering it was already in
    hand. It is not cosmetic: the path is the location column of the review comment, so a
    hallucinated one sends a reviewer to a file this change never touched — and it is the value
    any per-finding inline placement would be posted at, which is the sharper form of the same
    problem and the reason to fix it before that exists rather than after.

    Checked against the diff **as sent**, not against a fresh `git diff`. Three reasons, and the
    first is the one that matters: a model cannot name a file it was not shown, so the context is
    the tighter test. It also costs no subprocess, and it does not make this verb depend on a ref
    resolving — a review that reached the model and produced findings must not then fail on git.

    Per finding, because one unreachable path is not a reason to discard three real findings
    beside it.
    """
    from ...platform.scm import Diff

    touched: set[str] = set()
    hunks: dict[str, tuple[tuple[int, int], ...]] = {}
    for item in package.items:
        if item.kind == "diff":
            diff = Diff(text=item.content, base="", head="")
            touched |= set(diff.paths)
            hunks.update(diff.hunks)

    if not touched:
        # A diff with no `---`/`+++` lines at all — a pure mode change, or a binary file summarised
        # rather than shown. Nothing here can be checked, and refusing every finding on the
        # strength of a parse that found nothing would drop real output to enforce a rule this
        # input cannot answer. Fail open and leave the report alone: skipping the check drops
        # nothing, where applying it blindly would drop everything.
        return report, (), ()

    kept, unreachable, unplaced = [], [], []
    for finding in report.findings:
        path = _normalised(finding.path)
        if path not in touched:
            unreachable.append(finding.path or "(no path)")
            continue
        if _outside_every_hunk(path, finding.line, hunks):
            # The finding survives, the line does not. A model that noticed something real about
            # this file and pointed one line off is worth reading; a reviewer sent to a line the
            # change never touched is not. So the honest treatment is to keep the claim and drop
            # the coordinate, which is a different act from refusing the finding and is counted
            # under its own id.
            unplaced.append(f"{path}:{finding.line}")
            kept.append(replace(finding, line=None))
            continue
        kept.append(finding)
    return replace(report, findings=tuple(kept)), tuple(unreachable), tuple(unplaced)


def _outside_every_hunk(path: str, line: int | None, hunks: dict[str, tuple[tuple[int, int], ...]]) -> bool:
    """Whether `line` points outside every range this change produced in `path`.

    False for a finding with no line, which claims no coordinate and so cannot be wrong about one.
    False for a path with no hunk entry at all — a deleted file, or a diff whose header this could
    not parse — because *no new side* is not the same fact as *this line is wrong*, and treating
    them alike would strip the line off every finding on a deleted file to enforce a rule that
    input cannot answer.
    """
    if line is None:
        return False
    spans = hunks.get(path)
    if not spans:
        return False
    return not any(start <= line <= end for start, end in spans)


def _normalised(path: str) -> str:
    """A model's spelling of a path, as the diff would spell it.

    Deliberately minimal: whitespace and a leading `./`, and nothing more. Stripping a leading
    `a/` or `b/` was tempting and is wrong — `a/` is a legal directory name, and a rule that
    rewrites a real path to make it match is the guessing O1 refuses in the neighbouring case.
    Anything else that does not match exactly is refused **and counted**, so a systematic
    mismatch shows up as a number rather than as a lens that went quiet.
    """
    cleaned = path.strip()
    return cleaned[2:] if cleaned.startswith("./") else cleaned


def _to_report(value: object, aspect: str) -> ReviewReport:
    data = value if isinstance(value, dict) else {}
    findings = []
    for raw in data.get("findings", []) or []:
        if not isinstance(raw, dict):
            continue
        findings.append(
            ReviewFinding(
                path=str(raw.get("path", "")),
                summary=str(raw.get("summary", "")),
                detail=str(raw.get("detail", "")),
                line=raw.get("line") if isinstance(raw.get("line"), int) else None,
                severity=str(raw.get("severity", "note")),
                aspect=aspect,
            )
        )
    return ReviewReport(findings=tuple(findings), verdict=str(data.get("verdict", "")), aspect=aspect)
