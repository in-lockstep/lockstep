#!/usr/bin/env python3
"""Render the report as a page GitHub Pages can serve.

Deterministic on purpose, for two reasons. The published site is a pull request diff somebody
reviews, so a rendering that varied between identical reports would produce noise nobody can read
past. And a model asked to emit HTML will eventually emit HTML that does something — rendering here
means the agent's output is only ever text placed into a template.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.6 ui-serif, Georgia, serif; max-width: 46rem; margin: 0 auto; padding: 3rem 1.25rem 6rem; }}
  h1 {{ font-family: ui-sans-serif, system-ui, sans-serif; line-height: 1.1; margin-bottom: .25rem; }}
  .meta {{ font: .8rem ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .05em;
           text-transform: uppercase; opacity: .65; margin-bottom: 2.5rem; }}
  h2 {{ font-family: ui-sans-serif, system-ui, sans-serif; margin-top: 2.5rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .95rem; }}
  th {{ text-align: left; font-family: ui-sans-serif, system-ui, sans-serif; font-size: .78rem;
        text-transform: uppercase; letter-spacing: .08em; opacity: .65; padding-bottom: .4rem; }}
  td, th {{ border-bottom: 1px solid color-mix(in srgb, currentColor 15%, transparent); padding: .5rem .75rem .5rem 0; }}
  code {{ font-size: .85em; }}
  .note {{ opacity: .7; font-size: .9rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="meta">{generated} &middot; {total} issues &middot; query <code>{jql}</code></p>
{body}
<h2>Counts</h2>
{counts}
<p class="note">Counted from the tracker; the commentary above was written from these numbers.</p>
</body>
</html>
"""


def table(title: str, rows: dict[str, int]) -> str:
    if not rows:
        return ""
    cells = "".join(
        f"<tr><td>{html.escape(str(key))}</td><td>{value}</td></tr>" for key, value in rows.items()
    )
    return f"<h3>{html.escape(title)}</h3><table><tr><th>{html.escape(title)}</th><th>Count</th></tr>{cells}</table>"


def render_body(report: dict[str, Any]) -> str:
    """Place the agent's text into the page. Escaped: it is text, never markup."""
    parts = []
    if report.get("headline"):
        parts.append(f"<p><strong>{html.escape(report['headline'])}</strong></p>")
    for section in report.get("sections", []):
        heading = html.escape(str(section.get("heading", "")))
        parts.append(f"<h2>{heading}</h2>")
        for paragraph in section.get("paragraphs", []):
            parts.append(f"<p>{html.escape(str(paragraph))}</p>")
        if section.get("items"):
            items = "".join(f"<li>{html.escape(str(item))}</li>" for item in section["items"])
            parts.append(f"<ul>{items}</ul>")
    return "\n".join(parts)


def render(report: dict[str, Any], summary: dict[str, Any], title: str, jql: str, now: datetime) -> str:
    counts = "".join(
        table(label, summary.get(key, {}))
        for label, key in (("Type", "by_type"), ("Priority", "by_priority"), ("Component", "by_component"))
    )
    return PAGE.format(
        title=html.escape(title),
        generated=now.strftime("%Y-%m-%d %H:%M UTC"),
        total=summary.get("total", 0),
        jql=html.escape(jql),
        body=render_body(report),
        counts=counts,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--title", default="Triage report")
    parser.add_argument("--jql", default="")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    page = render(report, summary, args.title, args.jql, datetime.now(UTC))
    (output / "index.html").write_text(page, encoding="utf-8")
    # Pages runs Jekyll by default, which would ignore anything it does not recognise.
    (output / ".nojekyll").write_text("", encoding="utf-8")
    print(f"rendered the site -> {output / 'index.html'}")


if __name__ == "__main__":
    main()
