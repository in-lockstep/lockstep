"""Quality and readiness checks.

Two questions, deliberately separated. `lint` asks whether the *spec* is well built — is every agent
evaluated, every script tested, is AI being spent on work a script should do. `doctor` asks whether
the *target* will accept it — are the secrets declared, the refs pinned, the permissions minimal.

A spec can be excellent and still un-deployable, and vice versa; conflating them makes both easier
to ignore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import __version__
from .emit.agentic import ENGINE_BY_PROVIDER, UNMAPPED_PROVIDERS
from .emit.context import PINS_PATH, Pins
from .emit.validate import MAX_JOB_MINUTES
from .errors import LockstepError
from .spec.model import LOCKSTEP_DIR, CapabilityUse, Spec, StepKind

# Work an agent should never be doing: deterministic transformations cost tokens, vary run to run,
# and are the exact thing a script does better.
DETERMINISTIC_WORK = (
    "sort",
    "filter",
    "deduplicate",
    "dedupe",
    "convert format",
    "parse json",
    "validate schema",
)


# Words that make a sentence binding. Their presence is what separates a rule from a method.
NORMATIVE = ("MUST", "MUST NOT", "NEVER", "SHALL", "REQUIRED:")


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str
    location: str = ""
    hint: str = ""

    def render(self) -> str:
        head = f"{self.severity.value}: {self.code}: "
        head += f"{self.location} — {self.message}" if self.location else self.message
        if self.hint:
            head += f"\n       {self.hint}"
        return head


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: Severity, code: str, message: str, *, location: str = "", hint: str = "") -> None:
        self.findings.append(Finding(severity, code, message, location, hint))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        if not self.findings:
            return "  no findings"
        return "\n".join(f"  {finding.render()}" for finding in self.findings)


# --- lint: is the spec well built? -----------------------------------------


def lint(spec: Spec) -> Report:
    report = Report()
    _check_agents_have_evals(spec, report)
    _check_scripts_have_tests(spec, report)
    _check_deterministic_first(spec, report)
    _check_foreach_context(spec, report)
    _check_layer_boundaries(spec, report)
    return report


def _check_agents_have_evals(spec: Spec, report: Report) -> None:
    from .spec.model import INHERITED_DIR

    for name, agent in sorted(spec.agents.items()):
        # An inherited agent is evalled by whoever published it — the cases travel with the agent,
        # which is the only place they can be written against the prompt they are testing. A
        # consumer that had to write them would be testing somebody else's lens from the outside.
        if agent.inherited_from:
            alias = agent.inherited_from
            local = name.removeprefix(f"{alias}/")
            cases = spec.home / INHERITED_DIR / alias / "evals" / local / "cases"
            where = f"{alias}: evals/{local}/cases/"
        else:
            cases = spec.home / "evals" / name / "cases"
            where = f"evals/{name}/cases/"

        if cases.is_dir():
            _check_case_shape(cases, where, report)

        if not cases.is_dir() or not any(cases.glob("*.json")):
            report.add(
                Severity.ERROR,
                "LNT001",
                f"agent {name!r} has no eval cases",
                location=agent.src.rel if agent.src else name,
                hint=f"add cases under {where} — an agent without evals cannot be "
                "changed safely, and the eval gate has nothing to gate on",
            )


# What an eval case may assert. Declared here rather than imported: the compiler must not depend on
# the runtime, so `tests/test_contract.py` holds the two copies to each other instead.
EXPECT_KEYS = ("schema", "equals", "contains", "absent", "count", "rubric")


def _check_case_shape(cases: Path, where: str, report: Report) -> None:
    """A case that asserts nothing passed before anybody wrote it.

    `lockstep lint` used to check that a file existed. A file existing is not an eval, and the shape
    of the thing inside it is the difference between a suite that can fail and a directory listing.
    """
    import json

    for path in sorted(cases.glob("*.json")):
        location = f"{where}{path.name}"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            report.add(
                Severity.ERROR,
                "LNT007",
                f"eval case is not valid JSON ({error.msg} at line {error.lineno})",
                location=location,
            )
            continue
        if not isinstance(raw, dict) or "input" not in raw:
            report.add(
                Severity.ERROR,
                "LNT007",
                "eval case has no `input` — a case has to say what the agent was given",
                location=location,
            )
            continue

        expect = raw.get("expect")
        expect = expect if isinstance(expect, dict) else {}
        unknown = sorted(set(expect) - set(EXPECT_KEYS))
        if unknown:
            report.add(
                Severity.ERROR,
                "LNT008",
                f"eval case declares unknown expectation(s): {', '.join(unknown)}",
                location=location,
                hint=f"known expectations: {', '.join(EXPECT_KEYS)}. An unrecognised key is not a "
                "stricter case, it is one that never runs",
            )
        if not set(expect) & set(EXPECT_KEYS):
            report.add(
                Severity.ERROR,
                "LNT008",
                "eval case asserts nothing, so it passes for any output",
                location=location,
                hint=f"add at least one of: {', '.join(EXPECT_KEYS)}. `rubric` is prose judged by a "
                "model; the rest are checked by `pipeline-exec eval-grade` and mean the same thing "
                "on every run",
            )


def _check_scripts_have_tests(spec: Spec, report: Report) -> None:
    from .spec.model import INHERITED_DIR

    def _suite_path(target: str) -> str:
        if target.startswith(f"{INHERITED_DIR}/"):
            alias = Path(target).relative_to(INHERITED_DIR).parts[0]
            return f"{alias}: tests"
        return "tests"

    def suite_for(target: str) -> set[str]:
        """Where a script's tests live: beside it, wherever it was published."""
        directory = spec.home / "tests"
        if target.startswith(f"{INHERITED_DIR}/"):
            alias = Path(target).relative_to(INHERITED_DIR).parts[0]
            directory = spec.home / INHERITED_DIR / alias / "tests"
        return {p.name for p in directory.rglob("test_*.py")} if directory.is_dir() else set()

    seen: set[str] = set()
    for command in spec.commands.values():
        for step in command.steps:
            if step.kind is not StepKind.SCRIPT or step.target in seen:
                continue
            seen.add(step.target)
            stem = Path(step.target).stem.replace("-", "_")
            if f"test_{stem}.py" not in suite_for(step.target):
                report.add(
                    Severity.WARNING,
                    "LNT002",
                    f"script {step.target!r} has no unit test",
                    location=command.src.rel if command.src else command.name,
                    hint=f"add {_suite_path(step.target)}/test_{stem}.py — script steps run on "
                    "every execution, so a regression here is silent and permanent",
                )


