# Extracted from pipeline-framework scripts/generate-report.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports were adapted.
"""Turn a run directory of execution reports into dashboard data."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def build_dashboard_data(run_path: Path, output_dir: str) -> dict[str, Any]:
    """Build rich dashboard data from execution reports."""
    exec_dir = run_path / "executions"
    stories_data: list[dict[str, Any]] = []
    passed_total = 0
    failed_total = 0
    all_tags: set[str] = set()
    step_rates: list[float] = []

    cls_path = run_path / "heal-classifications.json"
    classifications: dict = {}
    if cls_path.exists():
        try:
            classifications = json.loads(cls_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            pass

    app_bug_count = sum(
        1 for c in classifications.values() if isinstance(c, dict) and c.get("category") == "app_bug"
    )

    if exec_dir.exists():
        for md_file in sorted(exec_dir.glob("*.md")):
            content = md_file.read_text(encoding="utf-8")
            story_id = md_file.stem
            is_passed = "**PASSED**" in content

            if is_passed:
                passed_total += 1
            else:
                failed_total += 1

            steps: list[dict[str, Any]] = []
            tool_pattern = re.compile(r"- \*\*Tool:\*\* `(.+?)`")
            expected_pattern = re.compile(r"- \*\*Expected:\*\* (.+?)$", re.MULTILINE)
            result_pattern = re.compile(r"- \*\*Result:\*\* (.*?)(?=\n\n|\n###|\Z)", re.DOTALL)
            phase_pattern = re.compile(r"## (\w+) Phase")
            screenshot_pattern = re.compile(r"- \*\*Screenshot:\*\* !\[\]\((.+?)\)")

            sections = re.split(r"### \[", content)
            for section in sections[1:]:
                status_match = re.match(r"(PASS|FAIL|SKIP)\] Step (\d+): (.+?)$", section, re.MULTILINE)
                if not status_match:
                    continue
                status_map = {"PASS": "passed", "FAIL": "failed", "SKIP": "skipped"}
                step_status = status_map.get(status_match.group(1), "failed")

                tool_m = tool_pattern.search(section)
                expected_m = expected_pattern.search(section)
                result_m = result_pattern.search(section)
                screenshot_m = screenshot_pattern.search(section)

                phase = "test"
                pos = content.find(section)
                for pm in phase_pattern.finditer(content):
                    if pm.start() < pos:
                        phase = pm.group(1).lower()

                steps.append(
                    {
                        "phase": phase,
                        "stepNumber": status_match.group(2),
                        "tool": tool_m.group(1) if tool_m else "",
                        "action": status_match.group(3),
                        "expected": expected_m.group(1) if expected_m else "",
                        "result": (result_m.group(1).strip()[:500] if result_m else ""),
                        "status": step_status,
                        "screenshotPath": screenshot_m.group(1) if screenshot_m else "",
                    }
                )

            total_steps = len(steps)
            passed_steps = sum(1 for s in steps if s["status"] in ("passed", "warn"))
            if total_steps > 0 and not is_passed:
                step_rates.append(passed_steps / total_steps * 100)

            script_path = Path(output_dir) / "test-scripts" / f"{story_id}.json"
            tags: list[str] = []
            summary = ""
            if script_path.exists():
                try:
                    script_data = json.loads(script_path.read_text(encoding="utf-8"))
                    tags = script_data.get("tags", [])
                    summary = script_data.get("summary", "")
                except (json.JSONDecodeError, ValueError):
                    pass

            if story_id in classifications and classifications[story_id].get("category") == "app_bug":
                if "app-bug" not in tags:
                    tags.append("app-bug")

            all_tags.update(tags)

            stories_data.append(
                {
                    "storyId": story_id,
                    "summary": summary,
                    "status": "passed" if is_passed else "failed",
                    "passed": is_passed,
                    "tags": tags,
                    "steps": steps,
                    "executionReport": True,
                }
            )

    tested = passed_total + failed_total

    mean_step = 0.0
    if len(step_rates) > 2:
        sorted_rates = sorted(step_rates)
        trimmed = sorted_rates[1:-1]
        mean_step = sum(trimmed) / len(trimmed)
    elif step_rates:
        mean_step = sum(step_rates) / len(step_rates)

    return {
        "stories": stories_data,
        "grandTotals": {"passed": passed_total, "failed": failed_total, "skipped": 0},
        "passRate": f"{passed_total * 100 / tested:.1f}" if tested > 0 else "0",
        "meanStepRate": f"{mean_step:.1f}",
        "tags": sorted(all_tags),
        "appBugCount": app_bug_count,
        "runTimestamp": run_path.name,
    }
