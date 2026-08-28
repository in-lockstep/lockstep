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
    _supply_chain(report, path)
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
    """GATE-CFG-2 — configuration must not come from the change under review."""
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    base = os.environ.get("GITHUB_BASE_REF", "")
    if event in ("pull_request", "pull_request_target") and not base:
        report.add(
            "DOC110",
            Severity.ERROR,
            "reviewing a pull request with no base ref, so configuration would resolve from the "
            "ref under review",
            "Set GITHUB_BASE_REF, or pass --base. Configuration defines the bindings, policy and "
            "path tiers constraining the run; loading it from the change under review lets that "
            "change rewrite its own constraints.",
        )
    if event == "pull_request_target":
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
    from .prompts.review import LENSES

    for aspect, lens in sorted(LENSES.items()):
        try:
            lens().body_text()
        except BodyNotFound as e:
            report.add("DOC140", Severity.ERROR, f"prompt body missing for {aspect}: {e}")


def _supply_chain(report: Report, root: Path) -> None:
    vendor = root / "src" / "in_lockstep" / "ai" / "llm" / "vendor.lock"
    if not vendor.exists():
        report.add(
            "DOC150",
            Severity.WARNING,
            "the vendored transport has no provenance record",
            "vendor.lock should record the origin commit and per-file hashes.",
        )


def _strict_policy(report: Report, root: Path) -> None:
    """`--strict` is what an organisation puts in a required check.

    It is the honest replacement for a compile-time refusal: the standard is a diff a repository
    can delete, and this is what notices.
    """
    module = root / "lockstep.py"
    if not module.exists():
        report.add(
            "DOC160",
            Severity.WARNING,
            "no lockstep.py; running on detected defaults",
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