def _check_deterministic_first(spec: Spec, report: Report) -> None:
    for name, agent in sorted(spec.agents.items()):
        body = agent.body.lower()
        matched = [phrase for phrase in DETERMINISTIC_WORK if phrase in body]
        if matched and agent.max_tool_turns == 0:
            report.add(
                Severity.WARNING,
                "LNT003",
                f"agent {name!r} appears to do deterministic work ({', '.join(matched)})",
                location=agent.src.rel if agent.src else name,
                hint="AI decides what to do; scripts do it. Deterministic transformations belong in "
                "a script step, which costs nothing and cannot vary between runs",
            )


def _check_foreach_context(spec: Spec, report: Report) -> None:
    for command in spec.commands.values():
        for step in command.steps:
            if step.kind is StepKind.AGENT and step.foreach and not step.parallel:
                report.add(
                    Severity.WARNING,
                    "LNT004",
                    f"foreach step {step.label!r} runs one item at a time",
                    location=f"{command.src.rel if command.src else command.name} step {step.number}",
                    hint="set `parallel:` — matrix legs are independent, and serialising them costs "
                    "wall-clock for nothing",
                )


def _check_layer_boundaries(spec: Spec, report: Report) -> None:
    """Each prompt layer answers one question. Text answering a different one belongs elsewhere.

    A rule stated in a skill and again in a guardrail is one rule in two places, and the copy nobody
    enforces wins by being the one somebody read. A rule stated *only* in a skill or a context is
    worse: it looks binding and nothing inlines it first or compiles it to a permission.
    """
    for kind, fragments in (("skill", spec.skills), ("context", spec.contexts)):
        for name, fragment in sorted(fragments.items()):
            found = sorted({word for word in NORMATIVE if word in fragment.body})
            if not found:
                continue
            report.add(
                Severity.WARNING,
                "LNT005",
                f"{kind} {name!r} states a rule ({', '.join(found)})",
                location=fragment.src.rel if fragment.src else name,
                hint=f"a {kind} answers "
                + ("how the job is done" if kind == "skill" else "what the subject is")
                + "; move the constraint to a guardrail, where it is inlined ahead of the agent "
                "body and can carry an `enforce:` block. See docs/layers.md",
            )

    for name, fragment in sorted(spec.skills.items()):
        named = sorted({term for term in _product_terms(spec) if term in fragment.body})
        if named:
            report.add(
                Severity.WARNING,
                "LNT006",
                f"skill {name!r} names something only the target has ({', '.join(named)})",
                location=fragment.src.rel if fragment.src else name,
                hint="a skill should read the same against a different application; anything true "
                "of only this one belongs in a context, which the profile selects. See docs/layers.md",
            )


