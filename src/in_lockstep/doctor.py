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

import json
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
    _prompt_bodies(report, lockstep)
    _packs(report, path)
    _cassettes(report, path)
    if lockstep is not None:
        _model_routes(report, lockstep)
        _pack_guardrails(report, lockstep)
        _tooling(report, lockstep, path)
        _sandbox_executables(report, lockstep, path)
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


def _cassettes(report: Report, root: Path) -> None:
    """Say what recordings are on disk, and whether git is ignoring them.

    The first diagnostic about cassettes at all, which is why it exists: recording is on by default
    in what `init` scaffolds, and a control with no diagnostic is one nobody can check. A recording
    holds the request verbatim -- the whole composed prompt and the whole diff -- and redaction
    masks credentials rather than source. So the question worth asking of a repository is not
    whether recording is on. It is whether the directory it writes to is one git would commit.

    Asked of git rather than by reading `.gitignore` here. `check-ignore` is the only thing that
    gets precedence, negation and a nested pattern right, it is installed wherever this runs, and
    reimplementing it would be this framework guessing at something the repository already states.

    Silent when there is nothing recorded, because a note about an empty directory is noise, and
    noise is how a report stops being read.
    """
    from .ai.replay import CASSETTE_DIR

    directory = root / CASSETTE_DIR
    tapes = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not tapes:
        return
    try:
        ignored = (
            subprocess.run(
                # `--` because a root beginning with a dash would otherwise be read as an
                # option. No shell is involved, so this is not injection; it is git parsing
                # a path as a flag and answering a question nobody asked. Raised by this
                # repository's own review of the change that added this check.
                ["git", "check-ignore", "-q", "--", str(directory)],
                cwd=root,
                capture_output=True,
                timeout=15,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        report.add("DOC169", Severity.NOTE, f"{len(tapes)} recording(s) in {CASSETTE_DIR}/ (git unavailable)")
        return
    if ignored:
        report.add(
            "DOC168",
            Severity.NOTE,
            f"{len(tapes)} recording(s) in {CASSETTE_DIR}/, ignored by git",
            "Each holds a whole composed prompt and the diff it was sent with. They are replay "
            "input and harvest input; nothing else reads them, and deleting one costs a real call.",
        )
        return
    report.add(
        "DOC168",
        Severity.WARNING,
        f"{len(tapes)} recording(s) in {CASSETTE_DIR}/, and git is NOT ignoring them",
        f"A commit from this tree publishes the prompts and diffs they hold. Add "
        f"`{CASSETTE_DIR}/` to .gitignore -- `in-lockstep init` writes that line, and will "
        f"append it to a .gitignore you already have.",
    )


#: What GitHub says when the branch exists and carries no protection rule. The only answer that
#: means the control is genuinely absent; every other failure means this could not find out.
_UNPROTECTED = "Branch not protected"


def _gh(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """`gh` with output captured, or None when gh could not be run at all."""
    try:
        return subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None


def _branch_protection(report: Report, root: Path) -> None:
    """With an ambient repository token, branch protection is the only remaining backstop.

    Reporting it is where this went wrong, and the failure is the one this repository's own rule
    names: **absent is not zero**. Any non-zero `gh` exit was reported as `the default branch has
    no protection rule, or it could not be read` at ERROR — one message for two facts, and the
    severity of the worse one. In CI that fired on every run: the job holds a token that cannot
    read the protection API, so a fully protected branch was reported as unprotected. The verdict
    had to be discarded with `continue-on-error` to keep the job usable, which is how a
    diagnostic that cries wolf ends up gating nothing at all (#249).

    So only GitHub actually saying `Branch not protected` is the ERROR. Everything else — no
    credential, no permission, no network, a default branch by another name — is a NOTE that says
    the check did not run, which is what its two neighbours (`DOC122`, `DOC125`) already did.

    The default branch is asked for rather than assumed. This checked `branches/main/protection`
    literally, so a repository whose default is `master` got `Branch not found` and, under the old
    reporting, an ERROR about a rule it may well have had.
    """
    if not (root / ".git").exists():
        return
    named = _gh(root, "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name")
    if named is None:
        report.add("DOC120", Severity.NOTE, "could not check branch protection (gh unavailable)", "")
        return
    branch = named.stdout.strip()
    if named.returncode != 0 or not branch:
        report.add(
            "DOC120",
            Severity.NOTE,
            "could not read this repository's default branch; branch protection was not checked",
            _gh_said(named),
        )
        return

    result = _gh(root, "api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection")
    if result is None:
        report.add("DOC120", Severity.NOTE, "could not check branch protection (gh unavailable)", "")
        return
    if result.returncode == 0:
        _protection_bypasses(report, root, branch, result.stdout)
        return
    if _UNPROTECTED in (result.stderr + result.stdout):
        report.add(
            "DOC121",
            Severity.ERROR,
            f"the default branch ({branch}) has no protection rule",
            "The apply job holds an ambient repository token that can write any branch, so "
            "branch protection is what keeps protected branches unreachable. Without it, "
            "'writes go through a pull request' is a convention rather than a guarantee.",
        )
        return
    report.add(
        "DOC120",
        Severity.NOTE,
        f"could not read branch protection for {branch}; it was not checked",
        _gh_said(result)
        + " A job token usually cannot read this API. Run `in-lockstep doctor` at a terminal, "
        "where gh carries your own credentials, to get an answer.",
    )


def _protection_bypasses(report: Report, root: Path, branch: str, protection: str) -> None:
    """A protection rule an administrator can step around is enforced by choice, and doctor says
    whose. Two settings do it: `enforce_admins` off on the classic rule, and a bypass actor on an
    active ruleset that requires a pull request or a status check. Both were true here (#312) --
    one engineer, who is that admin, and a `required` check that was required of everybody else.
    WARNINGs and not ERRORs: the rule exists and holds for everyone but the named role, which is a
    fact to know rather than a check that failed. Neither call's failure is reported: the rule was
    already read, and a ruleset API a token cannot list is the NOTE `DOC120` would have made if the
    protection read had failed, not a second one.
    """
    try:
        rule = json.loads(protection)
    except ValueError:
        rule = {}
    enforce = rule.get("enforce_admins") if isinstance(rule, dict) else None
    if isinstance(enforce, dict) and enforce.get("enabled") is False:
        report.add(
            "DOC127",
            Severity.WARNING,
            f"branch protection on {branch} is not enforced for administrators (enforce_admins is off)",
            "An administrator can push past the required checks. With one engineer who is that "
            "administrator, `required` is enforceable only by choice; turn on 'Do not allow "
            "bypassing the above settings' to make it a guarantee.",
        )
    listed = _gh(root, "api", "repos/{owner}/{repo}/rulesets")
    if listed is None or listed.returncode != 0:
        return
    try:
        rulesets = json.loads(listed.stdout)
    except ValueError:
        return
    for summary in rulesets if isinstance(rulesets, list) else []:
        if not isinstance(summary, dict) or summary.get("enforcement") != "active":
            continue
        detail = _gh(root, "api", f"repos/{{owner}}/{{repo}}/rulesets/{summary.get('id')}")
        if detail is None or detail.returncode != 0:
            continue
        try:
            ruleset = json.loads(detail.stdout)
        except ValueError:
            continue
        if not isinstance(ruleset, dict):
            continue
        kinds = {str(r.get("type")) for r in ruleset.get("rules", []) if isinstance(r, dict)}
        actors = [a for a in ruleset.get("bypass_actors", []) if isinstance(a, dict)]
        guarded = sorted(kinds & {"pull_request", "required_status_checks"})
        if actors and guarded:
            who = ", ".join(
                f"{a.get('actor_type', '?')} {a.get('actor_id', '?')} ({a.get('bypass_mode', 'always')})"
                for a in actors
            )
            name = ruleset.get("name", summary.get("id"))
            report.add(
                "DOC128",
                Severity.WARNING,
                f"ruleset {name!r} lets {who} bypass its {' and '.join(guarded)} rule(s)",
                "A bypass actor on the ruleset that requires the review check makes that check "
                "required of everyone except the actor named. Remove the bypass, or say in the "
                "objectives ledger that the check is enforced by choice.",
            )


def _gh_said(result: subprocess.CompletedProcess[str]) -> str:
    """What gh reported, trimmed to one line. Quoted rather than paraphrased: the difference
    between `Bad credentials` and `Resource not accessible by integration` is the difference
    between two fixes, and a paraphrase loses it."""
    said = (result.stderr.strip() or result.stdout.strip()).splitlines()
    return f"gh said: {said[0][:200]}" if said else ""


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
        acknowledged = ledger.acknowledged_rewrites()
    except HistoryError:
        return
    for note in acknowledged:
        # Still said, under the name of the person who explained it. An acknowledged rewrite is
        # not an alarm and not a secret: the note is the reason the check can stay red for the
        # unexplained ones without teaching anybody to read past it (#295).
        report.add(
            "DOC173",
            Severity.NOTE,
            f"{ledger.branch}: commit {note.commit[:12]} rewrote {len(note.lines)} record(s), "
            f"acknowledged by {note.by}",
            f"{note.reason} ({note.ts}). `history --acknowledge` wrote this; the rewrite itself is "
            f"unchanged and the note is append-only like a record.",
        )
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


def _prompt_bodies(report: Report, lockstep: Any = None) -> None:
    """A prompt body is a file. A missing one should fail here, not on a first run.

    Two walks. The shipped maps, so a packaging mistake is caught before anybody routes to it;
    and every bound adapter's `compositions()`, the seam `Inspectable` exists for, so the body a
    repository pointed at is checked too -- which is the promise this function's docstring made
    for as long as it walked only the shipped maps, and the first thing an adopter tries (#311).
    """
    from .ai.prompt import BodyNotFound, Inspectable
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
    if lockstep is None:
        return
    for binding in lockstep.container.resolved():
        if isinstance(binding.impl, type) or not isinstance(binding.impl, Inspectable):
            continue
        for label, composition in sorted(binding.impl.compositions().items()):
            try:
                composition.prompt.body_text()
            except BodyNotFound as e:
                report.add(
                    "DOC140",
                    Severity.ERROR,
                    f"prompt body missing for {label} (bound by {composition.source}): {e}",
                    "A house body lives in `prompts/` at the repository root, as `Body.from_path`; "
                    "`in-lockstep show-prompt <name>` renders it once it resolves.",
                )


def _tooling(report: Report, lockstep: Any, root: Path) -> None:
    """The bound deterministic adapters can run the repository's tools from the repository.

    Two first-time users installed the tool as the site says, ran `selfcheck`, and got
    "ruff is not installed" and a red suite for repositories that had both in their own `.venv`:
    the adapters ran the tool's isolated interpreter (#167). An adapter that says where it found
    its tool (`Locatable`) is asked here, before any run, and a tool that is nowhere or that
    cannot be run from where it was found is an error naming the path tried.
    """
    import tempfile

    from .core.types import Locatable

    # The probe runs the repository's interpreter, which is the change under review's, so it gets
    # what a sandboxed adapter would get and no more: the pass-through variables, never this
    # process's credentials, and a working directory that is not the repository, so nothing under
    # review is on the import path. `doctor` loads configuration from the trusted ref for the
    # same reason, and a probe that undid that would be the hole.
    probe_env = {
        k: os.environ[k]
        for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
        if k in os.environ
    }
    probe_cwd = tempfile.gettempdir()

    # The detected root, absolute, rather than the `.` doctor was invoked in: it is what `ls`
    # resolves against, and a path in an error message should be one a person can open.
    repo_root = str(getattr(getattr(lockstep, "repo", None), "root", "") or Path(root).resolve())
    for binding in lockstep.container.resolved():
        impl = binding.impl
        if isinstance(impl, type):
            continue
        # The adapter's own tools, and then the ones the framework provisions for it (#375):
        # an adapter that names a `Graft` in `provisions` is asked where Node and Graft are the
        # same way a `PytestTest` is asked where pytest is. A framework-provisioned tool that is
        # not there yet is a NOTE, not an error: `in-lockstep provision` is what fills the cache,
        # and `doctor` runs before it in every job that runs both.
        asked: list[tuple[str, Any, bool]] = []
        if isinstance(impl, Locatable):
            asked.append((f"{binding.iface.__name__} -> {type(impl).__name__}", impl, False))
        for extra in getattr(impl, "provisions", ()) or ():
            if isinstance(extra, Locatable):
                asked.append(
                    (
                        f"{binding.iface.__name__} -> {type(impl).__name__} ({type(extra).__name__})",
                        extra,
                        True,
                    )
                )
        for who, located, provisioned in asked:
            _locations(
                report,
                located,
                who,
                repo_root,
                provisioned=provisioned,
                probe_env=probe_env,
                probe_cwd=probe_cwd,
            )


def _sandbox_executables(report: Report, lockstep: Any, root: Path) -> None:
    """What the `run_script` image actually has, against what the binding says it has.

    `Sandbox(executables=...)` is a declaration, and a declaration nobody can check is one that
    goes stale silently. This is where it is checked, because a probe here costs a container start
    in a diagnostic rather than on every run before the first model call -- which is the reason the
    tool itself does not probe.

    It also makes the declaration DISCOVERABLE. Without this an adopter has to already know that
    their image carries one of the twelve programs the tool offers, which is what a paid run found
    out by trying 36 times (#401). Here they are handed the tuple to paste.
    """
    from .ai.builtins import ALLOWED_COMMANDS

    runner = getattr(getattr(lockstep, "workshop", None), "commands", None)
    # `Lockstep.use` wraps the bound runner in a `WorktreeRunner`, whose `inner` is the sandbox
    # that owns the image. Unwrapped once rather than reached for by type, so an adopter's own
    # wrapper is treated the same way.
    runner = getattr(runner, "inner", runner)
    image = str(getattr(runner, "image", "") or "")
    if runner is None or not image:
        # No image is not a finding: `run_script` may be unbound, or bound to a runner that is not
        # a container at all, and neither is this check's business.
        return

    declared = tuple(getattr(runner, "executables", ()) or ())
    found = _probe_executables(runner, root)
    if found is None:
        report.add(
            "DOC183",
            Severity.NOTE,
            f"could not ask {image} what it carries, so `executables=` is unchecked",
            "A container runtime and a pullable image are what this needs; the declaration is "
            "still honoured, it is simply not verified here.",
        )
        return

    offered = tuple(program for program in ALLOWED_COMMANDS if program in found)
    if not declared:
        # A tuple a person can paste: one item needs its trailing comma, more must not have one.
        inner = ", ".join(f'"{program}"' for program in offered)
        listed = f"{inner}," if len(offered) == 1 else inner
        report.add(
            "DOC183",
            Severity.NOTE,
            f"{image} carries {len(offered)} of the {len(ALLOWED_COMMANDS)} programs run_script "
            f"offers, and the binding does not say so",
            f"Declare it where the sandbox is bound: `executables=({listed})`. Without it a model "
            f"is told it may run all {len(ALLOWED_COMMANDS)} and finds out otherwise a turn at a "
            f"time.",
        )
        return

    missing = tuple(program for program in declared if program not in found)
    unlisted = tuple(program for program in offered if program not in declared)
    if missing:
        report.add(
            "DOC183",
            Severity.WARNING,
            f"`executables=` names {', '.join(missing)}, which {image} does not carry",
            "The declaration has drifted from the image. A model will be offered these and get a "
            "stale-declaration error when it tries one.",
        )
    if unlisted:
        report.add(
            "DOC183",
            Severity.NOTE,
            f"{image} carries {', '.join(unlisted)}, which `executables=` does not name",
            "Not wrong -- an undeclared program is simply refused before it runs -- but a model "
            "could have used it.",
        )


def _probe_executables(runner: Any, root: Path) -> set[str] | None:
    """Which of the programs `run_script` offers resolve inside the bound image, or None.

    ONE container start, not one per program: the shell loop asks about all of them and prints the
    ones it finds. `sh` is the assumption, and where it does not hold -- a distroless image -- the
    probe cannot answer and says so rather than reporting an empty image.
    """
    import asyncio

    from .ai.builtins import ALLOWED_COMMANDS

    # `exit 0` at the end, and it is the whole reliability of this: without it the script's exit
    # code is the last `command -v`, which is 127 whenever the last program happens to be missing.
    # A probe that read that as "the shell is not there" would report every image as unaskable.
    tests = "; ".join(
        f"command -v {program} >/dev/null 2>&1 && echo {program}" for program in ALLOWED_COMMANDS
    )
    script = f"{tests}; exit 0"
    try:
        result = asyncio.run(runner.run(["sh", "-c", script], cwd=str(root), timeout=120.0))
    except (OSError, RuntimeError, ValueError):
        return None
    if getattr(result, "exit_code", 1) != 0:
        # The script ends `exit 0`, so anything else means it did not run: 126 is the runner
        # refusing (no container runtime), 127 is an image with no `sh`. Either way this learned
        # nothing, and an empty set would read as "the image carries nothing".
        return None
    return {line.strip() for line in str(getattr(result, "stdout", "")).splitlines() if line.strip()}


def _locations(
    report: Report,
    impl: Any,
    who: str,
    repo_root: str,
    *,
    provisioned: bool,
    probe_env: dict[str, str],
    probe_cwd: str,
) -> None:
    """One `Locatable`'s answers, as DOC180/DOC181/DOC182 lines."""
    from .core.verbs import Verb, verb_of

    for resolution in impl.locations(repo_root):
        if resolution.path is None and provisioned:
            report.add(
                "DOC182",
                Severity.NOTE,
                f"{who}: {resolution.tool} is not provisioned yet; looked for {', '.join(resolution.tried)}",
                "`in-lockstep provision` installs it into the framework's cache; a run that starts "
                "without it refuses the tool by name and goes on.",
            )
            continue
        if resolution.path is None:
            # The remedy for a missing pytest or ruff is to provision; the remedy for a
            # missing provisioner cannot be, because it is what provisioning runs.
            remedy = (
                "Put the provisioner on PATH or on the job's image. This binding is what builds "
                "the repository's environment, so nothing else can supply it."
                if verb_of(impl) is Verb.PROVISION
                else "Give the repository its own environment (`uv sync`, or `python -m venv .venv` "
                "and install the tool into it), or put the tool on PATH. An installed in-lockstep "
                "carries neither pytest nor ruff and must not run yours from its own interpreter."
            )
            report.add(
                "DOC180",
                Severity.ERROR,
                f"{who} found no {resolution.tool}; looked for {', '.join(resolution.tried)}",
                remedy,
            )
            continue
        if not resolution.probe:
            continue
        try:
            probe = subprocess.run(
                resolution.probe, capture_output=True, text=True, timeout=30, env=probe_env, cwd=probe_cwd
            )
        except (OSError, subprocess.SubprocessError) as e:
            report.add(
                "DOC181",
                Severity.ERROR,
                f"{who}: {resolution.tool} at {resolution.path} could not be run: {e}",
            )
            continue
        if probe.returncode != 0:
            tail = (probe.stderr or probe.stdout).strip().splitlines()[-1:] or [""]
            report.add(
                "DOC181",
                Severity.ERROR,
                f"{who}: {resolution.tool} at {resolution.path} ({resolution.how}) cannot do what the "
                f"adapter needs: `{' '.join(resolution.probe[1:])}` exited {probe.returncode}: {tail[0]}",
                "Install the tool into that environment, or bind the adapter with the path that has it.",
            )


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
        # What the registration declares the model can do, in the order the invoker asks: can it
        # do the job, then what does it cost. Every shipped AI verb asks for its answer in a
        # schema, so a registration declaring it will not honour one is a route every shipped
        # verb refuses before its first turn (GATE-MODEL-1) -- said here, where nothing is spent.
        if not registry.registration_for(selected).caps.structured_output:
            report.add(
                "DOC152",
                Severity.WARNING,
                f"route {verb} -> {model_id!r} is registered as not answering with a schema, so a "
                f"run would be refused",
                "Every shipped AI verb needs its answer in a schema. Route the verb to a model whose "
                "registration declares structured_output=True, or register this one so if it "
                "honours a schema when asked. The refusal names the model and the capability, and "
                "nothing is sent.",
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


def _packs(report: Report, root: Path) -> None:
    """DOC170-172. What an installed pack may do, against what this repository accepted.

    The comparison is possible at all because a receipt is canonical and derived: `in-lockstep add`
    recorded one, this re-derives from the code that is installed now, and the difference between
    them is a fact rather than a judgement.

    Only DOC170 is an error, and the line it draws is agency. A pack that gained a prompt, a
    version or a corpus case has changed; a pack that gained `reaches_network` may now do something
    this repository never agreed to, and the two should not fail the same way. Everything else here
    warns, in the same posture the standards layer takes about removal: visible, not impossible.
    """
    from .packs import installed, pinning
    from .receipt import compare, read_record, receipt_for_pack

    try:
        packs = installed()
    except Exception:  # pragma: no cover - defensive: a broken environment is not a finding here
        return
    if not packs:
        return

    for subject in packs:
        try:
            derived = receipt_for_pack(subject)
        except Exception as e:  # noqa: BLE001 - a pack that cannot be described is worth saying
            report.add(
                "DOC170",
                Severity.WARNING,
                f"pack {subject.name!r} could not be described: {e}",
                "An installed pack this cannot read is one nothing can check before it runs.",
            )
            continue

        drift = compare(read_record(root, subject.name), derived)
        if not drift.accepted:
            report.add(
                "DOC170",
                Severity.NOTE,
                f"pack {subject.name!r} is installed and was never accepted here",
                f"Ordinary until you bind it — installing offers a pack, it does not apply one. "
                f"`in-lockstep add {subject.name}` records what it may do.",
            )
        elif drift.widened:
            report.add(
                "DOC170",
                Severity.ERROR,
                f"pack {subject.name!r} may now do more than this repository accepted: "
                f"+{', +'.join(drift.widened)}",
                f"Read what changed, then accept it in a diff: "
                f"`in-lockstep add {subject.name} --accept`, and commit the record.",
            )
        elif drift.changes:
            report.add(
                "DOC170",
                Severity.WARNING,
                f"pack {subject.name!r} changed since it was accepted: {'; '.join(drift.changes)}",
                f"No new agency, so this is a note rather than a refusal. "
                f"`in-lockstep add {subject.name}` re-records it.",
            )

        state = pinning(root, subject.distribution)
        if state == "unpinned":
            report.add(
                "DOC172",
                Severity.WARNING,
                f"pack {subject.name!r} is installed but not pinned",
                "A receipt describes the code installed now; a pin is what makes that the code "
                "installed next time. Without one, capabilities were accepted for a range.",
            )


def _pack_guardrails(report: Report, lockstep: Any) -> None:
    """DOC171. A bound prompt whose stack no longer opens with the framework's baseline.

    Legal, and greppable, and exactly the thing a reader of somebody else's extension most needs
    told — so it warns rather than refuses, and it reads the BOUND adapters rather than the
    installed packs, because what matters is the prompt a run would actually send.
    """
    from .receipt import receipt_for

    try:
        derived = receipt_for(lockstep, root=Path(lockstep.repo.root))
    except Exception:  # pragma: no cover - defensive
        return
    for prompt in derived["prompts"]:
        if not prompt["guardrails_intact"]:
            report.add(
                "DOC171",
                Severity.WARNING,
                f"{prompt['label']} does not open with the shipped guardrail baseline",
                f"Its stack starts {prompt['projection'][0] if prompt['projection'] else '(empty)'}. "
                f"Constructing a fresh PromptLayers replaces the baseline; `plus()` appends and "
                f"keeps it. `in-lockstep show-prompt {prompt['label']} --diff` shows the change.",
            )
