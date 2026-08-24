"""Two controls this framework claimed and did not have.

`enforce:` bounded an agent — permissions, egress, tools, turns, credits — and bounded nothing about
a `script:` step, which is the step actually running arbitrary code. And the shipped baseline
guardrail asked a model to treat its input as data, which is a sentence in a prompt addressed to the
thing being attacked.

Both were found by reading a tool that does this in production. Both are floors here now.
"""

from __future__ import annotations

import yaml

from lockstep.emit import compile_spec
from lockstep.emit.semantic_diff import BLOCKING
from lockstep.spec.load import load_spec
from lockstep.spec.model import Sandbox

WORKFLOW = ".github/workflows/generate-tests.yml"
AGENT_JOB = "extract-stories-from-each-issue"


def jobs(root, name=WORKFLOW):
    return yaml.safe_load(compile_spec(root).files[name])["jobs"]


def exec_jobs(root, name=WORKFLOW):
    return {k: v for k, v in jobs(root, name).items() if isinstance(v.get("container"), dict)}


def add_sandbox(root, block):
    manifest = root / "pipeline.yaml"
    manifest.write_text(
        manifest.read_text().replace("    profiles: [my-app]", f"    profiles: [my-app]\n{block}"),
        encoding="utf-8",
    )


# --- the sandbox floor -------------------------------------------------------


def test_every_job_running_a_script_drops_its_capabilities(basic_spec_dir):
    """The floor, applied to a pipeline that declares nothing. Neither flag is optional."""
    found = exec_jobs(basic_spec_dir)
    assert found, "expected at least one job in the executor container"
    for job in found.values():
        assert "--cap-drop=ALL" in job["container"]["options"]
        assert "--security-opt=no-new-privileges" in job["container"]["options"]


def test_the_image_is_still_pinned_by_digest(basic_spec_dir):
    for job in exec_jobs(basic_spec_dir).values():
        assert "@sha256:" in job["container"]["image"]


def test_a_capability_can_be_added_back_by_name(basic_root):
    """`--cap-add=ALL` would be a way to write "no sandbox" that does not look like one."""
    add_sandbox(basic_root, "    sandbox:\n      capabilities: [NET_ADMIN]")
    options = next(iter(exec_jobs(basic_root).values()))["container"]["options"]
    assert options == (
        "--cap-drop=ALL --cap-add=DAC_OVERRIDE --security-opt=no-new-privileges --cap-add=NET_ADMIN"
    )


def test_resource_limits_are_declarable(basic_root):
    add_sandbox(basic_root, "    sandbox:\n      memory: 4g\n      cpus: '2'\n      pids: 512")
    options = next(iter(exec_jobs(basic_root).values()))["container"]["options"]
    assert "--memory=4g" in options and "--cpus=2" in options and "--pids-limit=512" in options


def test_the_floor_survives_whatever_is_declared(basic_root):
    add_sandbox(basic_root, "    sandbox:\n      user: '1001'")
    options = next(iter(exec_jobs(basic_root).values()))["container"]["options"]
    assert options.startswith("--cap-drop=ALL --cap-add=DAC_OVERRIDE --security-opt=no-new-privileges")
    assert "--user=1001" in options


def test_the_default_declares_no_user(basic_spec_dir):
    """The shipped image runs as root and GitHub mounts the workspace for root.

    Non-root is available and is not the default, because a default nobody has run is a guess.
    """
    assert "--user" not in next(iter(exec_jobs(basic_spec_dir).values()))["container"]["options"]


def test_widening_the_sandbox_is_a_blocking_change():
    assert "sandbox" in BLOCKING


def test_the_options_are_built_in_one_place():
    assert Sandbox().options() == "--cap-drop=ALL --cap-add=DAC_OVERRIDE --security-opt=no-new-privileges"


