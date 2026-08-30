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
from typing import Any


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
    lockstep = _load_configured(path)

    _spend_ceiling(report)
    _config_provenance(report)
    _branch_protection(report, path)
    _escalation_labels(report, path, lockstep)
    _actions_may_open_changes(report, path)
    _history_integrity(report, path)
    _egress(report)
    _prompt_bodies(report)
    if lockstep is not None:
        _model_routes(report, lockstep)
    if strict:
        _strict_policy(report, path)
        if lockstep is not None:
            _strict_baseline(report, lockstep)
            _strict_opt_outs(report, lockstep)
            _strict_approval_path(report, lockstep)
    return report


def _load_configured(root: Path) -> Any | None:
    """The repository's lifecycle, loaded once for every check that reads it.

    From the TRUSTED ref, exactly as `_default_lockstep` loads it — loading the working tree here
    would execute the change under review, which on a pull-request pipeline runs before the
    review with the provider key in the environment. That is the fail-open GATE-CFG-2 exists to
    refuse, and a diagnostic must not be the hole. The workflow-registry snapshot is restored,
    because a diagnostic must not leave the process knowing about workflows nobody asked it to
    run. None when there is no module, or when it will not load — a module that will not load
    fails other commands loudly, not this one.
    """
    from .core.workflow import restore, snapshot
    from .loader import NoLifecycle, load, lockstep_from
    from .platform.ci import detect as detect_ci

    ci_env = detect_ci()
    state = snapshot()
    try:
        module, _ref = load(
            str(root),
            base=ci_env.base_ref if ci_env else "",
            reviewing=ci_env.reviewing if ci_env else False,
        )
        return lockstep_from(module)
    except NoLifecycle:
        return None
    except Exception:  # noqa: BLE001 - see docstring
        return None
    finally:
        restore(state)


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