def _product_terms(spec: Spec) -> set[str]:
    """Words the pipeline's own contexts use to name the target, which a skill should not repeat."""
    terms: set[str] = set()
    for fragment in spec.contexts.values():
        terms.update(re.findall(r"`(/[a-z0-9][a-z0-9/_-]*)`", fragment.body))
    return terms


# --- doctor: will the target accept it? ------------------------------------


def doctor(spec: Spec, root: Path) -> Report:
    report = Report()
    pins = Pins.load(spec)
    _check_pins(pins, spec.capabilities_used(), spec.external_actions_used(), report)
    _check_inherits(spec, root, report)
    _check_missing_upstreams(spec, report)
    _check_runtime_compiler(spec, report)
    _check_engines(spec, report)
    _check_budgets(spec, report)
    _check_secrets(spec, report)
    _check_mcp_allowlists(spec, report)
    _check_timeouts(spec, report)
    _check_extensions(spec, report)
    return report


def _check_pins(pins: Pins, used: CapabilityUse, external_used: set[str], report: Report) -> None:
    if used.actions and not pins.actions_sha:
        report.add(
            Severity.ERROR,
            "DOC001",
            "capability actions are not pinned to a commit",
            hint="run `lockstep pin` — a floating tag can be moved under a pipeline that already "
            "passed review",
        )
    if used.executor and not pins.exec_digest:
        report.add(
            Severity.ERROR,
            "DOC002",
            "the executor image is not pinned by digest",
            hint="run `lockstep pin`, or record capabilities.exec.digest in .pipeline/pins.lock",
        )
    for action in sorted(external_used):
        if not pins.external.get(action):
            report.add(
                Severity.ERROR,
                "DOC012",
                f"external action {action!r} is not pinned",
                hint="run `lockstep pin` — a third-party action left on a tag can be replaced under "
                "a pipeline that already passed review",
            )
    for placeholder in pins.placeholders():
        report.add(
            Severity.ERROR,
            "DOC015",
            f"{placeholder} is pinned to a placeholder, not to anything that exists",
            hint="a zero pin has the shape of a pin and none of the value: the workflow it compiles "
            "into references a commit or digest that was never published. Publish the capability, "
            "then `lockstep pin` — or `lockstep pin --sha/--exec-digest` if you resolved it yourself",
        )
    if used.executor and not pins.exec_image:
        report.add(
            Severity.ERROR,
            "DOC016",
            "no executor image",
            hint="set capabilities.exec-image in pipeline.yaml — any registry works, e.g. "
            "`quay.io/<owner>/pipeline-exec` or `ghcr.io/<owner>/pipeline-exec`",
        )
    from .emit.context import is_local_requirement

    if pins.compiler_requirement and not is_local_requirement(pins.compiler_requirement):
        if not pins.compiler_version:
            report.add(
                Severity.WARNING,
                "DOC023",
                f"the compiler is a range ({pins.compiler_requirement}), not a pinned version",
                hint="run `lockstep pin` — everything else this pipeline runs is pinned to something "
                "immutable, and then the check that enforces that installs its own compiler from "
                "whatever the index offers that day",
            )
        elif pins.compiler_version != __version__:
            report.add(
                Severity.WARNING,
                "DOC024",
                f"pinned to compiler {pins.compiler_version}, but {__version__} is compiling",
                hint="the committed output was produced by one and is about to be checked by the "
                "other. Run `lockstep pin` to adopt this version deliberately, or install "
                f"{pins.compiler_version} to match what is committed",
            )

    if used.gh_aw and not pins.gh_aw_version:
        report.add(
            Severity.WARNING,
            "DOC003",
            "gh-aw is not pinned",
            hint="set capabilities.gh-aw in pipeline.yaml so lock files stay reproducible",
        )


