"""The command line, which had no tests at all until this file.

`cli.py` is the largest module in the package and the only one every user touches. It is also the
composition root: which `Lockstep` gets built, whether the repository's own module is loaded, and
which bindings survive are all decided here. None of that was asserted, and the gap showed —
`review`, the one command that spends money, built its own `Lockstep` from scratch and silently
discarded every binding, budget, policy contribution and model route the module declared.

`--dry-run` and `--offline` exist precisely so this file can be written without a key or a cent.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from in_lockstep.cli import main

ROOT_REPO = Path(__file__).resolve().parents[2]

MODULE = '''
from in_lockstep import Lockstep, Policy
from in_lockstep.core.spend import Budget

lockstep = Lockstep.detect()
lockstep.budget = Budget(usd={budget})
lockstep.contribute(Policy(name="repo", source="test", max_turns={turns}))
lockstep.models.route("review", "{model}")
'''


@pytest.fixture(autouse=True)
def _no_verb_leakage() -> Iterator[None]:
    """The intern table is process-global, so a verb one test defines outlives it."""
    from in_lockstep.core.verbs import Verb

    yield
    Verb.forget_custom()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory with its own lockstep.py, loaded from the working tree.

    Not a git repository, deliberately: with no ref to resolve, `config_ref` treats the working
    tree as the trusted source, which is the local-development path and the one under test here.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    return tmp_path


def _write(
    repo: Path,
    *,
    budget: float = 5.00,
    turns: int = 9,
    model: str = "anthropic:claude-haiku-4-5",
) -> None:
    (repo / "lockstep.py").write_text(MODULE.format(budget=budget, turns=turns, model=model))


def test_review_loads_the_repositorys_own_module(repo: Path) -> None:
    """The headline: `lockstep.py` is the configuration, including for the command that spends."""
    _write(repo)
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
    assert result.exit_code == 0, result.output
    # The module routes review at a model the CLI's own default would not have chosen. A route
    # nothing reads is how `Models.route` shipped: written by the config, consumed by nobody.
    assert "haiku" in (repo / ".in-lockstep/ledger/review-security.json").read_text()


def test_an_untyped_model_flag_does_not_outrank_a_declared_route(repo: Path) -> None:
    _write(repo, model="google:gemini-2.5-flash")
    CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
    assert "gemini-2.5-flash" in (repo / ".in-lockstep/ledger/review-security.json").read_text()


def test_an_explicit_model_flag_does_outrank_it(repo: Path) -> None:
    """An override the user actually typed is an override; a default is not."""
    _write(repo, model="google:gemini-2.5-flash")
    CliRunner().invoke(
        main, ["review", "--dry-run", "--base", "HEAD", "--model", "anthropic:claude-opus-4-6"]
    )
    assert "opus" in (repo / ".in-lockstep/ledger/review-security.json").read_text()


def test_no_module_still_runs_on_detected_defaults(repo: Path) -> None:
    """A repository without a lockstep.py is supported, not an error.

    It still has to state a ceiling, because `review` binds something that spends. That is
    GATE-BUDGET-1 rather than a gap in the fallback: the alternative is the CLI inventing a
    number, which is the failure the gate exists to prevent.
    """
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD", "--budget", "1.00"])
    assert result.exit_code == 0, result.output


def test_telemetry_says_when_the_cli_cannot_see_the_chain(repo: Path) -> None:
    """`spans 0` for a run that emitted spans elsewhere is a wrong number, not a missing one."""
    (repo / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.middleware import otel\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n"
        "lockstep.middleware += [otel()]\n"
    )
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
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
    """It pointed at `.in-lockstep/out/`, which no code path in the package ever creates."""
    import yaml

    CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    paths = [
        s["with"]["path"]
        for j in workflow["jobs"].values()
        for s in j["steps"]
        if "upload-artifact" in str(s.get("uses", ""))
    ]
    assert paths == [".in-lockstep/"], paths


def test_the_scaffold_carries_a_timeout(repo: Path) -> None:
    """Without one the CI default is 360 minutes, and there is no other wall clock in the job."""
    import yaml

    CliRunner().invoke(main, ["init"])
    workflow = yaml.safe_load((repo / ".github/workflows/lockstep.yml").read_text())
    assert all("timeout-minutes" in j for j in workflow["jobs"].values())


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


def test_apply_refuses_rather_than_exiting_zero_without_writing(repo: Path, tmp_path: Path) -> None:
    """A green `apply` job is read as "the change landed"."""
    import json

    artifact = tmp_path / "changeset.json"
    artifact.write_text(json.dumps({"changes": [{"path": "src/x.py", "body": "x = 1"}]}))

    ok = CliRunner().invoke(main, ["apply", "--from-artifact", str(artifact), "--dry-run"])
    assert ok.exit_code == 0
    assert "nothing was written" in ok.output

    refused = CliRunner().invoke(main, ["apply", "--from-artifact", str(artifact)])
    assert refused.exit_code != 0
    assert "does not write yet" in refused.output


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
    (repo / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.verbs import Verb\n"
        "lockstep = Lockstep.detect()\n"
        "Verb('reviwe')\n"
    )
    result = CliRunner().invoke(main, ["ls"])
    assert result.exit_code == 0, result.output
    assert "verbs defined but unbound" in result.output
    assert "reviwe" in result.output


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
        ".in-lockstep/ledger/x.json",
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
    (repo / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n"
    )
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
    assert result.exit_code != 0
    assert "no budget is declared" in result.output
    assert "Traceback" not in result.output, "a refusal is a message, not a crash"


def test_an_explicit_budget_flag_satisfies_it(repo: Path) -> None:
    (repo / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n"
    )
    result = CliRunner().invoke(
        main, ["review", "--dry-run", "--base", "HEAD", "--budget", "0.50"]
    )
    assert result.exit_code == 0, result.output


def test_ls_still_works_without_a_budget(repo: Path) -> None:
    """The diagnostic that tells you what is bound must survive the refusal that mentions it.

    `ls` never opens a run, so it does not trip the startup check — which is what lets someone
    read the error, run `ls`, and see the adapter it named.
    """
    (repo / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\nlockstep = Lockstep.detect()\n"
    )
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
    (repo / "lockstep.py").write_text(
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
    result = CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
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
    module = (ROOT_REPO / "lockstep.py").read_text()
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
    (repo / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=1.00)\n"
        "lockstep.bind(EgressPolicy, UnsandboxedEgress())\n"
    )
    result = CliRunner().invoke(main, ["review", "--base", "HEAD", "--model", "anthropic:claude-haiku-4-5"])
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

    (repo / "lockstep.py").write_text(
        "from in_lockstep import Lockstep\n"
        "from in_lockstep.core.spend import Budget\n"
        "from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress\n"
        "lockstep = Lockstep.detect()\n"
        "lockstep.budget = Budget(usd=0.0001)\n"
        "lockstep.bind(EgressPolicy, UnsandboxedEgress())\n"
    )
    CliRunner().invoke(main, ["review", "--dry-run", "--base", "HEAD"])
    record = json.loads((repo / ".in-lockstep/ledger/review-security.json").read_text())
    assert record["status"] == "blocked"
    assert record["reason"] == "cost.budget_exceeded", record


# -- the ledger keeps what was found, not only how much ---------------------------------------


def _reviewing_repo(repo: Path, *, findings: str) -> None:
    (repo / "lockstep.py").write_text(
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
    result = CliRunner().invoke(main, ["review", "--offline", "--base", "HEAD~1", "--head", "HEAD"])
    assert result.exit_code != 0
    assert "no longer matches the prompt" in result.output or "no cassette entry" in result.output
    assert "Traceback" not in result.output
