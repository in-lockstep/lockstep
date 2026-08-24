"""HTTP routes for the reporting service."""

from flask import Blueprint, request

from .export import export_report

bp = Blueprint("reports", __name__)


@bp.get("/reports/<report_id>")
def get_report(report_id: str) -> str:
    fmt = request.args.get("format", "pdf.tmpl")
    return export_report(report_id, fmt)