def _check_runtime_compiler(spec: Spec, report: Report) -> None:
    """A step that installs the compiler can regenerate this repository's own output.

    There is one legitimate reason to want that — re-pinning an upstream and proposing the recompile
    — and it is legitimate only because the result goes through a pull request. A pipeline that
    committed its own recompile would be a pipeline whose reviewed output stopped being the artifact
    that runs, so this is surfaced for a human rather than judged by the compiler.
    """
    for command in spec.commands.values():
        steps = [step for step in command.steps if step.uses_compiler]
        if not steps:
            continue
        location = command.src.rel if command.src else command.name
        report.add(
            Severity.WARNING,
            "DOC020",
            f"command {command.name!r} runs the compiler at runtime "
            f"({', '.join(step.id or step.label for step in steps)})",
            location=location,
            hint="the only intended use is proposing a recompile after an upstream moved. Check "
            "that its output reaches a pull request rather than a branch anybody merges from",
        )
        if command.github.propose is None:
            report.add(
                Severity.ERROR,
                "DOC021",
                f"command {command.name!r} recompiles but proposes nothing",
                location=location,
                hint="add a `propose:` block — a recompile that does not become a reviewable pull "
                "request either does nothing or does something nobody reviewed",
            )


def _check_inherits(spec: Spec, root: Path, report: Report) -> None:
    """An inherited pipeline has to be pinned, or the drift gate is comparing against a moving target."""
    import json

    from .spec.model import LOCKSTEP_DIR

    home = root / LOCKSTEP_DIR if spec.in_lockstep_dir else root
    path = home / PINS_PATH
    locked: dict[str, Any] = {}
    if path.is_file():
        try:
            locked = json.loads(path.read_text(encoding="utf-8")).get("inherits") or {}
        except json.JSONDecodeError:
            locked = {}

    for alias, source in sorted(spec.manifest.inherits.items()):
        if not source.startswith("github.com/"):
            report.add(
                Severity.WARNING,
                "DOC017",
                f"{alias!r} is inherited from a local path ({source})",
                hint="a path cannot be pinned, so nobody else can reproduce this build. Fine while "
                "developing an upstream and a consumer side by side; not fine on a default branch",
            )
        elif not (locked.get(alias) or {}).get("sha"):
            report.add(
                Severity.ERROR,
                "DOC018",
                f"{alias!r} is inherited from {source} but is not pinned to a commit",
                hint="run `lockstep pin` — an unpinned upstream can change what this pipeline runs "
                "without anything in this repository changing",
            )


