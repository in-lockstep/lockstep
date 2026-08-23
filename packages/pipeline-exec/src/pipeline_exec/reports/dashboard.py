# Extracted from pipeline-framework src/reports/dashboard.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from ..config import ExecConfig
from ..logging import log


def generate_dashboard(
    run_dir: str,
    data: dict[str, Any],
    config: ExecConfig,
    template_name: str = "dashboard.html.j2",
) -> Path:
    """Generate an HTML dashboard from execution data.

    Args:
        run_dir: Path to the run directory
        data: Dashboard data (stories, steps, summaries)
        config: Pipeline config
        template_name: Jinja2 template to use

    Returns:
        Path to the generated dashboard.html
    """
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)

    # Write data to separate .js file (avoids template literal issues)
    data_path = run_path / "dashboard-data.js"
    data_json = json.dumps(data, indent=2, default=str)
    data_path.write_text(f"const DATA = {data_json};", encoding="utf-8")

    # Load template
    template_dirs = [
        Path(config.output_dir) / "templates",
        Path(__file__).parent / "templates",
    ]
    existing_dirs = [str(d) for d in template_dirs if d.exists()]

    if not existing_dirs:
        # Generate a basic dashboard without templates
        dashboard_path = run_path / "dashboard.html"
        dashboard_path.write_text(
            _default_dashboard_html(data),
            encoding="utf-8",
        )
        log.info(f"  Dashboard: {dashboard_path}")
        return dashboard_path

    env = Environment(
        loader=FileSystemLoader(existing_dirs),
        autoescape=True,
    )

    try:
        template = env.get_template(template_name)
        html = template.render(data=data, run_dir=run_dir)
    except Exception:
        html = _default_dashboard_html(data)

    dashboard_path = run_path / "dashboard.html"
    dashboard_path.write_text(html, encoding="utf-8")
    log.info(f"  Dashboard: {dashboard_path}")
    return dashboard_path


def generate_index_page(output_dir: str, config: ExecConfig) -> Path:
    """Generate the index.html page listing all runs."""
    output_path = Path(output_dir)
    runs_dir = output_path / "runs"

    runs: list[dict[str, Any]] = []
    if runs_dir.exists():
        for entry in sorted(runs_dir.iterdir(), reverse=True):
            if entry.name == "latest" or not entry.is_dir():
                continue
            run_info: dict[str, Any] = {
                "name": entry.name,
                "date": entry.name.replace("_", " "),
                "has_dashboard": (entry / "dashboard.html").exists(),
            }

            # Read dashboard data if available
            data_file = entry / "dashboard-data.js"
            if data_file.exists():
                try:
                    content = data_file.read_text(encoding="utf-8")
                    json_str = content.replace("const DATA = ", "").rstrip(";")
                    data = json.loads(json_str)
                    totals = data.get("grandTotals", {})
                    run_info["passed"] = totals.get("passed", 0)
                    run_info["failed"] = totals.get("failed", 0)
                    run_info["skipped"] = totals.get("skipped", 0)
                    run_info["pass_rate"] = data.get("passRate", "—")
                    run_info["step_completion"] = data.get("meanStepRate", "—")
                except (json.JSONDecodeError, ValueError):
                    pass

            # Read heal classifications for app bug count
            cls_file = entry / "heal-classifications.json"
            app_bugs = 0
            if cls_file.exists():
                try:
                    cls = json.loads(cls_file.read_text(encoding="utf-8"))
                    app_bugs = sum(
                        1 for c in cls.values() if isinstance(c, dict) and c.get("category") == "app_bug"
                    )
                except (json.JSONDecodeError, ValueError):
                    pass

            run_info.setdefault("passed", 0)
            run_info.setdefault("failed", 0)
            run_info.setdefault("skipped", 0)
            run_info.setdefault("pass_rate", "—")
            run_info.setdefault("step_completion", "—")
            total = run_info["passed"] + run_info["failed"] + run_info.get("skipped", 0)
            tested = run_info["passed"] + run_info["failed"]
            run_info["total"] = total
            run_info["appBugs"] = app_bugs
            run_info["runType"] = "Partial" if tested > 0 and tested < total * 0.5 else "Full"
            run_info["passRate"] = run_info.pop("pass_rate")
            run_info["stepCompletion"] = run_info.pop("step_completion")
            run_info["hasDashboard"] = run_info.pop("has_dashboard")

            runs.append(run_info)

    # Try Jinja2 template
    template_dirs = [
        Path(config.output_dir) / "templates",
        Path(__file__).parent / "templates",
    ]
    existing_dirs = [str(d) for d in template_dirs if d.exists()]

    if existing_dirs:
        try:
            env = Environment(loader=FileSystemLoader(existing_dirs), autoescape=True)
            template = env.get_template("index.html.j2")
            html = template.render(runs=runs)
            index_path = output_path / "index.html"
            index_path.write_text(html, encoding="utf-8")
            log.info(f"  Index page: {index_path}")
            return index_path
        except Exception:
            pass

    index_path = output_path / "index.html"
    index_path.write_text(_default_index_html(runs), encoding="utf-8")
    log.info(f"  Index page: {index_path}")
    return index_path


def _default_dashboard_html(data: dict[str, Any]) -> str:
    """Minimal dashboard when no Jinja2 template is available."""
    totals = data.get("grandTotals", {})
    passed = totals.get("passed", 0)
    failed = totals.get("failed", 0)
    total = passed + failed
    rate = f"{passed * 100 / total:.1f}" if total > 0 else "0"

    return f"""<!DOCTYPE html>
<html><head><title>Pipeline Dashboard</title>
<style>body{{font-family:sans-serif;padding:20px;background:#111;color:#eee}}
.card{{display:inline-block;background:#1a1d27;padding:16px;margin:8px;border-radius:8px;min-width:100px;text-align:center}}
.value{{font-size:24px;font-weight:bold}}.pass{{color:#34d399}}.fail{{color:#f87171}}</style>
</head><body>
<h1>Pipeline Dashboard</h1>
<div class="card"><div class="value pass">{rate}%</div><div>Pass Rate</div></div>
<div class="card"><div class="value pass">{passed}</div><div>Passed</div></div>
<div class="card"><div class="value fail">{failed}</div><div>Failed</div></div>
<div class="card"><div class="value">{total}</div><div>Total</div></div>
<script src="dashboard-data.js"></script>
</body></html>"""


def _default_index_html(runs: list[dict[str, Any]]) -> str:
    """Minimal index page."""
    rows = ""
    for r in runs:
        passed = r.get("passed", 0)
        failed = r.get("failed", 0)
        rate = r.get("pass_rate", "—")
        link = (
            f'<a href="runs/{r["name"]}/dashboard.html">{r["name"]}</a>'
            if r.get("has_dashboard")
            else r["name"]
        )
        rows += f"<tr><td>{link}</td><td>{rate}%</td><td>{passed}</td><td>{failed}</td></tr>\n"

    return f"""<!DOCTYPE html>
<html><head><title>Pipeline Reports</title>
<style>body{{font-family:sans-serif;padding:20px;background:#111;color:#eee}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid #333}}
a{{color:#60a5fa}}</style>
</head><body>
<h1>Pipeline Reports</h1>
<table><tr><th>Run</th><th>Pass Rate</th><th>Passed</th><th>Failed</th></tr>
{rows}</table>
</body></html>"""
