"""Review, backed by a model.

Thin by design: it assembles context, runs one lens through the invoker, and maps the structured
answer back onto the verb's type. Everything interesting — the loop bounds, the tool allowlist,
the spend ceiling — belongs to the invoker, so a second AI verb does not re-implement any of it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ...ai.context import ContextCurator, ContextItem, ContextNeed, ContextPackage, Provenance
from ...ai.invoker import AiInvoker, InvocationBlocked, InvocationFailed, InvokePolicy
from ...ai.prompt import PromptLayers
from ...ai.structured import SchemaError, parse, schema_instruction, validate
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.verbs import Capability, Verb
from ...prompts.review import LENSES, REVIEW_SCHEMA, ReviewParams, ReviewPrompt, review_layers


class Review:
    """The verb interface."""


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


@dataclass
class ReviewSpec:
    base: str
    head: str
    aspect: str = "security"
    paths: tuple[str, ...] = ()
    token_budget: int = 60_000


class AiReview:
    verb: ClassVar[Verb] = Verb.REVIEW
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READS_REPO, Capability.SPENDS_BUDGET}
    )

    def __init__(
        self,
        invoker_factory: Callable[[Any], AiInvoker],
        *,
        repo_root: str = ".",
        policy: InvokePolicy | None = None,
        curator: ContextCurator | None = None,
        lenses: Mapping[str, type[ReviewPrompt]] | None = None,
    ) -> None:
        self.invoker_factory = invoker_factory
        self.repo_root = repo_root
        self.policy = policy or InvokePolicy(max_turns=1)
        self.curator = curator or ContextCurator()
        # `docs/extending.md` shows how to write a house prompt and, until this parameter, no way
        # to install one: there is no `bind_prompt`, and `invoke` read the module-global `LENSES`.
        # The only routes were mutating that global from a config file — a side effect on import,
        # in the file whose whole point is being inspectable — or overriding `invoke` wholesale.
        # Copied rather than aliased, so a later mutation of the global cannot reach a bound
        # adapter, and an adapter's lens map cannot leak back into the shipped one.
        self.lenses: Mapping[str, type[ReviewPrompt]] = dict(lenses) if lenses is not None else dict(LENSES)

    async def invoke(self, ctx: Any, inp: ReviewSpec) -> Outcome[ReviewReport]:
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
        layers: PromptLayers = review_layers()
        package = self._gather(inp)

        system = prompt.system(layers) + "\n\n" + schema_instruction(REVIEW_SCHEMA)
        messages = prompt.render(ReviewParams(base=inp.base, head=inp.head, aspect=inp.aspect), package)

        invoker: AiInvoker = self.invoker_factory(ctx)
        try:
            invocation = await invoker.run(
                system=system, messages=messages, context=package, policy=self.policy
            )
        except InvocationBlocked as e:
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

        return Outcome(
            status=Status.SUCCEEDED,
            value=report,
            findings=findings + injection_findings,
            cost=invocation.cost,
            decided=not invocation.exhausted,
            reason="exhausted" if invocation.exhausted else None,
        )

    def _gather(self, inp: ReviewSpec) -> ContextPackage:
        diff = _git_diff(self.repo_root, inp.base, inp.head, inp.paths)
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
