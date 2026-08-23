# Extracted from pipeline-framework src/builtins/test_runner.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ..config import ExecConfig
from ..executors.direct_executor import DirectExecutor
from ..executors.types import ScriptStep, TestResult, TestScript
from ..logging import log
from ..sanitize import sanitize


def load_test_scripts(config: ExecConfig) -> list[TestScript]:
    """Load all JSON test scripts from the outputs/test-scripts directory."""
    scripts_dir = Path(config.scripts_dir)
    if not scripts_dir.exists():
        log.warning(f"  No test scripts directory: {scripts_dir}")
        return []

    scripts: list[TestScript] = []
    for json_file in sorted(scripts_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            script = _parse_test_script(data)
            scripts.append(script)
        except (json.JSONDecodeError, KeyError, AttributeError, TypeError) as e:
            log.warning(f"  Skipping invalid script {json_file.name}: {e}")

    log.info(f"  Loaded {len(scripts)} test scripts")
    return scripts


def _parse_test_script(data: dict[str, Any]) -> TestScript:
    """Parse a JSON dict into a TestScript."""

    def parse_steps(steps_data: list[dict[str, Any]]) -> list[ScriptStep]:
        return [
            ScriptStep(
                step=s.get("step", 0),
                tool=s.get("tool", ""),
                action=s.get("action", ""),
                params=s.get("params", {}),
                expected=s.get("expected", ""),
            )
            for s in steps_data
        ]

    return TestScript(
        story_id=data.get("storyId", ""),
        summary=data.get("summary", ""),
        description=data.get("description", ""),
        test_type=data.get("testType", "api"),
        tags=data.get("tags", []),
        setup_steps=parse_steps(data.get("setupSteps", [])),
        test_steps=parse_steps(data.get("testSteps", [])),
        teardown_steps=parse_steps(data.get("teardownSteps", [])),
        execution_tier=data.get("executionTier", 1),
        heal_regen_count=data.get("healRegenCount", 0),
    )


def _filter_by_tags(scripts: list[TestScript], config: ExecConfig) -> list[TestScript]:
    """Filter scripts by tag toggles from .env-tests and environment checks."""
    import os

    env_tests_path = Path(config.tags_file)

    skip_tags: set[str] = set()

    # Load tag toggles. `TAG_<name>=skip` always skips; `TAG_<name>=skip-unless-env:VAR` skips only
    # when VAR is unset, which replaces the framework's hardcoded OCP rule with a declared one.
    if env_tests_path.exists():
        for line in env_tests_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("TAG_") or "=" not in line:
                continue
            name, _, setting = line.partition("=")
            tag = name.replace("TAG_", "").lower()
            if setting == "skip":
                skip_tags.add(tag)
            elif setting.startswith("skip-unless-env:") and not os.getenv(setting.split(":", 1)[1]):
                skip_tags.add(tag)

    if not skip_tags:
        return scripts

    filtered: list[TestScript] = []
    skipped = 0
    for script in scripts:
        if any(tag.lower() in skip_tags for tag in script.tags):
            skipped += 1
        else:
            filtered.append(script)

    if skipped:
        log.info(f"  Skipping {skipped} test(s) by tag filter")
    return filtered


def _filter_changed_only(scripts: list[TestScript], config: ExecConfig) -> list[TestScript]:
    """Filter to only scripts modified since their last execution."""
    scripts_dir = Path(config.scripts_dir)
    runs_dir = Path(config.output_dir) / "runs" / "latest" / "executions"

    if not runs_dir.exists():
        return scripts

    changed: list[TestScript] = []
    for script in scripts:
        script_file = scripts_dir / f"{script.story_id}.json"
        exec_file = runs_dir / f"{script.story_id}.md"

        if not exec_file.exists():
            changed.append(script)
        elif script_file.exists() and script_file.stat().st_mtime > exec_file.stat().st_mtime:
            changed.append(script)

    log.info(f"  Changed-only filter: {len(changed)}/{len(scripts)} scripts modified")
    return changed


async def _run_scripts_tiered(
    scripts: list[TestScript],
    config: ExecConfig,
    run_dir: str,
    agent_count: int = 3,
) -> list[TestResult]:
    """Run test scripts tier-by-tier: sequential across tiers, parallel within."""
    # Group by execution tier
    tiers: dict[int, list[TestScript]] = {}
    for script in scripts:
        tier = getattr(script, "execution_tier", 1) or 1
        tiers.setdefault(tier, []).append(script)

    results: list[TestResult] = []
    semaphore = asyncio.Semaphore(agent_count)

    base_timeout = config.ui_wait_timeout * 10 / 1000  # 10x the step timeout, in seconds (300s default)

    def _calc_timeout(script: TestScript) -> float:
        """Scale timeout by step count: base + 15s per step for UI tests."""
        step_count = len(script.setup_steps or []) + len(script.test_steps) + len(script.teardown_steps or [])
        if script.test_type == "ui":
            # UI tests need login time + per-step budget (each step may wait for selectors)
            return max(base_timeout, 90 + step_count * 15)
        return base_timeout

    async def run_one(script: TestScript, agent_id: int) -> TestResult:
        async with semaphore:
            executor = DirectExecutor(agent_id, config, run_dir)
            timeout = _calc_timeout(script)
            try:
                return await asyncio.wait_for(
                    executor.test_script(script),
                    timeout=timeout,
                )
            except TimeoutError:
                log.warning(f"    {script.story_id} TIMEOUT after {timeout:.0f}s")
                return TestResult(
                    story_id=script.story_id,
                    passed=False,
                    summary=f"Test timed out after {timeout:.0f}s",
                    executed_steps=[],
                    errors=[
                        {"id": "timeout", "title": "Test timeout", "description": f"Exceeded {timeout:.0f}s"}
                    ],
                )

    for tier_num in sorted(tiers.keys()):
        tier_scripts = tiers[tier_num]
        log.info(f"  --- Tier {tier_num}: Running {len(tier_scripts)} test(s) in parallel ---")

        tasks = [run_one(script, (i % agent_count) + 1) for i, script in enumerate(tier_scripts)]

        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
            except Exception as e:
                log.warning(f"    Test task failed unexpectedly: {e}")
                continue
            results.append(result)
            _write_execution_report(result, run_dir)
            _write_error_reports(result, run_dir)
            status = "PASS" if result.passed else "FAIL"
            log.info(f"    {result.story_id} [{status}] {result.summary}")

    return results


def _write_execution_report(result: TestResult, run_dir: str) -> None:
    """Write a markdown execution report for a test result."""
    exec_dir = Path(run_dir) / "executions"
    exec_dir.mkdir(parents=True, exist_ok=True)

    status = "PASSED" if result.passed else "FAILED"
    failed_count = sum(1 for s in result.executed_steps if s.status == "failed")
    total_count = len(result.executed_steps)

    lines = [
        f"# Test Execution Report: {status}\n",
        "## Metadata",
        "| Field | Value |",
        "|-------|-------|",
        f"| **Story ID** | {result.story_id} |",
        f"| **Status** | **{status}** |",
        f"| **Errors** | {len(result.errors)} |",
        "",
        "## Test Summary",
        f"{failed_count} step(s) failed out of {total_count}",
        "",
    ]

    current_phase = ""
    for step in result.executed_steps:
        if step.phase != current_phase:
            current_phase = step.phase
            phase_steps = [s for s in result.executed_steps if s.phase == current_phase]
            p = sum(1 for s in phase_steps if s.status in ("passed", "warn"))
            f = sum(1 for s in phase_steps if s.status == "failed")
            lines.append(f"\n## {current_phase.title()} Phase ({p} passed, {f} failed)\n")

        icon = "PASS" if step.status in ("passed", "warn") else "FAIL" if step.status == "failed" else "SKIP"
        lines.append(f"### [{icon}] Step {step.step_number}: {step.action}")
        lines.append(f"- **Tool:** `{step.tool}`")
        lines.append(f"- **Expected:** {step.expected}")
        lines.append(f"- **Result:** {sanitize(step.result[:2000])}")
        if step.screenshot_path:
            lines.append(f"- **Screenshot:** ![](../{step.screenshot_path})")
        lines.append("")

    if result.errors:
        lines.append("\n## Errors Found\n")
        for error in result.errors:
            lines.append(f"### {error.get('title', 'Error')}")
            lines.append(f"- **Description:** {sanitize(error.get('description', '')[:500])}")
            lines.append("")

    report_path = exec_dir / f"{result.story_id}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")