def _upstream_key(source: str) -> str:
    """Identity of an upstream, ignoring the ref it is pinned at.

    A consumer on `@v3.1.0` and an upstream on `@v3.2.0` name the same standards repository, and the
    question here is only whether the consumer has it at all.
    """
    base = source.partition("@")[0].rstrip("/")
    if base.startswith("github.com/"):
        return "/".join(base.removeprefix("github.com/").split("/")[:2]).lower()
    return Path(base).name.lower()


def _check_missing_upstreams(spec: Spec, report: Report) -> None:
    """An upstream this repository inherits, inherits something this repository does not.

    This is the cost of refusing transitive inheritance, and it fails in the quiet direction. A team
    publishes pipelines under an organization's sealed standards; a component repository inherits the
    team and forgets the organization. It compiles, lints and doctors clean while standing on none of
    those standards — the team's agents arrive with the organization's guardrails stripped out,
    because the team's inheritance was never the consumer's.

    A warning rather than an error: nothing here can tell a repository that forgot from one that
    declined, and only the repository knows which it did.
    """
    from .spec.load import load_manifest_only
    from .spec.model import INHERITED_DIR

    mine = {_upstream_key(source) for source in spec.manifest.inherits.values()}
    for alias in spec.manifest.inherits:
        directory = spec.home / INHERITED_DIR / alias
        if not (directory / "pipeline.yaml").is_file() and not (directory / LOCKSTEP_DIR).is_dir():
            continue  # not fetched — `lockstep fetch` is a different, louder failure
        try:
            upstream = load_manifest_only(directory)
        except LockstepError:
            continue  # an upstream whose manifest will not parse is its own error, reported elsewhere
        for their_alias, source in sorted(upstream.manifest.inherits.items()):
            if _upstream_key(source) in mine:
                continue
            report.add(
                Severity.WARNING,
                "DOC022",
                f"{alias!r} inherits {their_alias!r} ({source}), which this repository does not",
                location=spec.repo_path("pipeline.yaml"),
                hint=f"anything sealed in {source} does not reach this repository through {alias!r} "
                f"— inheritance is not transitive. Add `{their_alias}: {source}` to `inherits:` to "
                f"stand on it too, or ignore this if {alias!r} is deliberately the only standard here",
            )


def _check_engines(spec: Spec, report: Report) -> None:
    for name, agent in sorted(spec.agents.items()):
        provider = agent.provider or "vertex-claude"
        if agent.github.engine:
            continue
        if provider in UNMAPPED_PROVIDERS:
            report.add(
                Severity.ERROR,
                "DOC004",
                f"agent {name!r} uses provider {provider!r}, which has no engine on this target",
                location=agent.src.rel if agent.src else name,
                hint="this agent can only run on the local backend; set github.engine to compile it",
            )
        elif provider not in ENGINE_BY_PROVIDER:
            report.add(
                Severity.ERROR,
                "DOC005",
                f"agent {name!r} uses unknown provider {provider!r}",
                location=agent.src.rel if agent.src else name,
                hint=f"known providers: {', '.join(sorted(ENGINE_BY_PROVIDER))}",
            )