def test_the_floor_keeps_the_capability_the_runner_protocol_needs():
    """DAC_OVERRIDE is not hardening relaxed, it is the one thing Actions itself requires.

    The runner owns `$GITHUB_OUTPUT` and `$GITHUB_STATE`; the container runs as root; root writes a
    file it does not own by holding this capability. Dropping it made every step-output write fail
    with EACCES, which took out job-to-job communication entirely — including the output deciding
    which review lenses run, and the metering job that writes the run ledger.
    """
    options = Sandbox().options()
    assert "--cap-drop=ALL" in options
    assert "--cap-add=DAC_OVERRIDE" in options
    assert "--security-opt=no-new-privileges" in options


def test_nothing_else_is_added_back_by_default():
    """One exception, not a habit of them."""
    added = [part for part in Sandbox().options().split() if part.startswith("--cap-add=")]
    assert added == ["--cap-add=DAC_OVERRIDE"]


# --- the input scan ----------------------------------------------------------


def test_an_agent_is_preceded_by_a_scan_of_what_it_will_read(basic_spec_dir):
    """The shipped baseline enforces it, so every pipeline gets one without asking."""
    found = jobs(basic_spec_dir)
    assert f"scan-{AGENT_JOB}" in found
    run = " ".join(s.get("run", "") for s in found[f"scan-{AGENT_JOB}"]["steps"])
    assert "pipeline-exec scan-input" in run
    assert "--mode=warn" in run


def test_the_agent_waits_for_the_scan(basic_spec_dir):
    needs = jobs(basic_spec_dir)[AGENT_JOB]["needs"]
    assert f"scan-{AGENT_JOB}" in ([needs] if isinstance(needs, str) else needs)


def test_the_scan_is_added_to_the_agents_needs_never_substituted(basic_spec_dir):
    """Its `if:` reads `needs.<job>.outputs`; a dropped job makes that empty rather than an error."""
    found = jobs(basic_spec_dir)
    scan_needs = found[f"scan-{AGENT_JOB}"]["needs"]
    agent_needs = found[AGENT_JOB]["needs"]
    upstream = [scan_needs] if isinstance(scan_needs, str) else scan_needs
    assert set(upstream) <= set(agent_needs)


def test_the_scan_runs_in_the_sandbox_like_any_other_deterministic_step(basic_spec_dir):
    container = jobs(basic_spec_dir)[f"scan-{AGENT_JOB}"]["container"]
    assert "--cap-drop=ALL" in container["options"]


def test_a_fan_out_scans_the_whole_item_directory(basic_spec_dir):
    """Which item carries the payload is the question, not something the compiler can name."""
    run = " ".join(s.get("run", "") for s in jobs(basic_spec_dir)[f"scan-{AGENT_JOB}"]["steps"])
    assert "--input=" in run


def test_a_pipeline_whose_guardrails_ask_for_nothing_gets_no_scan(basic_root):
    """The framework's baseline asks for it. Take that away and the job disappears with it."""
    import lockstep.library as library

    library._load.cache_clear()
    baseline = library.HERE / "guardrails" / "baseline.md"
    original = baseline.read_text()
    try:
        baseline.write_text(original.replace("  scan-input: warn\n", ""), encoding="utf-8")
        library._load.cache_clear()
        assert f"scan-{AGENT_JOB}" not in jobs(basic_root)
    finally:
        baseline.write_text(original, encoding="utf-8")
        library._load.cache_clear()


def test_a_sealed_guardrail_can_make_it_block(basic_root):
    """warn is the shipped default; an organization that wants the stronger answer seals block."""
    guardrail = basic_root / "guardrails" / "common.md"
    guardrail.write_text(
        guardrail.read_text().replace("---\n\n", "enforce:\n  scan-input: block\n---\n\n", 1),
        encoding="utf-8",
    )
    run = " ".join(s.get("run", "") for s in jobs(basic_root)[f"scan-{AGENT_JOB}"]["steps"])
    assert "--mode=block" in run


def test_the_strictest_setting_wins(basic_spec_dir):
    """Two guardrails asking for different strengths are two constraints; the weaker satisfies one."""
    from lockstep.emit.fragments import resolve_layers

    spec = load_spec(basic_spec_dir)
    agent = spec.agents["story-extractor"]
    profile = spec.compiled_profiles()[0]
    assert resolve_layers(agent, None, profile, spec).enforce().scan_input == "warn"
