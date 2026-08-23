"""The published page is a pull request diff somebody reads, and it carries model output."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "render_site", Path(__file__).parent.parent / "scripts" / "render-site.py"
)
assert spec and spec.loader
render_site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_site)

NOW = datetime(2026, 8, 23, 7, 0, tzinfo=UTC)
REPORT = {
    "headline": "Sixty issues await triage; a third have no component.",
    "sections": [
        {
            "heading": "What stands out",
            "paragraphs": ["Checkout accounts for most of the recent bugs."],
            "items": ["APP-11 has been open 90 days"],
        }
    ],
}
SUMMARY = {"total": 60, "by_type": {"Bug": 40, "Story": 20}, "by_priority": {"High": 12}}


def render(report=REPORT, summary=SUMMARY):
    return render_site.render(report, summary, "Triage report", "project = APP", NOW)


def test_the_agents_words_reach_the_page():
    page = render()
    assert "Sixty issues await triage" in page
    assert "Checkout accounts for most" in page


def test_the_counts_come_from_the_summary_not_the_agent():
    page = render()
    assert "<td>Bug</td><td>40</td>" in page
    assert "60 issues" in page


def test_model_output_is_escaped_not_rendered():
    """A model asked for text will eventually produce markup; the page treats it as text."""
    page = render({"headline": "<script>alert(1)</script>", "sections": []})
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_an_injected_heading_cannot_break_out_of_the_template():
    page = render({"headline": "", "sections": [{"heading": "</h2><img src=x onerror=1>", "paragraphs": []}]})
    assert "<img" not in page


def test_the_rendering_is_stable():
    """The page is a diff reviewers read; churn between identical reports is noise."""
    assert render() == render()


def test_the_page_is_self_contained():
    """GitHub Pages serves it directly; a stylesheet fetched from elsewhere would not survive."""
    page = render()
    assert "<style>" in page
    assert "<link" not in page


def test_an_empty_report_still_produces_a_valid_page():
    page = render({}, {"total": 0})
    assert page.startswith("<!doctype html>")
    assert "0 issues" in page