def _write_error_reports(result: TestResult, run_dir: str) -> None:
    """Write individual error reports for failed steps."""
    if not result.errors:
        return

    errors_dir = Path(run_dir) / "errors"
    errors_dir.mkdir(parents=True, exist_ok=True)

    for error in result.errors:
        error_id = error.get("id", "unknown")
        error_path = errors_dir / f"{result.story_id}-{error_id}.md"
        content = (
            f"### {error.get('title', 'Error')}\n"
            f"- **Severity:** major\n"
            f"- **Description:** {sanitize(error.get('description', ''))}\n"
        )
        error_path.write_text(content, encoding="utf-8")


async def run_test_pipeline(
    config: ExecConfig,
    run_dir: str,
    changed_only: bool = False,
    ai_mode: bool = False,
    story_filter: str = "",
    concurrency: int = 0,
) -> dict[str, Any]:
    """Run the full test pipeline: load scripts, filter, execute, write reports.

    Returns summary dict with pass/fail counts.
    """
    # Load and filter scripts
    scripts = load_test_scripts(config)
    if not scripts:
        return {"passed": 0, "failed": 0, "skipped": 0, "total": 0}

    # Filter by story ID(s) if specified (comma-separated)
    if story_filter:
        filter_ids = {s.strip() for s in story_filter.split(",")}
        scripts = [s for s in scripts if s.story_id in filter_ids]
        log.info(f"  Story filter: {len(scripts)} script(s) matching {story_filter}")

    scripts = _filter_by_tags(scripts, config)

    if changed_only:
        scripts = _filter_changed_only(scripts, config)

    if not scripts:
        log.info("  No scripts to run after filtering")
        return {"passed": 0, "failed": 0, "skipped": 0, "total": 0}

    log.info(f"  Running {len(scripts)} test scripts")

    # Create run directory
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / "errors").mkdir(exist_ok=True)
    (Path(run_dir) / "screenshots").mkdir(exist_ok=True)

    # Execute
    effective_concurrency = concurrency if concurrency > 0 else config.agent_count
    results = await _run_scripts_tiered(scripts, config, run_dir, effective_concurrency)

    # Write reports
    for result in results:
        _write_execution_report(result, run_dir)
        _write_error_reports(result, run_dir)

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total_errors = sum(len(r.errors) for r in results)

    log.info(f"\n  Results: {passed} passed, {failed} failed, {total_errors} error(s) reported")

    # Write summary
    summary_path = Path(run_dir) / "summary.md"
    summary_path.write_text(
        f"# Pipeline Summary\n\n"
        f"| Metric | Value |\n"
        f"|--------|-------|\n"
        f"| **Stories Tested** | {len(results)} |\n"
        f"| **Passed** | {passed} |\n"
        f"| **Failed** | {failed} |\n"
        f"| **Errors** | {total_errors} |\n",
        encoding="utf-8",
    )

    return {
        "passed": passed,
        "failed": failed,
        "skipped": len(load_test_scripts(config)) - len(scripts),
        "total": len(results),
        "errors": total_errors,
        "run_dir": run_dir,
    }
