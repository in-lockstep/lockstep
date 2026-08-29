"""Will the target accept this, and are the controls actually in place?

Two different questions live in this file's ancestry, and keeping them apart matters: `lint` asks
whether a lifecycle is well built, `doctor` asks whether it will run safely where it is pointed.
A configuration can be excellent and undeployable, and conflating the two makes both easier to
ignore.

Several checks here exist because a control moved out of process when model invocation moved in.
Where the framework can only verify an attestation rather than the thing attested, it says so
rather than implying more than it knows.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


@dataclass(frozen=True)
class Check:
    code: str
    severity: Severity
    message: str
    hint: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, code: str, severity: Severity, message: str, hint: str = "") -> None:
        self.checks.append(Check(code, severity, message, hint))

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.severity is Severity.ERROR]

    @property
    def ok(self) -> bool:
        return not self.errors


def run(root: str | Path = ".", *, strict: bool = False) -> Report:
    report = Report()
    path = Path(root)

    _spend_ceiling(report)
    _config_provenance(report)
    _branch_protection(report, path)
    _egress(report)
    _prompt_bodies(report)
    _model_routes(report, path)
    if strict:
        _strict_policy(report, path)
    return report


def _spend_ceiling(report: Report) -> None:
    """GATE-COST-5.

    The per-day, per-agent ceiling the substrate used to enforce before a run started is gone,
    and nothing in-process replaces it: a budget enforced inside the process holding the API key
    cannot bound that process. The replacement is a provider-side organisation limit, which this
    can only ask about.
    """
    attested = os.environ.get("IN_LOCKSTEP_ORG_SPEND_LIMIT", "").strip()
    if not attested:
        report.add(
            "DOC101",
            Severity.ERROR,
            "no provider-side organisation spend limit is attested",
            "Set a hard monthly cap in the provider console and record it as "
            "IN_LOCKSTEP_ORG_SPEND_LIMIT=<amount>. A per-run budget cannot bound a runaway "
            "trigger, and the per-day ceiling the substrate enforced no longer exists.",
        )
        return
    report.add(
        "DOC102",
        Severity.NOTE,
        f"organisation spend limit attested as {attested}",
        "This is an attestation, not a verification: nothing here can read the provider console.",
    )


def _config_provenance(report: Report) -> None:
    """GATE-CFG-2 — configuration must not come from the change under review.

    Reads the CI environment through `platform.ci.detect` rather than `GITHUB_*` directly.
    This check spent its first months hardcoding GitHub's variables while `ci.detect` sat
    beside it computing the same answer for GitLab too — so on a GitLab merge-request pipeline
    the check silently passed and configuration loaded from the ref under review, which is the
    exact failure it exists to refuse.
    """
    from .platform.ci import detect

    env = detect()
    if env is None:
        return
    if env.reviewing and not env.base_ref:
        report.add(
            "DOC110",
            Severity.ERROR,
            "reviewing a change with no base ref, so configuration would resolve from the ref under review",
            "Set the host's base-ref variable (GITHUB_BASE_REF on GitHub Actions; GitLab sets "
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME on merge-request pipelines), or pass --base. "
            "Configuration defines the bindings, policy and path tiers constraining the run; "
            "loading it from the change under review lets that change rewrite its own "
            "constraints.",
        )
    if env.event == "pull_request_target":
        report.add(
            "DOC111",
            Severity.WARNING,
            "running on pull_request_target, which grants a write token to a workflow that can "
            "check out fork code",
            "Prefer pull_request, and let the two-job trampoline hold write access separately.",
        )


def _branch_protection(report: Report, root: Path) -> None:
    """With an ambient repository token, branch protection is the only remaining backstop."""
    if not (root / ".git").exists():
        return
    try:
        result = subprocess.run(
            ["gh", "api", "repos/{owner}/{repo}/branches/main/protection"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        report.add(
            "DOC120",
            Severity.NOTE,
            "could not check branch protection (gh unavailable)",
            "",
        )
        return
    if result.returncode != 0:
        report.add(
            "DOC121",
            Severity.ERROR,
            "the default branch has no protection rule, or it could not be read",
            "The apply job holds an ambient repository token that can write any branch, so "
            "branch protection is what keeps protected branches unreachable. Without it, "
            "'writes go through a pull request' is a convention rather than a guarantee.",
        )


def _egress(report: Report) -> None:
    from .privileged.egress import ENV_MODE, EgressMode, EgressPolicy

    policy = EgressPolicy.detect()
    if policy.mode is EgressMode.NONE:
        report.add(
            "DOC130",
            Severity.WARNING,
            "no egress enforcement is declared",
            f"Set {ENV_MODE}=enforced where the host constrains egress. Runs that hold write or "
            "execute tools, or that read untrusted content, are refused without it.",
        )
        return
    if not policy.verify():
        report.add(
            "DOC131",
            Severity.ERROR,
            f"{ENV_MODE} claims {policy.mode.value} but a probe reached the open internet",
            "An asserted mode a probe disproves reads as a control while providing none.",
        )


def _prompt_bodies(
    report: Report,
) -> None:
    """A prompt body is a file. A missing one should fail here, not on a first run."""
    from .ai.prompt import BodyNotFound
    from .prompts.implement import PROMPTS
    from .prompts.review import LENSES
    from .prompts.triage import TRIAGE_PROMPTS

    # Every family. Checking only the review lenses meant the check covered whichever prompts
    # existed when it was written, which is the shape of a check that silently stops covering
    # things — an implementing or triaging prompt with a missing body would have failed on a run
    # that had already resolved a container and a credential.
    everything: dict[str, type] = {**LENSES, **PROMPTS, **TRIAGE_PROMPTS}
    for aspect, lens in sorted(everything.items()):
        try:
            lens().body_text()
        except BodyNotFound as e:
            report.add("DOC140", Severity.ERROR, f"prompt body missing for {aspect}: {e}")


def _model_routes(report: Report, root: Path) -> None:
    """A route that would be refused at run time should say so here, where nothing is spent.

    An unregistered provider and an unpriced model both surface today at the first model call —
    after a container has resolved, a credential has loaded and a person has waited. Routes are
    declared in the module, so this walks them against the same registry and table the run would
    use. Warnings rather than errors, because doctor sees the default registry: a module that
    registers its own provider through `invoker_factory` is ahead of what this can verify.
    """
    from .ai.auth import Auth
    from .ai.bootstrap import Model, default_registry, table_for
    from .ai.pricing import CostTable
    from .core.workflow import restore, snapshot
    from .loader import NoLifecycle, load, lockstep_from
    from .platform.ci import detect as detect_ci

    # From the TRUSTED ref, exactly as `_default_lockstep` loads it — loading the working tree
    # here would execute the change under review, which on a pull-request pipeline runs before the
    # review with the provider key in the environment. That is the fail-open GATE-CFG-2 exists to
    # refuse, and a diagnostic must not be the hole. Reading base also means the routes reported
    # are the ones the run will actually use, not the head's.
    ci_env = detect_ci()
    # Loading the module registers its workflows in the process-global registry. Doctor only
    # reads the routes, so it restores the snapshot: a diagnostic must not leave the process
    # knowing about workflows nobody asked it to run.
    state = snapshot()
    try:
        module, _ref = load(
            str(root),
            base=ci_env.base_ref if ci_env else "",
            reviewing=ci_env.reviewing if ci_env else False,
        )
        lockstep = lockstep_from(module)
    except NoLifecycle:
        return
    except Exception:  # a module that will not load fails other commands loudly, not this one
        return
    finally:
        restore(state)
    routes = dict(getattr(lockstep.models, "routes", None) or {})
    if not routes:
        return
    try:
        registry = default_registry(Auth())
    except Exception:
        return
    container = lockstep.container
    bound = container.resolve(CostTable) if container.has(CostTable) else None
    for verb, model_id in sorted(routes.items()):
        selected = Model(model_id)
        if not selected.provider or selected.provider not in registry.names():
            report.add(
                "DOC150",
                Severity.WARNING,
                f"route {verb} -> {model_id!r} names a provider that is not registered",
                f"Registered: {', '.join(registry.names()) or '(none)'}. Model ids are "
                'qualified: "<provider>:<model>".',
            )
            continue
        # The same table the run builds, so "priced" here means priced there — including the
        # zero a free registration adds. Re-deriving the rule inline is how the two drift.
        table = table_for(registry, selected, bound)
        if not table.knows(selected.name):
            report.add(
                "DOC151",
                Severity.WARNING,
                f"route {verb} -> {model_id!r} is unpriced, so a run would be refused",
                "Add a rate to a CostTable and bind it in the module, or register the provider "
                "free=True where the destination genuinely bills nothing. An unpriced model is "
                "refused at the first call, after a credential has already been resolved.",
            )


def _strict_policy(report: Report, root: Path) -> None:
    """`--strict` is what an organisation puts in a required check.

    It is the honest replacement for a compile-time refusal: the standard is a diff a repository
    can delete, and this is what notices.
    """
    from .loader import LEGACY_MODULE_FILE, MODULE_FILE

    if (root / MODULE_FILE).exists():
        return
    # The check once looked for `lockstep.py` at the root — the location the loader had already
    # deprecated — so every migrated repository read as unconfigured to the one check an
    # organisation is told to require. The paths come from the loader now, so the two cannot
    # drift apart again.
    if (root / LEGACY_MODULE_FILE).exists():
        report.add(
            "DOC161",
            Severity.WARNING,
            f"lifecycle found at the deprecated {LEGACY_MODULE_FILE}, not {MODULE_FILE}",
            f"Move it to {MODULE_FILE}. The root is on sys.path for anything run from there, so "
            "a root module is importable by project code that never chose to depend on it.",
        )
        return
    report.add(
        "DOC160",
        Severity.WARNING,
        f"no {MODULE_FILE}; running on detected defaults",
        "That is supported, but an organisation's policy contributions cannot reach a "
        "repository that declares none.",
    )


def render(report: Report) -> str:
    if not report.checks:
        return "doctor: no findings"
    lines = []
    for check in report.checks:
        lines.append(f"{check.severity.value.upper():<7} {check.code}  {check.message}")
        if check.hint:
            for wrapped in _wrap(check.hint, 88):
                lines.append(f"                 {wrapped}")
    errors = len(report.errors)
    lines.append("")
    lines.append(f"{len(report.checks)} finding(s), {errors} error(s)")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines
