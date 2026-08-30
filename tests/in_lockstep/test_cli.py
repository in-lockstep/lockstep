"""The command line, which had no tests at all until this file.

`cli.py` is the largest module in the package and the only one every user touches. It is also the
composition root: which `Lockstep` gets built, whether the repository's own module is loaded, and
which bindings survive are all decided here. None of that was asserted, and the gap showed —
`review`, the one command that spends money, built its own `Lockstep` from scratch and silently
discarded every binding, budget, policy contribution and model route the module declared.

`--dry-run` and `--offline` exist precisely so this file can be written without a key or a cent.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.cli import main

ROOT_REPO = Path(__file__).resolve().parents[2]

MODULE = """
from in_lockstep import Lockstep, Policy
from in_lockstep.core.spend import Budget

lockstep = Lockstep.detect()
lockstep.budget = Budget(usd={budget})
lockstep.contribute(Policy(name="repo", source="test", max_turns={turns}))
lockstep.models.route("review", "{model}")
"""


DIFF = """diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 x = 1
+y = 2
"""


def _diff(root: Path) -> str:
    """A real patch on disk.

    These tests are about the composition root — which module loaded, which budget bound, which
    egress policy resolved — and they used to run `review` against an EMPTY diff, which now
    refuses. That refusal is the point: a review with nothing to look at was answering anyway, and
    a canned `{"findings": []}` would have read as a clean review of nothing.
    """
    path = root / "change.diff"
    path.write_text(DIFF)
    return str(path)


def _ledger_record(repo: Path, prefix: str) -> Path:
    """The newest ledger record for a run id prefix.

    Run ids carry a per-invocation stamp — a re-run must append a second record rather than
    silently replace the first — so tests find records the way `history --explain` does: by the
    prefix a person would type.
    """
    matches = sorted((repo / ".lockstep/ledger").glob(f"{prefix}-*.json"), key=lambda p: p.stat().st_mtime_ns)
    assert matches, f"no ledger record starting {prefix!r}"
    return matches[-1]


def _lifecycle(root: Path) -> Path:
    """Where the lifecycle module lives: `.lockstep/lockstep.py`, never the repository root.

    The root is on `sys.path` for anything run from there, so a module named `lockstep` sitting in
    it is importable by the project whether or not anyone meant it to be. A dot-directory is not a
    valid package name, which is what makes this location safe rather than merely tidy.
    """
    path = root / ".lockstep" / "lockstep.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def _no_registry_leakage() -> Iterator[None]:
    """Both registries are process-global, so what one test defines outlives it.

    `workflow.clear()` is the wrong tool: the framework's own `selfcheck` is registered when `cli`
    is imported, so clearing after a test removes it for every later test. Restoring a snapshot
    removes what the test added and leaves what it found.
    """
    from in_lockstep.core.verbs import Verb
    from in_lockstep.core.workflow import restore, snapshot

    state = snapshot()
    yield
    Verb.forget_custom()
    restore(state)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory with its own lockstep.py, loaded from the working tree.

    Not a git repository, deliberately: with no ref to resolve, `config_ref` treats the working
    tree as the trusted source, which is the local-development path and the one under test here.
    """
    monkeypatch.chdir(tmp_path)
    # EVERY GitHub variable, not the two that happened to matter when this was written.
    #
    # `GITHUB_WORKSPACE` was the one missed, and the consequence was not a wrong assertion: it is
    # what `Lockstep.detect()` prefers over the working directory, so on a runner these tests
    # resolved to the REAL checkout. `test_apply_opens_a_change_on_a_run_scoped_branch` created a
    # branch and tried to commit in it, and a later test saw the repository it had left dirty.
    # A test that can reach outside its tmp_path is a test that can corrupt the thing it runs in.
    for name in [k for k in os.environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write(
    repo: Path,
    *,
    budget: float = 5.00,
    turns: int = 9,
    model: str = "anthropic:claude-haiku-4-5",
) -> None:
    _lifecycle(repo).write_text(MODULE.format(budget=budget, turns=turns, model=model))


def test_review_loads_the_repositorys_own_module(repo: Path) -> None:
    """The headline: `lockstep.py` is the configuration, including for the command that spends."""
    _write(repo)
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD", "--diff", _diff(repo)])
    assert result.exit_code == 0, result.output
    # The module routes review at a model the CLI's own default would not have chosen. A route
    # nothing reads is how `Models.route` shipped: written by the config, consumed by nobody.
    assert "haiku" in _ledger_record(repo, "review-security").read_text()


def test_an_untyped_model_flag_does_not_outrank_a_declared_route(repo: Path) -> None:
    _write(repo, model="google:gemini-2.5-flash")
    CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD", "--diff", _diff(repo)])
    assert "gemini-2.5-flash" in _ledger_record(repo, "review-security").read_text()


def test_review_comment_upserts_a_sticky_pr_comment(repo: Path, monkeypatch) -> None:
    """`review --comment --pr N` renders the findings into one sticky PR comment via the SCM."""
    posted: dict = {}

    async def fake_upsert(self, target, body, marker):  # noqa: ANN001
        posted["target"], posted["body"], posted["marker"] = target, body, marker

    monkeypatch.setattr("in_lockstep.platform.scm.GitHubScm.upsert_comment", fake_upsert)
    _write(repo)
    result = CliRunner().invoke(
        main, ["review", "--dry-run", "--base", "HEAD", "--diff", _diff(repo), "--comment", "--pr", "17"]
    )
    assert result.exit_code == 0, result.output
    assert posted["target"] == 17
    assert "in-lockstep review" in posted["body"]
    assert "in-lockstep:review:security" in posted["marker"]
    assert "posted to PR #17" in result.output


def test_a_failed_comment_post_does_not_crash_a_successful_review(repo: Path, monkeypatch) -> None:
    """Posting is the last, least-essential step: a gh timeout (SubprocessError, not caught by a
    naive RuntimeError guard) must be reported, not turned into a traceback."""
    import subprocess

    async def boom(self, target, body, marker):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd="gh", timeout=60)

    monkeypatch.setattr("in_lockstep.platform.scm.GitHubScm.upsert_comment", boom)
    _write(repo)
    result = CliRunner().invoke(
        main, ["review", "--dry-run", "--base", "HEAD", "--diff", _diff(repo), "--comment", "--pr", "5"]
    )
    assert result.exit_code == 0, result.output
    assert "could not post" in result.output


def test_review_comment_without_a_pr_is_a_note_not_a_failure(repo: Path, monkeypatch) -> None:
    for var in [k for k in os.environ if k.startswith("GITHUB_")]:
        monkeypatch.delenv(var, raising=False)
    _write(repo)
    result = CliRunner().invoke(
        main, ["review", "--dry-run", "--base", "HEAD", "--diff", _diff(repo), "--comment"]
    )
    assert result.exit_code == 0, result.output
    assert "no PR number" in result.output


def _issue_file(root: Path) -> str:
    import json

    path = root / "issue.json"
    path.write_text(
        json.dumps(
            {
                "input": {
                    "key": "#412",
                    "summary": "Checkout returns 500 for every card payment",
                    "description": "Since the 14:20 deploy every card payment returns a 500.",
                    "criteria_source": "none",
                    "discussion": [{"author": "ops", "body": "Confirmed on prod."}],
                }
            }
        )
    )
    return str(path)


def test_triage_runs_end_to_end_from_a_file(repo: Path) -> None:
    """The whole point of the vertical: an issue in, a placed decision out, a ledger line written —
    with no key and no spend."""
    result = CliRunner().invoke(
        main, ["triage", "--ticket-file", _issue_file(repo), "--dry-run", "--budget", "0.10"]
    )
    assert result.exit_code == 0, result.output
    assert "triage    succeeded" in result.output
    record = _ledger_record(repo, "triage-412").read_text()
    assert '"kind": "triage"' in record


def test_triage_reads_the_corpus_shape_directly(repo: Path) -> None:
    """A user can point `--ticket-file` at an eval-corpus case, so the offline surface and the
    graded surface are the same file."""
    import json

    corpus = Path(__file__).resolve().parents[2] / "src/in_lockstep/corpus/triage/triage-analyst"
    case = json.loads((corpus / "production-outage.json").read_text())
    assert "input" in case, "the corpus shape carries the issue under `input`"
    result = CliRunner().invoke(
        main,
        ["triage", "--ticket-file", str(corpus / "production-outage.json"), "--dry-run", "--budget", "0.10"],
    )
    assert result.exit_code == 0, result.output


def test_triage_refuses_both_or_neither_ticket_source(repo: Path) -> None:
    result = CliRunner().invoke(main, ["triage", "--dry-run", "--budget", "0.10"])
    assert result.exit_code != 0
    assert "exactly one of --ticket or --ticket-file" in result.output


def test_triage_ledger_path_survives_a_slash_bearing_key(repo: Path) -> None:
    """A GitLab-style `group/project#42` key must not write the record to a nested path the
    ledger's read glob never finds. The store sanitises the run id, so it lands one file deep."""
    import json

    path = repo / "issue.json"
    path.write_text(json.dumps({"key": "group/project#42", "summary": "x", "description": "y"}))
    result = CliRunner().invoke(main, ["triage", "--ticket-file", str(path), "--dry-run", "--budget", "0.10"])
    assert result.exit_code == 0, result.output
    ledger = repo / ".lockstep/ledger"
    written = list(ledger.glob("*.json"))
    assert written, "the record must be readable at the top level, not buried in a nested path"
    assert not (ledger / "triage-group").exists(), "no nested directory from the slash"


def test_triage_from_json_derives_criteria_like_a_live_ticket(repo: Path) -> None:
    """A JSON dump of an issue whose body carries a task list triages the same as the issue read
    from the tracker — criteria come from the body, and the source is not falsely 'none'."""
    import json

    from in_lockstep.cli import _triage_spec_from_dict

    data = json.loads('{"key": "#5", "description": "Fix it.\\n\\n- [ ] returns 200\\n- [ ] logs the id"}')
    spec = _triage_spec_from_dict(data, fallback_key="5")
    assert spec.acceptance_criteria == ("returns 200", "logs the id")
    assert spec.criteria_source == "description", "criteria came from the body, so the source is not none"


def test_triage_uses_the_declared_route_over_the_cli_default(repo: Path) -> None:
    """`Models.route('triage', ...)` in the module wins over the command's own default model."""
    _lifecycle(repo).write_text(
        "from in_lockstep import Budget, Lockstep\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.0)\n"
        "lockstep.models.route('triage', 'anthropic:claude-sonnet-4-6')\n"
    )
    CliRunner().invoke(main, ["triage", "--ticket-file", _issue_file(repo), "--dry-run"])
    assert "claude-sonnet-4-6" in _ledger_record(repo, "triage-412").read_text()


def test_an_explicit_model_flag_does_outrank_it(repo: Path) -> None:
    """An override the user actually typed is an override; a default is not."""
    _write(repo, model="google:gemini-2.5-flash")
    CliRunner().invoke(
        main,
        [
            "review",
            "--dry-run",
            "--base",
            "HEAD",
            "--model",
            "anthropic:claude-opus-4-6",
            "--diff",
            _diff(repo),
        ],
    )
    assert "opus" in _ledger_record(repo, "review-security").read_text()


def test_no_module_still_runs_on_detected_defaults(repo: Path) -> None:
    """A repository without a lockstep.py is supported, not an error.

    It still has to state a ceiling, because `review` binds something that spends. That is
    GATE-BUDGET-1 rather than a gap in the fallback: the alternative is the CLI inventing a
    number, which is the failure the gate exists to prevent.
    """
    result = CliRunner().invoke(
        main, ["review", "--dry-run", "--base", "HEAD", "--budget", "1.00", "--diff", _diff(repo)]
    )
    assert result.exit_code == 0, result.output


def test_telemetry_says_when_the_cli_cannot_see_the_chain(repo: Path) -> None:
    """`spans 0` for a run that emitted spans elsewhere is a wrong number, not a missing one."""
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.middleware import otel\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n"
        "lockstep.middleware += [otel()]\n"
    )
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD", "--diff", _diff(repo)])
    assert "the CLI is not in that chain" in result.output
    assert "spans     0" not in result.output


def test_ls_reads_the_same_module_review_does(repo: Path) -> None:
    """`ls` answers "what will actually run". It is wrong the moment a command disagrees with it."""
    _write(repo, turns=3)
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "repo" in result.output


def test_run_refuses_an_unregistered_workflow_by_name(repo: Path) -> None:
    result = CliRunner().invoke(main, ["run", "nope"])
    assert result.exit_code != 0
    assert "selfcheck" in result.output


# -- init: the first thing a new adopter runs ------------------------------------------------


def test_init_scaffolds_commands_that_exist(repo: Path) -> None:
    """The failure that shipped: the scaffold invoked `run review --base`, and neither exists.

    `run` accepts only `selfcheck` and declares no `--base`, so the workflow every new adopter
    committed failed on their first pull request. A scaffold is the one artifact that must not
    describe a CLI other than the one it ships with, because nobody reads it before running it.
    """
    import re

    import yaml

    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())

    known = set(main.commands)
    invoked = set()
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            for verb in re.findall(r"in-lockstep ([a-z-]+)", step.get("run", "") or ""):
                invoked.add(verb)
    assert invoked, "the scaffold invokes no CLI command at all"
    assert invoked <= known, f"scaffold invokes {sorted(invoked - known)}, which do not exist"


def test_the_scaffolded_review_passes_only_options_review_declares(repo: Path) -> None:
    import re

    CliRunner().invoke(main, ["init"])
    text = (repo / ".github/workflows/lockstep.yml").read_text()
    declared = {o for p in main.commands["review"].params for o in p.opts}
    used = set(re.findall(r"(--[a-z-]+)", text.split("in-lockstep review")[1].split("env:")[0]))
    assert used <= declared, f"scaffold passes {sorted(used - declared)} to review"


def test_the_scaffold_uploads_a_path_something_writes(repo: Path) -> None:
    """It pointed at `.lockstep/out/`, which no code path in the package ever creates."""
    import yaml

    CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    paths = [
        s["with"]["path"]
        for j in workflow["jobs"].values()
        for s in j["steps"]
        if "upload-artifact" in str(s.get("uses", ""))
    ]
    assert paths == [".lockstep/"], paths


def test_the_scaffold_carries_a_timeout(repo: Path) -> None:
    """Without one the CI default is 360 minutes, and there is no other wall clock in the job."""
    import yaml

    CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    assert all("timeout-minutes" in j for j in workflow["jobs"].values())


def test_init_reflects_a_node_stack_instead_of_assuming_pytest(repo: Path) -> None:
    """A Node repo's scaffold must bind a command runner, not pytest — the drop-in failure the
    detection closes."""
    (repo / "package.json").write_text('{"scripts": {"test": "jest"}}')
    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    module = (repo / ".lockstep/lockstep.py").read_text()
    assert "CommandTest" in module and "PytestTest" not in module
    assert "npm" in module
    compile(module, "lockstep.py", "exec")


def test_init_on_a_python_repo_is_the_familiar_pytest_scaffold(repo: Path) -> None:
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n[tool.ruff]\n")
    CliRunner().invoke(main, ["init"])
    module = (repo / ".lockstep/lockstep.py").read_text()
    assert "lockstep.bind(Test, PytestTest" in module
    assert "lockstep.bind(Validate, RuffValidate" in module
    compile(module, "lockstep.py", "exec")


def test_init_leaves_a_commented_stub_for_an_undetected_verb(repo: Path) -> None:
    """An empty directory gets no default that runs unbidden, and no unused import — the module
    still compiles with both deterministic verbs as self-contained commented stubs."""
    result = CliRunner().invoke(main, ["init"])
    module = (repo / ".lockstep/lockstep.py").read_text()
    assert "No test runner was detected" in module
    assert "No linter was detected" in module
    assert "lockstep.bind(Test," not in module.replace("#   lockstep.bind(Test,", "")
    compile(module, "lockstep.py", "exec")
    assert result.exit_code == 0


def test_ls_reports_what_detection_found(repo: Path) -> None:
    (repo / "package.json").write_text('{"scripts": {"test": "jest"}}')
    result = CliRunner().invoke(main, ["ls"])
    assert "detected" in result.output
    assert "stack: node" in result.output


def test_the_scaffold_states_the_measured_budget_not_the_guessed_one(repo: Path) -> None:
    """lockstep.yml raised its ceiling from $0.25 after a real run showed what $0.25 was sized
    for — it refused the runs most worth reviewing. The scaffold had kept the old number, so a
    fresh adopter's first large pull request refused where this repository's would not."""
    CliRunner().invoke(main, ["init"])
    text = (repo / ".github/workflows/lockstep.yml").read_text()
    assert "--budget 0.75" in text
    assert "--budget 0.25" not in text


def test_the_scaffold_asserts_no_egress_mode_a_hosted_runner_disproves(repo: Path) -> None:
    """`IN_LOCKSTEP_EGRESS: enforced` on a GitHub-hosted runner fails the probe — the runner
    reaches the open internet — so the scaffold refused every adopter's first hosted review.
    The variable belongs where a host actually constrains egress, and the scaffold says so in a
    comment rather than asserting a mode the environment disproves."""
    CliRunner().invoke(main, ["init"])
    for line in (repo / ".github/workflows/lockstep.yml").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "IN_LOCKSTEP_EGRESS" not in stripped


def test_the_scaffold_pins_what_runs_next_to_the_credential(repo: Path) -> None:
    """The framework by version, the actions by SHA. An unpinned install feeds whatever the
    registry serves next to the job holding the provider key — release-python.yml already pins
    by SHA for exactly this reason, and the scaffold had drifted from it."""
    import re

    from in_lockstep import __version__

    CliRunner().invoke(main, ["init"])
    text = (repo / ".github/workflows/lockstep.yml").read_text()
    assert f"in-lockstep[anthropic]=={__version__}" in text
    assert "IN_LOCKSTEP_VERSION" not in text, "the placeholder must not survive init"
    for used in re.findall(r"uses: (\S+)", text):
        action, _, ref = used.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{action} is pinned by {ref!r}, not by SHA"


def test_the_scaffold_skips_rather_than_fails_without_a_credential(repo: Path) -> None:
    """A pull request from a fork gets no secrets, and a red check the contributor cannot fix
    teaches everyone to ignore red checks."""
    import yaml

    CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    review = next(s for s in workflow["jobs"]["review"]["steps"] if s.get("name") == "Review")
    # `secrets` context, not `env`: it reads in a step `if` and keeps the key scoped to this one
    # step rather than exposing it to every step in the job.
    assert "secrets.ANTHROPIC_API_KEY" in review.get("if", ""), "the review step must guard on the credential"
    assert "env" in review and "ANTHROPIC_API_KEY" in review["env"], (
        "the key belongs on the step, not the job"
    )


def test_init_implement_scaffolds_the_credential_split(repo: Path) -> None:
    """The /implement flow used to require reverse-engineering this repository's own trampoline.

    The property that must survive parameterization: no job holds a provider key and write
    access, and the writing job does not even install a provider SDK."""
    import yaml

    result = CliRunner().invoke(main, ["init", "--implement"])
    assert result.exit_code == 0, result.output
    workflow = yaml.safe_load((repo / ".github/workflows/implement.yml").read_text())
    jobs = workflow["jobs"]
    assert set(jobs) == {"gate", "implement", "propose"}
    for name, job in jobs.items():
        text = yaml.dump(job)
        holds_key = "ANTHROPIC_API_KEY" in text
        writes = (job.get("permissions") or {}).get("contents") == "write"
        assert not (holds_key and writes), f"{name} holds a provider key and write access"
    assert "anthropic" not in yaml.dump(jobs["propose"]).lower()


def test_init_implement_extends_the_module_and_it_loads(repo: Path, monkeypatch) -> None:
    """Not just parses — loads, binds, and registers. This is the parity guard against the
    scaffolded module drifting from the framework it is generated by: if a rename breaks the
    appended block, the load fails here instead of on an adopter's first run."""
    from in_lockstep.adapters.pytest_adapter import Test
    from in_lockstep.core.workflow import get, restore, snapshot
    from in_lockstep.loader import load, lockstep_from
    from in_lockstep.platform.scm import Scm
    from in_lockstep.platform.tickets import TicketSource

    CliRunner().invoke(main, ["init", "--implement"])
    text = (repo / ".lockstep/lockstep.py").read_text()
    assert "implement/from-ticket" in text and "implement/propose" in text

    state = snapshot()
    try:
        module, _ref = load(str(repo))
        lockstep = lockstep_from(module)
        assert lockstep.container.has(Scm)
        assert lockstep.container.has(TicketSource)
        # Test is bound now too, so from-ticket can run the suite against the staged change.
        assert lockstep.container.has(Test)
        assert get("implement/from-ticket") is not None
        assert get("implement/propose") is not None
    finally:
        restore(state)


def test_init_fix_extends_the_module_and_it_loads(repo: Path) -> None:
    """The fix scaffold binds Fix and registers its two workflows, and its guarded binds mean it
    also loads standalone — Test, Scm and TicketSource are bound even without --implement."""
    from in_lockstep.adapters.ai.fix import Fix
    from in_lockstep.adapters.pytest_adapter import Test
    from in_lockstep.core.workflow import get, restore, snapshot
    from in_lockstep.loader import load, lockstep_from
    from in_lockstep.platform.scm import Scm
    from in_lockstep.platform.tickets import TicketSource

    CliRunner().invoke(main, ["init", "--fix"])
    text = (repo / ".lockstep/lockstep.py").read_text()
    assert "fix/from-ticket" in text and "fix/propose" in text

    state = snapshot()
    try:
        module, _ref = load(str(repo))
        lockstep = lockstep_from(module)
        assert lockstep.container.has(Fix)
        assert lockstep.container.has(Scm)
        assert lockstep.container.has(TicketSource)
        assert lockstep.container.has(Test)
        assert get("fix/from-ticket") is not None
        assert get("fix/propose") is not None
    finally:
        restore(state)


def test_init_fix_writes_the_ai_generated_event_hook(repo: Path) -> None:
    """The self-feeding half of the loop: an issue labelled `ai-generated` routes to the fix
    workflow. It fires on the label, gates on it (no comment-gate, because labelling is
    write-access), and keeps the same credential split — no job holds a provider key and write."""
    import yaml

    result = CliRunner().invoke(main, ["init", "--fix"])
    assert result.exit_code == 0, result.output
    workflow = yaml.safe_load((repo / ".github/workflows/ai-generated.yml").read_text())

    # `yaml.safe_load` turns the bare `on:` key into Python True, so read it by that key.
    triggers = workflow[True]["issues"]["types"]
    assert "labeled" in triggers and "opened" in triggers
    fix = workflow["jobs"]["fix"]
    assert "ai-generated" in fix["if"], "the job is gated on the label"
    assert set(workflow["jobs"]) == {"fix", "propose"}, "no gate job — the label is the gate"

    for name, job in workflow["jobs"].items():
        text = yaml.dump(job)
        holds_key = "ANTHROPIC_API_KEY" in text
        writes = (job.get("permissions") or {}).get("contents") == "write"
        assert not (holds_key and writes), f"{name} holds a provider key and write access"
    assert "anthropic" not in yaml.dump(workflow["jobs"]["propose"]).lower()


def test_init_implement_and_fix_compose_without_binding_twice(repo: Path) -> None:
    """Both scaffolds in one module load cleanly — the fix block's guarded binds do not re-bind
    what --implement already did, and both verbs' workflows register."""
    from in_lockstep.adapters.ai.fix import Fix
    from in_lockstep.adapters.ai.implement import Implement
    from in_lockstep.core.workflow import get, restore, snapshot
    from in_lockstep.loader import load, lockstep_from

    CliRunner().invoke(main, ["init", "--implement"])
    CliRunner().invoke(main, ["init", "--fix"])

    state = snapshot()
    try:
        module, _ref = load(str(repo))
        lockstep = lockstep_from(module)
        assert lockstep.container.has(Implement)
        assert lockstep.container.has(Fix)
        assert get("implement/from-ticket") is not None
        assert get("fix/from-ticket") is not None
        # One approval gate, not two: the fix block guards adding its own.
        gates = [m for m in lockstep.middleware if getattr(m, "provides_approval", False)]
        assert len(gates) == 1
    finally:
        restore(state)


def test_init_implement_will_not_clobber_an_unrecognised_module(repo: Path) -> None:
    """A module that never bound `lockstep` would take a NameError from the appended binds, so
    the append is refused rather than silently breaking a file we do not understand."""
    (repo / ".lockstep").mkdir(exist_ok=True)
    (repo / ".lockstep" / "lockstep.py").write_text("# hand-written, uses a different name\nls = None\n")
    result = CliRunner().invoke(main, ["init", "--implement"])
    assert result.exit_code == 0
    text = (repo / ".lockstep/lockstep.py").read_text()
    assert "implement/from-ticket" not in text, "must not append binds to a module it cannot verify"
    assert "not a recognisable lockstep module" in result.output


def test_init_implement_guard_is_a_parse_not_a_substring(repo: Path) -> None:
    """`# lockstep config` mentions the name in a comment but binds nothing — a substring guard
    would pass it, the append would compile, and it would NameError only at load."""
    (repo / ".lockstep").mkdir(exist_ok=True)
    (repo / ".lockstep" / "lockstep.py").write_text("# lockstep config, hand-written\nls = None\n")
    result = CliRunner().invoke(main, ["init", "--implement"])
    text = (repo / ".lockstep/lockstep.py").read_text()
    assert "implement/from-ticket" not in text
    assert "not a recognisable lockstep module" in result.output


def test_init_implement_is_idempotent(repo: Path) -> None:
    CliRunner().invoke(main, ["init", "--implement"])
    first = (repo / ".lockstep/lockstep.py").read_text()
    CliRunner().invoke(main, ["init", "--implement"])
    assert (repo / ".lockstep/lockstep.py").read_text() == first


def test_init_implement_invokes_only_commands_that_exist(repo: Path) -> None:
    import re

    import yaml

    CliRunner().invoke(main, ["init", "--implement"])
    workflow = yaml.safe_load((repo / ".github/workflows/implement.yml").read_text())
    known = set(main.commands)
    invoked = set()
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            for verb in re.findall(r"in-lockstep ([a-z-]+)", step.get("run", "") or ""):
                invoked.add(verb)
    assert invoked, "the scaffold invokes no CLI command at all"
    assert invoked <= known, f"scaffold invokes {sorted(invoked - known)}, which do not exist"


def test_the_trampoline_is_independent_of_the_repository(tmp_path: Path, monkeypatch) -> None:
    """Q4's condition: byte-identical in an empty directory and a full repository.

    A compiler cannot pass this. It binds the workflow file only — `init`'s lockstep.py scaffold
    may detect the stack freely.
    """
    outputs = []
    for name, populate in (("empty", False), ("full", True)):
        target = tmp_path / name
        target.mkdir()
        if populate:
            (target / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
            (target / "tests").mkdir()
        monkeypatch.chdir(target)
        CliRunner().invoke(main, ["init"])
        outputs.append((target / ".github/workflows/lockstep.yml").read_text())
    assert outputs[0] == outputs[1]


def _git_repo(root: Path) -> None:
    """A real repository, because `open_change` makes a branch and commits to it."""
    import subprocess

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, capture_output=True, check=True)

    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "base")
    run("git", "branch", "-M", "main")


def _staged(root: Path, path: str = "src/x.py") -> str:
    import json

    target = root / "artifact"
    target.mkdir(exist_ok=True)
    (target / "changeset.json").write_text(
        json.dumps(
            {
                "summary": "add x",
                "ticket": "#1",
                "changes": [{"path": path, "contents": "x = 1\n", "author": "agent"}],
            }
        )
    )
    return str(target)


def test_apply_dry_run_checks_the_guard_and_writes_nothing(repo: Path) -> None:
    result = CliRunner().invoke(main, ["apply", "--from-artifact", _staged(repo), "--dry-run"])
    assert result.exit_code == 0
    assert "nothing was written" in result.output
    assert not (repo / "src" / "x.py").exists()


def test_apply_opens_a_change_on_a_run_scoped_branch(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #59. `apply` checked the guard and then wrote nothing, which made the privileged
    half of the two-job split a job with nothing to do."""
    import subprocess

    from in_lockstep.platform.scm import RUN_BRANCH_PREFIX

    _git_repo(repo)
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    result = CliRunner().invoke(main, ["apply", "--from-artifact", _staged(repo)])
    assert result.exit_code == 0, result.output

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert branch == f"{RUN_BRANCH_PREFIX}/implement/1/42", "the staged ticket (#1) is a segment"
    assert (repo / "src" / "x.py").read_text() == "x = 1\n"

    message = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert "Ticket: #1" in message, "the trailer is what survives every migration"


def test_apply_refuses_a_protected_path_before_it_writes(repo: Path) -> None:
    _git_repo(repo)
    result = CliRunner().invoke(main, ["apply", "--from-artifact", _staged(repo, "lockstep.py")])
    assert result.exit_code == 3, result.output
    assert "refused" in result.output


def test_apply_refuses_to_run_beside_a_provider_credential(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two-job split asserted about this process, not about the YAML that started it.

    A check that lives only as a comment in a workflow file is one a copied workflow loses.
    """
    _git_repo(repo)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key-but-long-enough")
    result = CliRunner().invoke(main, ["apply", "--from-artifact", _staged(repo)])
    assert result.exit_code != 0
    assert "must not" in result.output and "write" in result.output


def test_init_does_not_announce_a_job_it_did_not_write(repo: Path) -> None:
    """It described a two-job split while scaffolding one job. Prose drifts; this notices."""
    import yaml

    result = CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    for job in ("run", "apply"):
        if job not in workflow["jobs"]:
            assert f"`{job}` holds" not in result.output


def test_ls_surfaces_a_verb_nothing_serves(repo: Path) -> None:
    """Verbs are open, so one can exist that the bindings block never mentions.

    That is the shape a typo takes: `Verb("reviwe")` is a legitimate verb nothing serves, and
    without this it is invisible until work silently fails to route.
    """
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.verbs import Verb\n"
        "lockstep = Lockstep.detect()\n"
        "Verb('reviwe')\n"
    )
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "verbs defined but unbound" in result.output
    assert "reviwe" in result.output


def test_ls_prints_the_model_routes(repo: Path) -> None:
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.models.route('triage', 'local:qwen3-8b')\n"
        "lockstep.models.route('review', 'bedrock:claude-sonnet-4-6')\n"
    )
    result = CliRunner().invoke(main, ["ls"])
    assert "models" in result.output
    assert "triage" in result.output and "local:qwen3-8b" in result.output
    assert "bedrock:claude-sonnet-4-6" in result.output


def test_ls_flags_a_route_to_a_verb_that_does_not_exist(repo: Path) -> None:
    """A route keyed on a typo'd verb names a verb nothing serves; ls says so rather than letting
    it read as configured."""
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.models.route('reviwe', 'anthropic:claude-sonnet-4-6')\n"
    )
    result = CliRunner().invoke(main, ["ls"])
    assert "no such verb" in result.output


def test_ls_stays_quiet_about_unbound_shipped_verbs(repo: Path) -> None:
    """Seven of nine ship unbound in a default install. Printing them buries the signal."""
    _write(repo)
    result = CliRunner().invoke(main, ["ls"])
    assert "verbs defined but unbound" not in result.output
    for shipped in ("triage", "debug", "implement"):
        assert shipped not in result.output


# -- apply: the guard over an artifact that crossed a trust boundary --------------------------
#
# `tests/in_lockstep/test_controls.py` tests `ChangeGuard` directly. Nothing asserted it runs
# *here*, which is the one place it defends a boundary rather than a data structure: the artifact
# was produced by a different job, and a previous job having produced it is not a reason to trust
# it. This is the one of GATE-GUARD-1's three named paths that exists.


def _artifact(tmp_path: Path, *changes: dict) -> Path:
    import json

    payload = tmp_path / "changeset.json"
    payload.write_text(json.dumps({"changes": list(changes), "summary": "s"}))
    return payload


@pytest.mark.parametrize(
    "protected",
    [
        "lockstep.py",
        ".lockstep/ledger/x.json",
        ".github/workflows/ci.yml",
        ".git/hooks/pre-commit",
        "pyproject.toml",
        "conftest.py",
        "tests/conftest.py",
        "CODEOWNERS",
        ".env",
        "deploy/secrets.pem",
    ],
)
def test_apply_refuses_a_protected_path_from_an_artifact(tmp_path: Path, protected: str) -> None:
    payload = _artifact(tmp_path, {"path": protected, "contents": "x", "author": "agent"})
    result = CliRunner().invoke(main, ["apply", "--from-artifact", str(payload), "--dry-run"])
    assert result.exit_code == 3, f"{protected} was not refused: {result.output}"
    assert "refused" in result.output
    assert protected in result.output


def test_apply_allows_an_ordinary_source_path(tmp_path: Path) -> None:
    """The guard has to permit the thing the framework exists to do."""
    payload = _artifact(tmp_path, {"path": "src/app/orders.py", "contents": "x", "author": "agent"})
    result = CliRunner().invoke(main, ["apply", "--from-artifact", str(payload), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "1 change(s) pass the guard" in result.output


def test_apply_refuses_a_path_escaping_the_repository(tmp_path: Path) -> None:
    payload = _artifact(tmp_path, {"path": "../outside.py", "contents": "x", "author": "agent"})
    result = CliRunner().invoke(main, ["apply", "--from-artifact", str(payload), "--dry-run"])
    assert result.exit_code == 3, result.output


def test_apply_reports_a_missing_artifact_rather_than_traceback(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["apply", "--from-artifact", str(tmp_path / "nope.json")])
    assert result.exit_code != 0
    assert "no changeset at" in result.output


def test_apply_accepts_a_directory_as_well_as_a_file(tmp_path: Path) -> None:
    """The scaffolded job downloads an artifact directory, not a file."""
    _artifact(tmp_path, {"path": "src/ok.py", "contents": "x", "author": "agent"})
    result = CliRunner().invoke(main, ["apply", "--from-artifact", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output


# -- the offline commands, which exist so this is inspectable without a key -------------------


def test_show_prompt_renders_with_provenance(repo: Path) -> None:
    result = CliRunner().invoke(main, ["show-prompt", "security"])
    assert result.exit_code == 0, result.output
    assert "guardrail:baseline" in result.output


def test_show_prompt_names_the_lenses_it_has(repo: Path) -> None:
    result = CliRunner().invoke(main, ["show-prompt", "nonsense"])
    assert result.exit_code != 0
    assert "security" in result.output


def test_eval_report_does_not_call_an_unjudged_rubric_a_pass(repo: Path) -> None:
    result = CliRunner().invoke(main, ["eval", "report"])
    assert result.exit_code == 0, result.output
    assert "outstanding" in result.output
    assert "n/a — nothing decided" in result.output


def test_eval_list_names_every_case(repo: Path) -> None:
    result = CliRunner().invoke(main, ["eval", "list"])
    assert result.exit_code == 0, result.output
    assert "27 case(s)" in result.output


def test_eval_reports_a_missing_corpus_rather_than_zero_cases(repo: Path) -> None:
    result = CliRunner().invoke(main, ["eval", "report", "--corpus", str(repo / "absent")])
    assert result.exit_code != 0
    assert "no corpus at" in result.output


def test_doctor_runs_and_reports_findings(repo: Path) -> None:
    result = CliRunner().invoke(main, ["doctor"])
    assert "finding(s)" in result.output


def test_run_selfcheck_dispatches_both_verbs(repo: Path) -> None:
    """A module is the whole configuration, so it has to bind what it wants run.

    `_write`'s module deliberately binds nothing, which is why this one scaffolds instead: a
    lockstep.py that declares a budget and no adapters resolves nothing, and that is correct —
    the fallback to detected defaults applies when there is no module at all, not when there is
    one that came out empty.
    """
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    (repo / "sample.py").write_text("x = 1\n")
    result = CliRunner().invoke(main, ["run", "selfcheck", "--paths", str(repo)])
    assert "validate" in result.output
    assert "test" in result.output
    assert "spend" in result.output


def test_the_killswitch_halts_before_any_adapter(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE-ASYNC-3, through the CLI: the flag must beat the chain, not sit inside it."""
    # A detectable stack, so the scaffold binds a Test for the killswitch to halt before — an
    # empty directory now binds nothing, and there would be no adapter to prove the flag beats.
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n[tool.ruff]\n")
    CliRunner().invoke(main, ["init"])
    monkeypatch.setenv("IN_LOCKSTEP_DISABLE", "1")
    result = CliRunner().invoke(main, ["run", "selfcheck", "--paths", str(repo)])
    assert result.exit_code == 3, result.output


def test_review_refuses_a_repo_that_declares_no_budget(repo: Path) -> None:
    """GATE-BUDGET-1 through the CLI, which is where a person meets it.

    `--budget` deliberately has no default. A flag that silently supplies a ceiling would make
    this unsatisfiable in the one place it matters: every run would have a budget nobody chose,
    and the refusal could never fire.
    """
    _lifecycle(repo).write_text("from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n")
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD", "--diff", _diff(repo)])
    assert result.exit_code != 0
    assert "no budget is declared" in result.output
    assert "Traceback" not in result.output, "a refusal is a message, not a crash"


def test_an_explicit_budget_flag_satisfies_it(repo: Path) -> None:
    _lifecycle(repo).write_text("from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n")
    result = CliRunner().invoke(
        main, ["review", "--dry-run", "--base", "HEAD", "--budget", "0.50", "--diff", _diff(repo)]
    )
    assert result.exit_code == 0, result.output


def test_ls_still_works_without_a_budget(repo: Path) -> None:
    """The diagnostic that tells you what is bound must survive the refusal that mentions it.

    `ls` never opens a run, so it does not trip the startup check — which is what lets someone
    read the error, run `ls`, and see the adapter it named.
    """
    _lifecycle(repo).write_text("from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n")
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "bindings" in result.output


def test_the_scaffolded_module_satisfies_the_check(repo: Path) -> None:
    """`init` must not scaffold something that refuses to run."""
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    result = CliRunner().invoke(main, ["run", "selfcheck", "--paths", str(repo)])
    assert "no budget is declared" not in result.output


def test_apply_refuses_a_test_deletion_without_a_ticket(tmp_path: Path) -> None:
    """GATE-TESTGUARD-1 on the enforcement path that exists."""
    payload = _artifact(tmp_path, {"path": "tests/test_orders.py", "author": "agent"})
    result = CliRunner().invoke(main, ["apply", "--from-artifact", str(payload), "--dry-run"])
    assert result.exit_code == 3, result.output
    assert "test-deleted-without-ticket" in result.output


def test_apply_allows_a_test_deletion_that_names_a_ticket(tmp_path: Path) -> None:
    import json

    payload = tmp_path / "changeset.json"
    payload.write_text(
        json.dumps(
            {
                "changes": [{"path": "tests/test_orders.py", "author": "agent"}],
                "ticket": "PROJ-12",
                "summary": "s",
            }
        )
    )
    result = CliRunner().invoke(main, ["apply", "--from-artifact", str(payload), "--dry-run"])
    assert result.exit_code == 0, result.output


def test_apply_reads_the_working_tree_to_tell_an_added_skip_from_an_existing_one(
    repo: Path, tmp_path: Path
) -> None:
    """The reader is what makes the rule exact rather than merely safe.

    `apply` runs with the repository checked out, which is the one place the pre-change content
    is available — so a change that merely edits a file which already had a skip is allowed.
    """
    existing = "@pytest.mark.skip\ndef test_x(): ...\n"
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text(existing)

    payload = _artifact(
        tmp_path,
        {
            "path": "tests/test_x.py",
            "contents": existing + "\ndef test_y():\n    assert True\n",
            "author": "agent",
        },
    )
    result = CliRunner().invoke(main, ["apply", "--from-artifact", str(payload), "--dry-run"])
    assert result.exit_code == 0, result.output


def test_apply_still_refuses_a_newly_added_skip(repo: Path, tmp_path: Path) -> None:
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")

    payload = _artifact(
        tmp_path,
        {"path": "tests/test_x.py", "contents": "@pytest.mark.skip\ndef test_x(): ...\n", "author": "agent"},
    )
    result = CliRunner().invoke(main, ["apply", "--from-artifact", str(payload), "--dry-run"])
    assert result.exit_code == 3, result.output
    assert "test-silenced-without-ticket" in result.output


def test_a_bound_egress_policy_is_the_one_that_runs(repo: Path) -> None:
    """The opt-out `egress.py`'s own refusal recommends, finally read.

    "or bind UnsandboxedEgress deliberately" appears in the refusal message, in ADR 0001 and in
    the controls crosswalk, and nothing resolved the binding — so the one escape hatch the design
    offers was a sentence. A control whose escape hatch nobody wired is a control people work
    around some other way.

    Asserted with a policy that REFUSES rather than one that permits, deliberately. A permissive
    binding proves nothing here: `--dry-run` sets `transmits=False`, which already lifts the
    untrusted-content trigger, so a passing run would be consistent with the binding being ignored.
    A refusing one can only be the bound object.
    """
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.privileged.egress import EgressPolicy, EgressRefused\n"
        "\n"
        "class AlwaysRefuse(EgressPolicy):\n"
        "    def check(self, **kw):\n"
        "        raise EgressRefused('egress.test_binding', 'the bound policy ran')\n"
        "\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n"
        "lockstep.bind(EgressPolicy, AlwaysRefuse())\n"
    )
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD", "--diff", _diff(repo)])
    assert "egress.test_binding" in result.output, result.output
    assert "the bound policy ran" in result.output


def test_the_unsandboxed_opt_out_permits_what_the_default_refuses(repo: Path) -> None:
    """The pair, so the opt-out and what it lifts cannot drift apart.

    `transmits=False` is why this is asserted through `EgressPolicy.required` rather than a run:
    the CLI's only offline providers suppress the trigger this opt-out exists to lift, so a
    command-level test would be green either way.
    """
    from in_lockstep.core.verbs import Capability
    from in_lockstep.privileged.egress import EgressMode, EgressPolicy, UnsandboxedEgress

    writes = frozenset({Capability.WRITES_FILES})
    default = EgressPolicy(mode=EgressMode.NONE)
    assert default.required(capabilities=writes, untrusted_context=True) is not None

    UnsandboxedEgress().check(capabilities=writes, untrusted_context=True)


def test_this_repository_opts_out_deliberately_and_says_so() -> None:
    """The weakening is meant to be legible. If the line moves, the reasoning must move with it."""
    module = (ROOT_REPO / ".lockstep" / "lockstep.py").read_text()
    if "UnsandboxedEgress" not in module:
        return  # the opt-out was removed, which is the other acceptable state
    assert "OPT-OUT" in module, "the binding is here without the paragraph explaining its cost"
    assert "IN_LOCKSTEP_EGRESS=enforced" in module, "it must say CI does not take this path"


def test_a_missing_provider_credential_is_a_message_not_a_typeerror(repo: Path) -> None:
    """The first thing a new adopter hits, and it used to be a library's TypeError.

    Anthropic's client raises "Could not resolve authentication method" from inside
    `messages.create` — accurate, and arriving as a traceback from a library the user did not
    call, after the budget check has passed and the run looks like it is working.
    """
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n"
        "lockstep.bind(EgressPolicy, UnsandboxedEgress())\n"
    )
    result = CliRunner().invoke(
        main,
        ["review", "--base", "HEAD", "--model", "anthropic:claude-haiku-4-5", "--diff", _diff(repo)],
    )
    assert result.exit_code != 0
    assert "no credential for provider 'anthropic'" in result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert "nothing was charged" in result.output.lower()
    assert "Traceback" not in result.output


def test_a_local_provider_needs_no_credential(repo: Path) -> None:
    """Ollama has no key, and demanding one would make the free path the awkward one."""
    from in_lockstep.ai.auth import Auth
    from in_lockstep.ai.bootstrap import credentials_for

    assert credentials_for(Auth(), "local").secret_values() == frozenset()


def test_the_ledger_records_why_not_only_that(repo: Path) -> None:
    """`status` says errored; `reason` says which kind, and only one of them was written.

    A run refused by a budget and a run rejected by a provider are both `errored`, and the
    terminal distinguished them while the durable record did not — so nothing downstream could
    group failures by the thing that differs.
    """
    import json

    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=0.0001)\n"
        "lockstep.bind(EgressPolicy, UnsandboxedEgress())\n"
    )
    # No `--diff`, so there is nothing to review. Any refusal proves the property this test is
    # about — that `reason` reaches the record and not only `status` — and this one is reachable
    # with no key. It used to use a budget refusal, which a dry run can no longer produce: a run
    # that cannot spend is no longer stopped by a spending ceiling. See the test below.
    CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
    record = json.loads(_ledger_record(repo, "review-security").read_text())
    assert record["status"] == "blocked"
    assert record["reason"] == "review.no_content", record


def test_a_run_that_cannot_spend_is_not_stopped_by_a_spending_ceiling(repo: Path) -> None:
    """`--offline` and `--dry-run` exist so this can be exercised with no key and no cent.

    A budget blocking them would make the free path the one that needs a budget argument — and the
    ceiling would be refusing a run it has nothing to protect.
    """
    import json

    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=0.0001)\n"
        "lockstep.bind(EgressPolicy, UnsandboxedEgress())\n"
    )
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD", "--diff", _diff(repo)])
    assert result.exit_code == 0, result.output
    assert "replayed; nothing was billed" in result.output

    record = json.loads(_ledger_record(repo, "review-security").read_text())
    assert record["cost_usd"] == 0.0
    # The number that stops this reading as a model whose price was never known.
    assert record["billed_fraction"] == 0.0
    assert record["tokens"] > 0, "a replay that reported no usage would not be a replay"


# -- the ledger keeps what was found, not only how much ---------------------------------------


def _reviewing_repo(repo: Path, *, findings: str) -> None:
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n"
        "lockstep.bind(EgressPolicy, UnsandboxedEgress())\n"
    )
    (repo / "canned.json").write_text(findings)


def test_the_ledger_stores_the_findings_themselves(repo: Path) -> None:
    """Three real findings once existed nowhere but a terminal scrollback.

    A record whose purpose is evidence kept the count and discarded the content, so the ledger
    could say a run found things without being able to say what they were.
    """
    import json

    from in_lockstep.core.outcome import Finding, Severity

    finding = Finding(
        id="review.security",
        message="Unquoted variable in `find` allows word-splitting.",
        severity=Severity.WARNING,
        path="actions/save/action.yml",
        line=29,
    )
    record = finding.as_record()
    assert record == {
        "id": "review.security",
        "message": "Unquoted variable in `find` allows word-splitting.",
        "severity": "warning",
        "blocking": False,
        "path": "actions/save/action.yml",
        "line": 29,
    }
    assert json.dumps(record), "a record has to survive serialization"


def test_an_absent_path_is_omitted_not_written_empty() -> None:
    """`path: ""` reads as a finding about the repository root. That is a different claim."""
    from in_lockstep.core.outcome import Finding

    record = Finding(id="cost.budget_exceeded", message="over").as_record()
    assert "path" not in record
    assert "line" not in record


def test_a_long_message_is_truncated_visibly() -> None:
    """A finding's message is model output, and a record is meant to stay diffable."""
    from in_lockstep.core.outcome import Finding

    record = Finding(id="x", message="y" * 5000).as_record()
    message = record["message"]
    assert isinstance(message, str)
    assert len(message) < 700
    assert message.endswith("…[truncated]")


def test_the_count_is_the_true_total_even_when_items_are_capped(repo: Path) -> None:
    """The mismatch is the signal: `count: 120, items: [50]` says so without a separate flag."""
    import asyncio as aio
    import json

    from in_lockstep.core.outcome import Finding
    from in_lockstep.platform.ledger.store import InRepoLedger

    many = [Finding(id=f"f{i}", message="m") for i in range(120)]
    ledger = InRepoLedger(root=repo / "l")
    aio.run(
        ledger.append(
            "r",
            {"findings": {"count": len(many), "items": [f.as_record() for f in many[:50]]}},
        )
    )
    stored = json.loads((repo / "l" / "r.json").read_text())["findings"]
    assert stored["count"] == 120
    assert len(stored["items"]) == 50


# -- the shipped replay fixture ----------------------------------------------------------------


def test_offline_works_with_nothing_recorded(repo: Path) -> None:
    """`--offline` is advertised as the free path, and needed a recording the user did not have.

    Both halves ship: the cassette, and the diff it was recorded against. A cassette is keyed on
    the whole composed prompt — which embeds the diff and the range — so a fixture without them
    would replay for nobody, because the key could never match anything a user actually holds.
    """
    _write(repo)
    result = CliRunner().invoke(main, ["review", "--offline"])
    assert result.exit_code == 0, result.output
    assert "replaying the shipped fixture" in result.output
    assert "actions/save/action.yml" in result.output


def test_the_fixture_is_a_real_recording(repo: Path) -> None:
    """It records a real model call against a real merged PR, not an authored stand-in.

    That distinction is the whole reason it took a real call to produce: a hand-written cassette
    is a fixture that looks like evidence, which is the failure this repository keeps finding.
    """
    import json

    from in_lockstep.cli import _shipped_fixture

    fixture = _shipped_fixture()
    assert fixture is not None
    assert fixture["base"] and fixture["head"]

    cassette = json.loads(Path(fixture["cassette"]).read_text())
    entry = next(iter(cassette["provider_calls"].values()))
    usage = entry["usage"]
    assert usage["input_tokens"] > 1000, "a canned answer would not have real token counts"
    assert usage["output_tokens"] > 0
    assert entry["stop_reason"] == "end_turn", "not truncated"


def test_the_fixture_carries_no_credential() -> None:
    """It was written through Redact, and it is committed. Both facts have to hold together."""
    from in_lockstep.cli import _shipped_fixture

    fixture = _shipped_fixture()
    assert fixture is not None
    raw = Path(fixture["cassette"]).read_text()
    for shape in ("sk-ant-", "ghp_", "Bearer ", "AKIA"):
        assert shape not in raw, f"the shipped cassette contains something shaped like {shape!r}"


def test_a_stale_fixture_says_so_rather_than_raising(repo: Path) -> None:
    """A prompt edit invalidates the key, and re-recording costs a real model call.

    So the failure has to name the cause. `LookupError: no cassette entry` on its own reads as a
    bug in replay rather than as "the prompt moved and this recording did not".
    """
    _write(repo)
    result = CliRunner().invoke(
        main,
        # A diff, because the refusal for having nothing to review now comes FIRST and this test is
        # about what happens when a cassette does not match — two different failures that used to
        # be one, since a review with no content still went to the provider.
        ["review", "--offline", "--base", "HEAD~1", "--head", "HEAD", "--diff", _diff(repo)],
    )
    assert result.exit_code != 0
    assert "no longer matches the prompt" in result.output or "no cassette entry" in result.output
    assert "Traceback" not in result.output


def test_the_scaffold_passes_a_workspace_id_as_a_variable(repo: Path) -> None:
    """An identity-linked key 400s without it, and the scaffold is what a new adopter commits.

    A variable rather than a secret, deliberately: a workspace id identifies, it does not
    authenticate, and putting one in `secrets` would seed `Redact` with it — masking it out of
    exactly the error messages that name it.
    """
    import yaml

    CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    # Job-level rather than step-level, so the fork-PR guard (`if: env.ANTHROPIC_API_KEY != ''`)
    # can read the same value the review step does.
    envs = [job.get("env", {}) for job in workflow["jobs"].values()] + [
        step.get("env", {}) for job in workflow["jobs"].values() for step in job["steps"]
    ]
    env = next(e for e in envs if "ANTHROPIC_API_KEY" in e)
    assert env["ANTHROPIC_WORKSPACE_ID"] == "${{ vars.ANTHROPIC_WORKSPACE_ID }}"
    assert env["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"


def test_the_scaffold_states_a_budget_for_the_adoption_case(repo: Path) -> None:
    """A repository's first pull request is the one that adds lockstep.py.

    Configuration loads from the trusted ref, so that PR has no config to load and no declared
    ceiling — and GATE-BUDGET-1 refuses. This repository hit it on its own pivot PR. The workflow
    file is base-ref content for a `pull_request` event, the same property that makes
    config-from-base safe, so a ceiling stated there is equally out of the PR's reach.
    """
    CliRunner().invoke(main, ["init"])
    text = (repo / ".github/workflows/lockstep.yml").read_text()
    assert "--budget" in text, "a repository adopting this cannot review its own adoption PR"


# -- implement ---------------------------------------------------------------------------------
#
# The second command that spends, and the first that can write and execute. What is asserted here
# is mostly the composition root rather than the strategy: which module was loaded, whether the
# controls that key off `AiImplement`'s capability declaration actually fire at the CLI boundary,
# and whether the change leaves as an artifact rather than as a write. The loop itself is tested
# against a scripted provider in `test_implement_oneshot.py`, where it costs no CLI plumbing.

IMPLEMENT_MODULE = """
from in_lockstep import Lockstep
from in_lockstep.core.spend import Budget
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress

lockstep = Lockstep.detect()
lockstep.budget = Budget(usd=5.00)
# The documented opt-out, named after what it does. An implementing tool set declares
# WRITES_FILES and EXECUTES_CODE, which makes egress enforcement mandatory — so on a laptop with
# an open network this line is what a repository writes instead of a flag, and it is greppable.
lockstep.bind(EgressPolicy, UnsandboxedEgress())
"""

TICKET = """# Add a greeting

The greeter should say hello.

- [ ] `greet()` returns a greeting
"""


def _ticket_file(repo: Path) -> str:
    (repo / "TICKET.md").write_text(TICKET)
    return str(repo / "TICKET.md")


def test_implement_needs_exactly_one_source_for_the_ticket(repo: Path) -> None:
    result = CliRunner().invoke(main, ["implement", "--dry-run", "--approve"])
    assert result.exit_code != 0
    assert "exactly one of --ticket or --ticket-file" in result.output


def test_implement_refuses_without_an_approval_path(repo: Path) -> None:
    """GATE-APPROVAL-1 at the CLI boundary. A model that can write needs a human somewhere."""
    _lifecycle(repo).write_text(IMPLEMENT_MODULE)
    result = CliRunner().invoke(main, ["implement", "--dry-run", "--ticket-file", _ticket_file(repo)])
    assert result.exit_code != 0
    assert "ApprovalGate" in result.output


def test_implement_refuses_when_egress_is_unenforced(repo: Path) -> None:
    """Declaring EXECUTES_CODE is what makes enforcement mandatory, and nothing opts out here."""
    result = CliRunner().invoke(
        main,
        ["implement", "--dry-run", "--approve", "--budget", "1.00", "--ticket-file", _ticket_file(repo)],
    )
    assert result.exit_code != 0
    assert "egress" in result.output
    assert "UnsandboxedEgress" in result.output, "the refusal has to name the way out"


def test_implement_runs_the_wiring_and_writes_a_ledger_record(repo: Path) -> None:
    """`--dry-run` stages nothing, so this exits non-zero and still proves the setup."""
    _lifecycle(repo).write_text(IMPLEMENT_MODULE)
    result = CliRunner().invoke(
        main, ["implement", "--dry-run", "--approve", "--ticket-file", _ticket_file(repo)]
    )
    assert "implement/oneshot" in result.output
    assert "implement.no_changes" in result.output
    record = (repo / ".lockstep/ledger/implement-Add a greeting.json").exists() or any(
        p.name.startswith("implement-") for p in (repo / ".lockstep/ledger").iterdir()
    )
    assert record, "a run that reached the model seam leaves a record even having changed nothing"


def test_implement_reads_a_ticket_file_and_its_task_list(repo: Path) -> None:
    """No tracker, no network. The criteria come through the same parser a real issue body does."""
    from in_lockstep.cli import _ticket_from_file

    ticket = _ticket_from_file(_ticket_file(repo))
    assert ticket.title == "Add a greeting"
    assert ticket.acceptance_criteria == ("`greet()` returns a greeting",)


def test_implement_reads_a_json_ticket(repo: Path) -> None:
    from in_lockstep.cli import _ticket_from_file

    (repo / "t.json").write_text('{"key": "PROJ-7", "title": "Do it", "description": "- [ ] it is done"}')
    ticket = _ticket_from_file(str(repo / "t.json"))
    assert ticket.key == "PROJ-7"
    assert ticket.acceptance_criteria == ("it is done",)


def test_a_missing_ticket_file_is_a_message_not_a_traceback(repo: Path) -> None:
    result = CliRunner().invoke(main, ["implement", "--dry-run", "--approve", "--ticket-file", "nope.md"])
    assert result.exit_code != 0
    assert "no ticket file at nope.md" in result.output


def test_a_changeset_artifact_round_trips_into_apply_inline(repo: Path) -> None:
    """The two halves have to agree on the format, and only a round trip proves they do."""
    from in_lockstep.cli import _load_changeset, _write_artifact
    from in_lockstep.core.types import ChangeAuthor, ChangeSet, FileChange

    changeset = ChangeSet(
        changes=(FileChange(path="src/a.py", contents="x = 1\n", author=ChangeAuthor.AGENT),),
        summary="did it",
        ticket="#1",
    )
    _write_artifact(str(repo / "out"), changeset)
    assert _load_changeset(str(repo / "out")) == changeset

    result = CliRunner().invoke(main, ["apply-inline", "--from-artifact", str(repo / "out")])
    assert result.exit_code == 0, result.output
    assert (repo / "src" / "a.py").read_text() == "x = 1\n"


OWN_ADAPTER = """
import json

from in_lockstep import Lockstep
from in_lockstep.adapters.ai import Implement, Oneshot
from in_lockstep.ai.invoker import AiInvoker, InvokePolicy
from in_lockstep.ai.pricing import CostTable, Rate
from in_lockstep.core.spend import Budget
from in_lockstep.llm.interface import LLMProvider
from in_lockstep.llm.types import LLMOutput, TokenUsage, ToolCall
from in_lockstep.middleware.approval import ApprovalGate
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress

lockstep = Lockstep.detect()
lockstep.budget = Budget(usd=2.00)
lockstep.bind(EgressPolicy, UnsandboxedEgress())
lockstep.middleware += [ApprovalGate(granted=lambda call: True)]

SCRIPT = [
    LLMOutput(content="", tool_calls=[ToolCall(id="1", name="write_file",
              input={"path": "src/a.py", "contents": "x = 1\\n"})]),
    LLMOutput(content=json.dumps({"summary": "wrote it", "notes": [], "unfinished": []})),
]


class Scripted(LLMProvider):
    def name(self):
        return "scripted"

    async def generate(self, input):
        out = SCRIPT.pop(0)
        out.usage = TokenUsage(input_tokens=100, output_tokens=20)
        return out


table = CostTable()
table.add("house-model", Rate(input_per_m=1.0, output_per_m=2.0))

lockstep.bind(
    Implement,
    Oneshot(
        lambda ctx: AiInvoker(
            Scripted(), model="house-model", cost_table=table, spend=ctx.spend,
            egress=UnsandboxedEgress(),
        ),
        repo_root=lockstep.repo.root,
        policy=InvokePolicy(max_turns=4, max_tokens=1024),
    ),
)
"""


def test_a_repository_that_binds_its_own_adapter_keeps_it(repo: Path) -> None:
    """The CLI's own binding is a default, and a default must not overrule a decision."""
    _lifecycle(repo).write_text(OWN_ADAPTER)
    result = CliRunner().invoke(
        main,
        ["implement", "--ticket-file", _ticket_file(repo), "--out", str(repo / "change")],
    )
    assert result.exit_code == 0, result.output
    assert "wrote it" in result.output
    assert (repo / "change" / "changeset.json").exists()


def test_the_ledger_does_not_name_a_model_the_cli_did_not_choose(repo: Path) -> None:
    """A record naming a model that was never called is worse than one quiet about which was.

    The same shape as `priced_fraction` being omitted rather than written as zero: a measurement
    nobody took is not a measurement of nothing. The repository below binds its own adapter, so
    `--model` was never consulted and there is nothing honest for this command to write.
    """
    import json

    _lifecycle(repo).write_text(OWN_ADAPTER)
    CliRunner().invoke(
        main,
        ["implement", "--ticket-file", _ticket_file(repo), "--model", "anthropic:claude-opus-4-6"],
    )
    record = json.loads(_ledger_record(repo, "implement-TICKET").read_text())
    assert "model" not in record
    assert record["strategy"] == "implement/oneshot"


def test_the_gate_refuses_with_exit_3_and_says_which_route_was_checked(repo: Path) -> None:
    """A refusal that names the routes is the difference between "add them to CODEOWNERS" and
    "invite them to the org"."""
    (repo / "CODEOWNERS").write_text("*  @alice\n")
    result = CliRunner().invoke(
        main, ["gate", "--actor", "mallory", "--association", "CONTRIBUTOR", "--codeowners", "CODEOWNERS"]
    )
    assert result.exit_code == 3
    assert "codeowner=no" in result.output
    assert "association=CONTRIBUTOR" in result.output


def test_the_gate_allows_a_code_owner(repo: Path) -> None:
    (repo / "CODEOWNERS").write_text("*  @alice\n")
    result = CliRunner().invoke(
        main, ["gate", "--actor", "@Alice", "--association", "NONE", "--codeowners", "CODEOWNERS"]
    )
    assert result.exit_code == 0
    assert "allowed" in result.output


def test_a_missing_codeowners_file_is_not_a_crash(repo: Path) -> None:
    """A repository without one is ordinary; the association route still decides."""
    result = CliRunner().invoke(
        main, ["gate", "--actor", "dana", "--association", "MEMBER", "--codeowners", "nope"]
    )
    assert result.exit_code == 0


def test_who_approved_an_unattended_run_reaches_the_ledger(repo: Path) -> None:
    """A grant nobody can be traced to is not much of a grant.

    Absent for an attended local run, where the person reading the output approved it and a name
    would be noise; present for anything a trigger fired.
    """
    import json

    _lifecycle(repo).write_text(IMPLEMENT_MODULE)
    CliRunner().invoke(
        main,
        ["implement", "--dry-run", "--approved-by", "@tpouyer", "--ticket-file", _ticket_file(repo)],
    )
    record = json.loads(_ledger_record(repo, "implement-TICKET").read_text())
    assert record["approval"] == {"by": "@tpouyer", "attended": False}


def test_an_attended_run_records_who_and_that_they_watched(repo: Path) -> None:
    import json

    _lifecycle(repo).write_text(IMPLEMENT_MODULE)
    CliRunner().invoke(main, ["implement", "--dry-run", "--approve", "--ticket-file", _ticket_file(repo)])
    record = json.loads(_ledger_record(repo, "implement-TICKET").read_text())
    # Still recorded, and recorded as ATTENDED — which is the point. A person at a terminal and a
    # gated CI actor are both grants and are not the same grant.
    assert record["approval"]["attended"] is True
    assert record["approval"]["by"]


def test_approved_by_alone_satisfies_the_approval_gate(repo: Path) -> None:
    """The unattended form has to actually grant, or a trigger can never start a run."""
    _lifecycle(repo).write_text(
        IMPLEMENT_MODULE.replace("lockstep.middleware += [ApprovalGate(granted=lambda call: True)]", "")
    )
    result = CliRunner().invoke(
        main,
        ["implement", "--dry-run", "--approved-by", "@tpouyer", "--ticket-file", _ticket_file(repo)],
    )
    assert "ApprovalGate" not in result.output, result.output


# -- `run <workflow>`, which is the entry point external CI is meant to use --------------------


def _workflow_repo(repo: Path, body: str) -> None:
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep, Outcome, Status, workflow\n"
        "from in_lockstep.core.spend import Budget\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n" + body
    )


def test_run_dispatches_a_registered_workflow(repo: Path) -> None:
    """`@workflow` registered into a registry nothing dispatched from.

    A repository could declare a lifecycle the CLI would not run — which is most of what this
    framework claims to be, and the gap that gets worked around with shell in a CI file.
    """
    _workflow_repo(
        repo,
        "@workflow(id='demo/ok')\nasync def demo(ctx):\n    return Outcome(status=Status.SUCCEEDED)\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/ok"])
    assert result.exit_code == 0, result.output
    assert "demo/ok  succeeded" in result.output


def test_an_unknown_workflow_lists_what_the_module_registered(repo: Path) -> None:
    """The check ran BEFORE the module loaded, so it listed an empty registry every time.

    "registered: selfcheck" was true of the process and useless to the person, who had just
    written the workflow it did not mention.
    """
    _workflow_repo(
        repo,
        "@workflow(id='demo/ok')\nasync def demo(ctx):\n    return Outcome(status=Status.SUCCEEDED)\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/nope"])
    assert result.exit_code != 0
    assert "demo/ok" in result.output, "it must name what this repository actually registered"


def test_arguments_reach_the_workflow(repo: Path) -> None:
    _workflow_repo(
        repo,
        "@workflow(id='demo/args')\n"
        "async def demo(ctx, who='nobody'):\n"
        "    return Outcome(status=Status.SUCCEEDED, findings=(\n"
        "        __import__('in_lockstep').Finding(id='hello', message=f'hello {who}'),))\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/args", "--arg", "who=world"])
    assert "hello world" in result.output, result.output


def test_a_signature_mismatch_names_the_parameters(repo: Path) -> None:
    """The traceback for this points at asyncio, not at the workflow the user named."""
    _workflow_repo(
        repo,
        "@workflow(id='demo/args')\n"
        "async def demo(ctx, who='nobody'):\n"
        "    return Outcome(status=Status.SUCCEEDED)\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/args", "--arg", "wrong=1"])
    assert result.exit_code != 0
    assert "It takes who" in result.output
    assert "Traceback" not in result.output


def test_a_typed_port_parameter_is_injected_from_the_container(repo: Path) -> None:
    """The signature names the port, the dispatcher fills it.

    A workflow parameter annotated with a container-bound class arrives resolved — the body never
    touches `ctx.container` — and the error message for a missing CLI argument lists only the
    arguments a caller actually supplies, not the injected ports.
    """
    _workflow_repo(
        repo,
        "class Port:\n"
        "    def greeting(self):\n"
        "        return 'hello from the port'\n"
        "lockstep.bind(Port, Port())\n"
        "@workflow(id='demo/ports')\n"
        "async def demo(ctx, who: str, port: Port):\n"
        "    return Outcome(status=Status.SUCCEEDED, findings=(\n"
        "        __import__('in_lockstep').Finding(id='hi', message=f'{port.greeting()} {who}'),))\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/ports", "--arg", "who=world"])
    assert "hello from the port world" in result.output, result.output

    mismatch = CliRunner().invoke(main, ["run", "demo/ports"])
    assert mismatch.exit_code != 0
    assert "It takes who" in mismatch.output, mismatch.output
    assert "port" not in mismatch.output.split("It takes")[1].splitlines()[0], (
        "the injected port is not an argument the caller supplies"
    )


def test_a_supplied_argument_is_never_overridden_by_injection(repo: Path) -> None:
    """`--arg` wins: injection fills only what the caller left empty."""
    _workflow_repo(
        repo,
        "@workflow(id='demo/override')\n"
        "async def demo(ctx, who: str = 'default'):\n"
        "    return Outcome(status=Status.SUCCEEDED, findings=(\n"
        "        __import__('in_lockstep').Finding(id='hi', message=f'got {who}'),))\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/override", "--arg", "who=explicit"])
    assert "got explicit" in result.output, result.output


def test_a_malformed_arg_is_refused(repo: Path) -> None:
    _workflow_repo(
        repo,
        "@workflow(id='demo/ok')\nasync def demo(ctx):\n    return Outcome(status=Status.SUCCEEDED)\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/ok", "--arg", "noequals"])
    assert result.exit_code != 0
    assert "NAME=VALUE" in result.output


def test_a_blocked_workflow_exits_three(repo: Path) -> None:
    """The exit code a control refusing produces, so CI can tell it from a failure."""
    _workflow_repo(
        repo,
        "@workflow(id='demo/blocked')\nasync def demo(ctx):\n    return Outcome.blocked_by('demo.refused')\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/blocked"])
    assert result.exit_code == 3, result.output
    assert "demo.refused" in result.output


def test_a_dispatched_workflow_leaves_a_ledger_record(repo: Path) -> None:
    """Moving a process into a @workflow must not cost the run its evidence.

    A record that exists for the built-in commands and not for the recommended path is an argument
    for not taking the recommendation.
    """
    import json

    _workflow_repo(
        repo,
        "@workflow(id='demo/ok')\n"
        "async def demo(ctx, issue='#1'):\n"
        "    return Outcome(status=Status.SUCCEEDED)\n",
    )
    CliRunner().invoke(main, ["run", "demo/ok", "--arg", "issue=#59"])
    records = list((repo / ".lockstep/ledger").glob("demo-ok-*.json"))
    assert records, "a dispatched workflow wrote no record"
    record = json.loads(records[0].read_text())
    assert record["kind"] == "workflow"
    assert record["workflow"] == "demo/ok"
    # The provenance: which issue, which actor. Without it the record cannot say what it was for.
    assert record["args"] == {"issue": "#59"}


def test_run_states_a_ceiling_the_module_did_not(repo: Path) -> None:
    """One workflow in a module can be far more expensive than another, and Budget is run-scoped."""
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep, Outcome, Status, workflow\n"
        "lockstep = Lockstep.detect()\n"
        "@workflow(id='demo/ok')\n"
        "async def demo(ctx):\n"
        "    return Outcome(status=Status.SUCCEEDED)\n"
    )
    assert CliRunner().invoke(main, ["run", "demo/ok", "--budget", "3.00"]).exit_code == 0


def test_selfcheck_still_works(repo: Path) -> None:
    """The one built-in workflow, which the old dispatcher special-cased."""
    CliRunner().invoke(main, ["init"])
    result = CliRunner().invoke(main, ["run", "selfcheck", "--paths", str(repo)])
    assert "validate" in result.output


def test_a_setup_error_in_a_workflow_is_a_message_not_a_traceback(repo: Path) -> None:
    """The recommended path must not be worse than the built-in one.

    `review` and `implement` translate a missing credential into one sentence. Until this, a
    `@workflow` doing the same work produced forty lines of traceback — which is a reason not to
    move a process into `lockstep.py`, and moving it there is the whole recommendation.
    """
    _workflow_repo(
        repo,
        "from in_lockstep.ai.bootstrap import MissingCredential\n"
        "@workflow(id='demo/setup')\n"
        "async def demo(ctx):\n"
        "    raise MissingCredential('no credential for provider')\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/setup"])
    assert result.exit_code != 0
    assert "no credential for provider" in result.output
    assert "Traceback" not in result.output


# -- the seam a maturing project crosses -------------------------------------------------------
#
# The framework's premise is that a process is defined once and its INVOCATION changes as a project
# grows: a terminal at first, a hosted trigger later. That only holds if the same command works on
# both sides. Approval was where it did not — `implement` took `--approve`, `run` took nothing, and
# the recommended path needed an environment variable a developer would have to know to export.


def test_the_same_workflow_command_serves_a_terminal_and_a_trigger(repo: Path) -> None:
    """One invocation, two provenances. The difference is who the human is, not the process."""
    import json

    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep, Outcome, Status, workflow\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.middleware.approval import ApprovalGate\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n"
        "lockstep.middleware += [ApprovalGate()]\n"
        "@workflow(id='demo/needs-a-human')\n"
        "async def demo(ctx):\n"
        "    return Outcome(status=Status.SUCCEEDED)\n"
    )

    attended = CliRunner().invoke(main, ["run", "demo/needs-a-human", "--approve"])
    assert attended.exit_code == 0, attended.output
    record = json.loads(_ledger_record(repo, "demo-needs-a-human").read_text())
    assert record["approval"]["attended"] is True

    unattended = CliRunner().invoke(main, ["run", "demo/needs-a-human", "--approved-by", "octocat"])
    assert unattended.exit_code == 0, unattended.output
    record = json.loads(_ledger_record(repo, "demo-needs-a-human").read_text())
    assert record["approval"] == {"by": "octocat", "attended": False}


def test_a_workflow_needing_a_human_refuses_when_nobody_asked(repo: Path) -> None:
    """No flag, no grant. The refusal names both forms rather than only the local one."""
    import asyncio

    from in_lockstep.adapters.ai.implement import Implement
    from in_lockstep.core.container import Container
    from in_lockstep.core.context import Approval, RunContext
    from in_lockstep.core.middleware import ActionCall
    from in_lockstep.core.outcome import Status
    from in_lockstep.core.verbs import Capability
    from in_lockstep.middleware.approval import ApprovalGate

    class Writer:
        capabilities = frozenset({Capability.WRITES_FILES, Capability.SPENDS_BUDGET})

    container = Container()
    container.bind(Implement, Writer())
    ctx = RunContext(run_id="r", repo=None, container=container)  # type: ignore[arg-type]

    async def go(approval: Approval) -> object:
        ctx.approval = approval
        call = ActionCall(Implement(ticket=None))

        async def nxt() -> object:
            from in_lockstep.core.outcome import Outcome

            return Outcome(status=Status.SUCCEEDED)

        return await ApprovalGate()(ctx, call, nxt)

    refused = asyncio.run(go(Approval()))
    assert refused.status is Status.BLOCKED
    assert "--approve" in refused.findings[0].message
    assert "--approved-by" in refused.findings[0].message

    assert asyncio.run(go(Approval(by="octocat"))).status is Status.SUCCEEDED


# -- `history`, and where a run record actually goes --------------------------------------------


def test_the_history_command_says_when_there_is_none_yet(repo: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    result = CliRunner().invoke(main, ["history"])
    assert result.exit_code == 0
    assert "no history yet" in result.output


def test_a_run_in_a_git_repository_records_to_the_orphan_branch(repo: Path) -> None:
    """Not into the working tree, where `.lockstep/` is gitignored and the record is lost."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    _workflow_repo(
        repo,
        "@workflow(id='demo/ok')\nasync def demo(ctx):\n    return Outcome(status=Status.SUCCEEDED)\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/ok"])
    assert result.exit_code == 0, result.output
    assert "lockstep-history:records/" in result.output
    assert not (repo / ".lockstep" / "ledger").exists(), "it fell back to the working tree"

    listing = CliRunner().invoke(main, ["history"])
    assert "1 record(s)" in listing.output
    assert "demo-ok" in listing.output


def test_a_directory_that_is_not_a_repository_still_records(repo: Path) -> None:
    """The fallback, and it is a fallback rather than a default — there is no branch to use."""
    _workflow_repo(
        repo,
        "@workflow(id='demo/ok')\nasync def demo(ctx):\n    return Outcome(status=Status.SUCCEEDED)\n",
    )
    result = CliRunner().invoke(main, ["run", "demo/ok"])
    assert result.exit_code == 0, result.output
    assert (repo / ".lockstep" / "ledger").exists()


def test_a_root_lockstep_py_is_loaded_and_called_deprecated(repo: Path) -> None:
    """Read, not refused, and not ignored either — all three were tried and only one is right.

    Ignoring it runs on detected defaults while a working configuration sits unread, which is the
    worst outcome because everything appears to work with none of the repository's bindings.
    Refusing breaks every existing repository on upgrade — and cannot be satisfied at all by the
    pull request that performs the move, since configuration comes from the base branch and the
    move exists only on the branch under review. That change would fail its own review forever.
    """
    (repo / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n"
    )
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "DEPRECATED" in result.output
    assert "git mv lockstep.py .lockstep/lockstep.py" in result.output
    assert "config    none" not in result.output, "it fell through to detected defaults"


def test_the_new_location_wins_when_both_exist(repo: Path) -> None:
    """A repository mid-migration must not have which file is in effect decided by luck."""
    (repo / "lockstep.py").write_text("raise AssertionError('the root copy was loaded')\n")
    _lifecycle(repo).write_text("from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n")
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "DEPRECATED" not in result.output


def test_init_refuses_to_scaffold_beside_a_root_module(repo: Path) -> None:
    """Two configurations and no way to tell which is in effect is worse than one in the wrong place."""
    (repo / "lockstep.py").write_text("lockstep = None\n")
    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code != 0
    assert "no longer read" in result.output
    assert not (repo / ".lockstep" / "lockstep.py").exists()


def test_init_scaffolds_into_the_lockstep_directory(repo: Path) -> None:
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    assert (repo / ".lockstep" / "lockstep.py").is_file()
    assert not (repo / "lockstep.py").exists(), "the root is on sys.path; nothing goes there"


# -- egress-manifest --------------------------------------------------------------------------


def test_egress_manifest_narrows_to_the_routed_providers(repo: Path) -> None:
    """With routes declared, the list is what this repository dials, not the whole default set."""
    _write(repo, model="anthropic:claude-haiku-4-5")
    result = CliRunner().invoke(main, ["egress-manifest"])
    assert result.exit_code == 0, result.output
    assert "api.anthropic.com" in result.output
    assert "generativelanguage.googleapis.com" not in result.output


def test_egress_manifest_includes_the_bound_policys_extras(repo: Path) -> None:
    """`allow` finally consulted: the operator's declared additions appear beside the endpoints."""
    _lifecycle(repo).write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.privileged.egress import EgressPolicy\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n"
        'lockstep.bind(EgressPolicy, EgressPolicy(allow=("proxy-extra.example",)))\n'
        'lockstep.models.route("review", "anthropic:claude-haiku-4-5")\n'
    )
    result = CliRunner().invoke(main, ["egress-manifest"])
    assert result.exit_code == 0, result.output
    assert "proxy-extra.example" in result.output
    assert "api.anthropic.com" in result.output


def test_egress_manifest_without_routes_prints_every_registered_endpoint(repo: Path) -> None:
    """No routes means no narrowing, and an honest superset beats a silent empty list."""
    _lifecycle(repo).write_text("from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n")
    result = CliRunner().invoke(main, ["egress-manifest"])
    assert result.exit_code == 0, result.output
    assert "api.anthropic.com" in result.output
