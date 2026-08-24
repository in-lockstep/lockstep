"""Rendering a stored report through one of the templates we ship."""

from pathlib import Path
from typing import Any

from ..repo import load_report

TEMPLATES = Path(__file__).parent / "templates"


def export_report(report_id: str, template_name: str) -> str:
    template = TEMPLATES / template_name
    return render(template.read_text(encoding="utf-8"), load_report(report_id))


def render(template: str, report: dict[str, Any]) -> str:
    for key, value in report.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template