def _check_budgets(spec: Spec, report: Report) -> None:
    for name, agent in sorted(spec.agents.items()):
        if agent.github.max_ai_credits is None:
            report.add(
                Severity.ERROR,
                "DOC006",
                f"agent {name!r} has no credit budget",
                location=agent.src.rel if agent.src else name,
                hint="set github.max-ai-credits — an unbounded agent on a schedule is an unbounded bill",
            )
    if spec.manifest.per_run_ai_credits is None:
        if not spec.agents:
            # A run budget bounds what the models may spend. With no agent to spend it, asking for
            # one is a number nothing reads.
            return
        report.add(
            Severity.WARNING,
            "DOC007",
            "no per-run credit budget",
            hint="set budgets.per_run_ai_credits in pipeline.yaml so a runaway run fails loudly",
        )
        return

    # A band lets a consumer raise one agent; the run budget is the ceiling over all of them. Raising
    # past it is a run that fails partway through, which is the most expensive way to find out.
    for command in spec.commands.values():
        agents = {step.target for step in command.steps if step.kind is StepKind.AGENT}
        worst = sum(spec.agents[name].github.max_ai_credits or 0 for name in agents if name in spec.agents)
        if worst > spec.manifest.per_run_ai_credits:
            tuned = sorted(name for name in agents if name in spec.agents and spec.agents[name].tuned)
            report.add(
                Severity.ERROR,
                "DOC019",
                f"command {command.name!r} can spend {worst} credits, over the run budget of "
                f"{spec.manifest.per_run_ai_credits}",
                location=command.src.rel if command.src else command.name,
                hint=(
                    f"tuned here: {', '.join(tuned)}. Lower it, or raise budgets.per_run_ai_credits"
                    if tuned
                    else "lower an agent's max-ai-credits, or raise budgets.per_run_ai_credits"
                ),
            )


def _check_secrets(spec: Spec, report: Report) -> None:
    from .emit.profiles import ENV_REF

    for name, profile in sorted(spec.profiles.items()):
        declared = set(profile.github.secrets) | set(profile.github.vars)
        for key, raw in profile.values.items():
            match = ENV_REF.match(raw.strip())
            if match and match.group(1) not in declared:
                report.add(
                    Severity.ERROR,
                    "DOC008",
                    f"profile {name!r} reads {match.group(1)!r} for {key!r}, which it does not declare",
                    location=profile.src.rel if profile.src else name,
                    hint="add it to github.secrets or github.vars; the compiler will not guess where "
                    "a credential lives",
                )
        if profile.github.secrets and not profile.github.environment:
            report.add(
                Severity.WARNING,
                "DOC009",
                f"profile {name!r} uses secrets but declares no environment",
                location=profile.src.rel if profile.src else name,
                hint="a GitHub Environment scopes the secrets and can require approval before a run "
                "touches production",
            )


def _check_mcp_allowlists(spec: Spec, report: Report) -> None:
    for name, server in sorted(spec.mcp_servers.items()):
        if not server.tools:
            report.add(
                Severity.ERROR,
                "DOC010",
                f"MCP server {name!r} declares no tools",
                location="mcp/servers.json",
                hint="list its tools; the compiler turns that list into the gateway allow-list, and "
                "an empty list means the agent gets whatever the server offers",
            )


def _check_extensions(spec: Spec, report: Report) -> None:
    """An extension builtin is taken on trust; say so, and say how to verify it."""
    extensions = spec.manifest.extensions
    if extensions.builtins and not extensions.packages:
        report.add(
            Severity.ERROR,
            "DOC013",
            f"{len(extensions.builtins)} extension builtin(s) declared but no package provides them",
            hint="list the distributions under `extensions.packages` so a generated repository "
            "installs them; otherwise the workflow will fail with `No such command`",
        )
    if extensions.builtins:
        report.add(
            Severity.WARNING,
            "DOC014",
            f"extension builtins are not verifiable here: {', '.join(sorted(extensions.builtins))}",
            hint="run `pipeline-exec list-commands` in CI with the extension installed to prove "
            "they exist before a scheduled run finds out they do not",
        )


def _check_timeouts(spec: Spec, report: Report) -> None:
    for name, command in sorted(spec.commands.items()):
        timeout = command.github.timeout_minutes
        if timeout and timeout > MAX_JOB_MINUTES:
            report.add(
                Severity.ERROR,
                "DOC011",
                f"command {name!r} declares a {timeout}-minute timeout",
                location=command.src.rel if command.src else name,
                hint=f"a single job may not exceed {MAX_JOB_MINUTES} minutes; fan the work out instead",
            )
