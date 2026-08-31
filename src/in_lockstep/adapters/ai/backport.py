"""Conflict resolution, backed by a model.

Thin like `triage.py`, and for the same reason: one turn, no tools, structured output. Everything
the invoker owns — the loop bound, the spend ceiling, the injection scan, egress — is not
re-implemented here. What is specific to backport is the containment rule, enforced in code rather
than only asked for in the prompt: a resolution may touch the conflicted paths and nothing else,
because a model allowed to "resolve" its way into other files is implementing, and implementing
has its own verb with its own gates.

This is a `ConflictResolver`, not a verb adapter: `GitBackport` owns the verb and calls this only
at the one moment git could not finish. The conflicted files and the commit's patch are repository
history — reviewed, merged code — so they travel as `TRUSTED_REPO`; the injection scan still runs
over them, because provenance is a label, not a pass.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from ...ai.context import ContextItem, ContextPackage, Provenance
from ...ai.invoker import AiInvoker, InvocationBlocked, InvocationFailed, InvokePolicy
from ...ai.prompt import Composition, PromptLayers, compositions
from ...ai.structured import SchemaError, parse, schema_instruction, validate
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.types import FileChange
from ...core.verbs import Verb
from ...privileged.egress import EgressRefused
from ...prompts.backport import (
    BACKPORT_PROMPTS,
    BACKPORT_SCHEMA,
    BackportParams,
    BackportPrompt,
    backport_layers,
)
from ..backport import Conflict

DEFAULT_MAX_TOKENS = 16_384


class AiBackportResolver:
    verb: ClassVar[Verb] = Verb.BACKPORT

    def __init__(
        self,
        invoker_factory: Callable[[Any], AiInvoker] | None = None,
        *,
        policy: InvokePolicy | None = None,
        prompts: Mapping[str, type[BackportPrompt]] | None = None,
        prompt_id: str = "backport/conflict-resolver",
        layers: PromptLayers | None = None,
    ) -> None:
        # No invoker by default: the model comes from `lockstep.models.route(<verb>, ...)`,
        # resolved per run off the context. Passing one is the seam for a custom registry,
        # gateway, or cassette provider.
        self.invoker_factory = invoker_factory
        # One turn: the resolver is handed everything it may see, so a second turn has no tool
        # result to react to. `max_tokens` is the large number here, not the turn count — the
        # answer is whole files.
        self.policy = policy or InvokePolicy(max_turns=1, max_tokens=DEFAULT_MAX_TOKENS)
        self.prompts: Mapping[str, type[BackportPrompt]] = (
            dict(prompts) if prompts is not None else dict(BACKPORT_PROMPTS)
        )
        self.prompt_id = prompt_id
        # Injected like every other adapter's — usually `backport_layers().plus(guardrails=...)`
        # so the shipped baseline stays underneath.
        self.layers = layers

    def compositions(self) -> dict[str, Composition]:
        """This adapter's prompts, for `show-prompt` and `ls`. See `AiReview.compositions`."""
        return compositions(
            self.prompts,
            self.layers if self.layers is not None else backport_layers(),
            verb=str(type(self).verb),
            source=type(self).__name__,
        )

    async def resolve(self, ctx: Any, conflict: Conflict) -> Outcome[tuple[FileChange, ...]]:
        lens = self.prompts.get(self.prompt_id)
        if lens is None:
            return _blocked(
                "backport.unknown_prompt",
                f"no backport prompt named {self.prompt_id!r}; have {sorted(self.prompts)}",
            )

        prompt: BackportPrompt = lens()
        layers: PromptLayers = self.layers if self.layers is not None else backport_layers()
        package = _package(conflict)
        system = prompt.system(layers) + "\n\n" + schema_instruction(BACKPORT_SCHEMA)
        params = BackportParams(commit=conflict.commit, subject=conflict.subject, paths=conflict.paths)

        from ...ai.bootstrap import routed_invoker

        factory = self.invoker_factory or routed_invoker(type(self).verb)
        invoker: AiInvoker = factory(ctx)
        try:
            invocation = await invoker.run(
                system=system,
                messages=prompt.render(params, package),
                context=package,
                policy=self.policy,
            )
        except InvocationBlocked as e:
            return _blocked(e.reason, str(e))
        except EgressRefused as e:
            return _blocked(e.reason, str(e))
        except InvocationFailed as e:
            return _errored(e.reason, str(e), None)

        if invocation.truncated:
            return _errored(
                "backport.truncated",
                f"the model stopped at the {self.policy.max_tokens}-token output cap with the "
                f"merged files unfinished. Raise `InvokePolicy.max_tokens`.",
                invocation.cost,
            )

        try:
            parsed = parse(invocation.content)
        except SchemaError as e:
            return _errored("backport.unparseable", str(e), invocation.cost)
        problems = validate(parsed.value, BACKPORT_SCHEMA)
        if problems:
            return Outcome(
                status=Status.ERRORED,
                reason="backport.schema_mismatch",
                cost=invocation.cost,
                findings=tuple(
                    Finding(id="backport.schema_mismatch", message=p, severity=Severity.ERROR, blocking=True)
                    for p in problems
                ),
            )

        data = parsed.value if isinstance(parsed.value, dict) else {}
        listed = data.get("files")
        raw_files = listed if isinstance(listed, list) else []
        files = tuple(
            FileChange(path=str(f.get("path", "")), contents=str(f.get("contents", "")))
            for f in raw_files
            if isinstance(f, dict)
        )
        # The containment rule, enforced rather than trusted: only the conflicted paths. The
        # deterministic half re-checks this before writing — a rule enforced at one point is
        # enforced at none — but refusing here attributes the refusal to the answer that earned it.
        strays = sorted({f.path for f in files} - set(conflict.paths))
        if strays:
            return Outcome(
                status=Status.FAILED,
                reason="backport.resolution_out_of_scope",
                cost=invocation.cost,
                findings=tuple(
                    Finding(
                        id="backport.resolution_out_of_scope",
                        message=f"the resolution writes {path!r}, which did not conflict.",
                        severity=Severity.ERROR,
                        path=path,
                        blocking=True,
                    )
                    for path in strays
                ),
            )
        if not files:
            return Outcome(
                status=Status.FAILED,
                reason="backport.empty_resolution",
                cost=invocation.cost,
                findings=(
                    Finding(
                        id="backport.empty_resolution",
                        message="the model returned no merged files, so the conflict stands.",
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )

        noted = data.get("notes")
        notes = noted if isinstance(noted, list) else []
        findings = tuple(
            Finding(id="backport.resolution_note", message=str(n), severity=Severity.WARNING) for n in notes
        ) + tuple(
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
            value=files,
            cost=invocation.cost,
            findings=findings,
            decided=not invocation.exhausted,
            reason="exhausted" if invocation.exhausted else None,
        )


def _package(conflict: Conflict) -> ContextPackage:
    """The commit being replayed, then each conflicted file with its markers."""
    items = [
        ContextItem(
            kind="diff",
            content=f"The commit being cherry-picked ({conflict.subject}):\n\n{conflict.patch}",
            provenance=Provenance.TRUSTED_REPO,
            path=conflict.commit,
        )
    ]
    for change in conflict.files:
        items.append(
            ContextItem(
                kind="file",
                content=(
                    change.contents
                    if change.contents is not None
                    else "(deleted on one side of the conflict)"
                ),
                provenance=Provenance.TRUSTED_REPO,
                path=change.path,
            )
        )
    return ContextPackage(items=tuple(items))


def _blocked(reason: str, message: str) -> Outcome[tuple[FileChange, ...]]:
    return Outcome.blocked_by(
        reason,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


def _errored(reason: str, message: str, cost: Any) -> Outcome[tuple[FileChange, ...]]:
    from ...core.outcome import Cost

    return Outcome(
        status=Status.ERRORED,
        reason=reason,
        cost=cost if cost is not None else Cost(),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