def _escalation_labels(report: Report, root: Path, lockstep: Any = None) -> None:
    """The self-feeding loop routes on labels, so the labels are a control, not decoration.

    `escalate` files each follow-up ticket with `ai-generated` and `ai-attempt-N`, and both carry
    weight. `ai-generated` is what the trigger matches AND — because applying a label needs write
    access and commenting does not — it is the authorization that trampoline has instead of a gate
    job. `ai-attempt-N` is where the attempt count lives: `attempt_of` reads the highest N off the
    source ticket, which is how the loop is bounded without a store to count in.

    So a missing label is not cosmetic in either case. Missing `ai-generated` and nothing routes,
    after the run that failed has already been paid for. Missing `ai-attempt-N` and, if the host
    drops the label rather than refusing the create, every follow-up reads attempt 0 and files
    attempt 1 — the cap silently stops existing, which is the one failure mode this loop must not
    have.

    Scoped to repositories that have actually wired the loop: a workflow file has to name the
    label before this says anything. Inventing a finding for a repository that never asked for the
    hook is how a code teaches people to ignore it.
    """
    if not (root / ".git").exists():
        return
    from .platform.propose import AI_GENERATED, ATTEMPT_PREFIX

    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    wired = [w.name for w in workflows if AI_GENERATED in _read(w)]
    if not wired:
        return

    try:
        result = subprocess.run(
            ["gh", "api", "repos/{owner}/{repo}/labels", "--paginate", "--jq", ".[].name"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        report.add("DOC122", Severity.NOTE, "could not check the escalation labels (gh unavailable)")
        return
    if result.returncode != 0:
        report.add(
            "DOC122",
            Severity.NOTE,
            "could not read this repository's labels; the escalation labels were not checked",
        )
        return

    present = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    where = ", ".join(wired)
    if AI_GENERATED not in present:
        report.add(
            "DOC123",
            Severity.ERROR,
            f"{where} routes on the `{AI_GENERATED}` label, which this repository does not have",
            f"Create it: `gh label create {AI_GENERATED}`. Until it exists, a failed run pays for "
            f"a model call and then cannot file the follow-up ticket, and nothing would route to "
            f"the fixing verb even if it could.",
        )

    # One per attempt the cap allows, because `escalate` names the label after the number.
    cap = int(getattr(lockstep, "max_attempts", 3) or 3)
    missing = [f"{ATTEMPT_PREFIX}{n}" for n in range(1, cap + 1) if f"{ATTEMPT_PREFIX}{n}" not in present]
    if missing:
        report.add(
            "DOC124",
            Severity.ERROR,
            f"{len(missing)} of {cap} attempt label(s) are missing: {', '.join(missing)}",
            f"`attempt_of` reads the loop's attempt count off these, so a host that drops an "
            f"unknown label instead of refusing the create leaves every follow-up reading attempt "
            f"0 — max_attempts={cap} stops bounding anything. Create them: "
            f"`{'; '.join(f'gh label create {name}' for name in missing)}`.",
        )


def _actions_may_open_changes(report: Report, root: Path) -> None:
    """A propose job that cannot open a pull request fails at the last call it makes.

    The whole design routes writes through a change request a human reads, so the privileged half
    of every trampoline ends in `Scm.open_change`. There is a repository setting that forbids
    exactly that — "Allow GitHub Actions to create and approve pull requests", off by default — and
    when it is off, nothing is wrong with the configuration, the credentials or the change. The run
    does all of its work, pays for its model call, pushes its branch, and dies on the last API call
    with `GitHub Actions is not permitted to create or approve pull requests`.

    That is the worst place to discover a setting. Reporting the branch as recoverable is little
    comfort when the run that produced it cost real money, and an unattended trigger has nobody
    watching to notice.

    Keyed off `pull-requests: write`, because a job asking for that permission is a job that
    intends to open one. A repository whose trampolines only review says nothing here.
    """
    if not (root / ".git").exists():
        return
    proposers = [
        path.name
        for path in sorted((root / ".github" / "workflows").glob("*.yml"))
        if "pull-requests: write" in _read(path)
    ]
    if not proposers:
        return

    try:
        result = subprocess.run(
            ["gh", "api", "repos/{owner}/{repo}/actions/permissions/workflow"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        report.add(
            "DOC125", Severity.NOTE, "could not check whether Actions may open a change (gh unavailable)"
        )
        return
    if result.returncode != 0:
        report.add(
            "DOC125",
            Severity.NOTE,
            "could not read this repository's Actions permissions; whether a propose job can open "
            "a change was not checked",
        )
        return

    import json

    try:
        settings = json.loads(result.stdout or "{}")
    except ValueError:  # pragma: no cover - a shape change is a note, not a crash
        report.add("DOC125", Severity.NOTE, "could not parse this repository's Actions permissions")
        return

    # Absent rather than false is the honest unknown: an older host, or a shape that moved.
    allowed = settings.get("can_approve_pull_request_reviews")
    if allowed is None:
        report.add(
            "DOC125",
            Severity.NOTE,
            "this host does not report whether Actions may open a change; it was not checked",
        )
        return
    if not allowed:
        report.add(
            "DOC126",
            Severity.ERROR,
            f"{', '.join(proposers)} opens a pull request, which Actions is not permitted to do here",
            "Settings → Actions → General → Workflow permissions → 'Allow GitHub Actions to "
            "create and approve pull requests'. Without it the privileged job does all of its "
            "work and fails on its last call with `GitHub Actions is not permitted to create or "
            "approve pull requests`, after the unprivileged half has already paid for a model.",
        )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - unreadable file is the same as not mentioning it
        return ""


def _history_integrity(report: Report, root: Path) -> None:
    """A ledger that was rewritten must fail the same check the controls do.

    `report` says it at read time; this says it where an organisation puts a required check, so
    tampering with the evidence breaks CI rather than waiting for an auditor to run `report`.
    Quiet when the branch does not exist — a repository that has never recorded has nothing to
    verify, and inventing a warning for it would teach people to ignore this code.
    """
    if not (root / ".git").exists():
        return
    from .platform.ledger import GitLedger, HistoryError

    ledger = GitLedger(root=root)
    try:
        problems = ledger.verify()
    except HistoryError:
        return
    if problems:
        shown = "; ".join(problems[:3]) + ("; …" if len(problems) > 3 else "")
        report.add(
            "DOC167",
            Severity.ERROR,
            f"{ledger.branch} is not append-only: {len(problems)} record(s) rewritten",
            f"{shown}. A run record modified or deleted after append is exactly what the ledger "
            "exists to make visible. Note the check reads the retained chain only: a force-push "
            "that replaced the chain discards the contradiction, so protect the history branch "
            "against force-push and deletion on the remote.",
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


def _model_routes(report: Report, lockstep: Any) -> None:
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

    models = getattr(lockstep, "models", None)
    routes = dict(getattr(models, "routes", None) or {})
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


def _strict_baseline(report: Report, lockstep: Any) -> None:
    """The org baseline, checked instead of hoped for.

    An organisation states its floor as environment variables in the required check's own
    environment — the same attestation seam `IN_LOCKSTEP_ORG_SPEND_LIMIT` uses, and deliberately
    NOT something the repository's module can supply, because the module is the thing under
    check. Nothing here fires for a repository whose organisation states no baseline: strict
    without a baseline is exactly what it was before.

    This is also the KISS answer to the Tier.MANDATE debate: visibility through a required check
    that an organisation controls, before any new container semantics. A repository can still
    delete its policy layer — and this is what notices, loudly, in the check the org requires.
    """
    required = [
        name.strip()
        for name in os.environ.get("IN_LOCKSTEP_REQUIRED_POLICIES", "").split(",")
        if name.strip()
    ]
    if required:
        present = {str(getattr(layer, "name", "")) for layer in lockstep.policy.layers}
        for name in required:
            if name not in present:
                report.add(
                    "DOC162",
                    Severity.ERROR,
                    f"required policy layer {name!r} is not contributed",
                    "The organisation baseline (IN_LOCKSTEP_REQUIRED_POLICIES) names layers this "
                    f"module must contribute; present: {', '.join(sorted(present)) or '(none)'}. "
                    "A deleted standard is a visible diff — this is the check that sees it.",
                )

    ceiling = os.environ.get("IN_LOCKSTEP_MAX_BUDGET_USD", "").strip()
    if ceiling:
        try:
            org_max = float(ceiling)
        except ValueError:
            report.add(
                "DOC163",
                Severity.WARNING,
                f"IN_LOCKSTEP_MAX_BUDGET_USD is {ceiling!r}, which is not a number",
            )
        else:
            declared = getattr(lockstep.budget, "usd", None)
            if declared is None:
                report.add(
                    "DOC163",
                    Severity.ERROR,
                    f"the organisation caps a run at ${org_max:.2f} but this module declares no budget",
                    "Declare one in lockstep.py (`lockstep.budget = Budget(usd=...)`) at or "
                    "under the cap. An absent ceiling is not a compliant ceiling.",
                )
            elif declared > org_max:
                report.add(
                    "DOC163",
                    Severity.ERROR,
                    f"the declared budget ${declared:.2f} exceeds the organisation's ${org_max:.2f}",
                    "Ceilings compose downward: a repository may tighten the org maximum, never raise it.",
                )

    turns = os.environ.get("IN_LOCKSTEP_MAX_TURNS", "").strip()
    if turns.isdigit():
        resolved = lockstep.policy.resolve()
        declared_turns = getattr(resolved, "max_turns", None)
        if declared_turns is None or declared_turns > int(turns):
            report.add(
                "DOC163",
                Severity.ERROR,
                f"the resolved turn ceiling is {declared_turns or 'unbounded'}; "
                f"the organisation's maximum is {turns}",
                "Contribute a policy layer with max_turns at or under the org maximum.",
            )


def _strict_opt_outs(report: Report, lockstep: Any) -> None:
    """The named opt-outs, as named findings.

    `UnsandboxedEgress` and `UnsandboxedRun` were designed to be greppable lines in a diff; this
    puts the same names in the check an organisation requires, so the opt-out is visible in the
    place a fleet actually looks. Warnings, not errors — visibility, not impossibility, per the
    resolved tension: a repository may have decided this deliberately, and the finding names
    where that decision lives so a reviewer can read its justification.
    """
    from .privileged.egress import EgressPolicy, UnsandboxedEgress

    container = lockstep.container
    if container.has(EgressPolicy) and isinstance(container.resolve(EgressPolicy), UnsandboxedEgress):
        report.add(
            "DOC165",
            Severity.WARNING,
            "the egress opt-out is bound (UnsandboxedEgress)",
            "Every run may reach the open internet. Deliberate on a laptop; on a host that can "
            "constrain egress, remove the binding and set IN_LOCKSTEP_EGRESS=enforced instead.",
        )

    for binding in container.resolved():
        commands = getattr(binding.impl, "commands", None)
        runner = getattr(commands, "inner", commands)  # a WorktreeRunner wraps its runner
        if type(runner).__name__ == "UnsandboxedRun":
            report.add(
                "DOC166",
                Severity.WARNING,
                f"{binding.iface.__name__} runs commands through UnsandboxedRun",
                "Model-chosen commands execute on this host with its environment and its "
                "credentials. The named adapter exists so this is a decision a diff shows; "
                "this finding is the same decision where the fleet looks.",
            )


def _strict_approval_path(report: Report, lockstep: Any) -> None:
    """An adapter that both spends and writes needs an approval path before the run refuses.

    `Lockstep.context` refuses at run time; a required check should say so before a trigger
    fires, in the same place the rest of the org floor is asserted.
    """
    from .core.middleware import provides_approval
    from .core.verbs import Capability

    if any(provides_approval(m) for m in lockstep.middleware):
        return
    for binding in lockstep.container.resolved():
        capabilities = getattr(binding.impl, "capabilities", None) or frozenset()
        if {Capability.SPENDS_BUDGET, Capability.WRITES_FILES} <= set(capabilities):
            report.add(
                "DOC164",
                Severity.ERROR,
                f"{binding.iface.__name__} spends and writes, and no middleware provides approval",
                "Add ApprovalGate() to lockstep.middleware. Every run of this adapter will be "
                "refused at startup without it — this says so before a trigger finds out.",
            )
            return


def as_json(report: Report) -> str:
    """The fleet scanner's format: stable codes, machine-readable severities, one exit-deciding
    boolean. Hints ride along because the hint is the remediation, and a dashboard that can only
    say DOC162 sends its reader back to the terminal."""
    import json

    return json.dumps(
        {
            "ok": report.ok,
            "errors": len(report.errors),
            "checks": [
                {
                    "code": c.code,
                    "severity": c.severity.value,
                    "message": c.message,
                    **({"hint": c.hint} if c.hint else {}),
                }
                for c in report.checks
            ],
        },
        indent=2,
        sort_keys=True,
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
