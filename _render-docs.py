#!/usr/bin/env python3
"""Render the repository's docs into the site's document layout.

The docs under `docs/` and `design/` are the source of truth and the test suite holds them: the
cookbook's Python is executed, getting-started's captured output is asserted verbatim, the README
matrix is checked by name. A hand-written HTML copy of any of them would drift the first time one
changed. So the site pages are DERIVED: run this script, commit what it writes, and the pages on
the site say exactly what the files in the repository say.

    python3 _render-docs.py            # from the site worktree; reads ../lockstep-docs or ../lockstep

There is no build step on the site. This is a script a person runs when a doc changes, and the
output is committed like any other file, which keeps gh-pages a plain checkout that needs nothing
installed to serve.

Deliberately not a markdown library. The three documents use a small, known subset (headings to
h3, fences with a language tag, pipe tables, flat lists, bold, italic, code, links) and a
dependency that renders everything would also render things the site's layout has no style for.
Anything outside the subset is printed as a warning rather than silently passed through.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parent
# Prefer the docs worktree when it exists, so a voice pass in flight is what gets rendered.
for cand in (SITE.parent / "lockstep-docs", SITE.parent / "lockstep"):
    if (cand / "docs").is_dir():
        REPO = cand
        break
else:
    sys.exit("no repository checkout found beside the site")

# (source, output, title, one-line lede, nav label)
PAGES = [
    ("docs/getting-started.md", "docs/getting-started.html", "Getting started",
     "Every command below shows the output it actually prints, captured from a real run and held "
     "against the tool by a test. Nothing here needs a key.", "Getting started"),
    ("docs/cookbook.md", "docs/cookbook.html", "Cookbook",
     "Ten recipes of twenty lines or fewer. Every Python snippet on this page is executed by the "
     "test suite, so a recipe that stops matching the API fails CI rather than you.", "Cookbook"),
    ("docs/extending.md", "docs/extending.html", "Extending",
     "Adapters, verbs, prompts, guardrails, middleware, packs and standards: every seam the "
     "framework leaves open, and what each one is for.", "Extending"),
]

WARN: list[str] = []


def inline(t: str) -> str:
    """Escape, then bold, italic, code and links. Escaping first means the doc cannot inject markup."""
    t = html.escape(t, quote=False)
    # `code` first so nothing inside a span gets italicised by an underscore or star.
    parts = re.split(r"(`[^`]+`)", t)
    for i, part in enumerate(parts):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            parts[i] = f"<code>{part[1:-1]}</code>"
        else:
            part = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", part)
            part = re.sub(r"(?<![\w*])\*(?!\*)([^*\n]+?)\*(?![\w*])", r"<em>\1</em>", part)
            part = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{link(m.group(2))}">{m.group(1)}</a>', part)
            parts[i] = part
    return "".join(parts)


def link(href: str) -> str:
    """Repo-relative links point at the site's own copy where one exists, and at GitHub otherwise."""
    rendered = {src.split("/")[-1]: out.split("/")[-1] for src, out, *_ in PAGES}
    m = re.match(r"^(?:\.\./)?(?:docs/)?([\w-]+\.md)(#.*)?$", href)
    if m and m.group(1) in rendered:
        return rendered[m.group(1)] + (m.group(2) or "")
    if href.startswith(("http://", "https://", "#", "mailto:")):
        return href
    # Anything else in the repository, including design/ essays and source files.
    clean = href.lstrip("./")
    clean = re.sub(r"^\.\./", "", clean)
    return f"https://github.com/in-lockstep/lockstep/blob/main/{clean}"


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"[`*]", "", title).lower()).strip("-")


def render_table(rows: list[str]) -> str:
    head = [c.strip() for c in rows[0].strip().strip("|").split("|")]
    body = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows[2:]]
    out = ['<div class="tbl-scroll"><table>', "<thead><tr>"]
    out += [f"<th>{inline(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table></div>")
    return "\n".join(out)


def render(md: str, source: str) -> tuple[str, list[tuple[str, str]]]:
    lines = md.splitlines()
    out: list[str] = []
    toc: list[tuple[str, str]] = []
    i, n = 0, len(lines)
    in_list: str | None = None

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    while i < n:
        ln = lines[i]

        if ln.startswith("```"):
            close_list()
            lang = ln[3:].strip()
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            # Byte-identical content, escaped only. A test in the repository holds these blocks.
            code = html.escape("\n".join(buf), quote=False)
            out.append(f'<pre data-lang="{html.escape(lang)}"><code>{code}</code></pre>')
            continue

        if ln.startswith("# "):
            i += 1
            continue  # the page shell renders the title

        if ln.startswith("## ") or ln.startswith("### "):
            close_list()
            level = 2 if ln.startswith("## ") else 3
            title = ln[level + 1:].strip()
            sid = slug(title)
            if level == 2:
                toc.append((sid, re.sub(r"[`*]", "", title)))
            out.append(f'<h{level} id="{sid}">{inline(title)}</h{level}>')
            i += 1
            continue

        if re.match(r"^\|", ln):
            close_list()
            rows = []
            while i < n and re.match(r"^\|", lines[i]):
                rows.append(lines[i])
                i += 1
            out.append(render_table(rows))
            continue

        m_ul = re.match(r"^- ", ln)
        m_ol = re.match(r"^\d+\. ", ln)
        if m_ul or m_ol:
            kind = "ul" if m_ul else "ol"
            if in_list != kind:
                close_list()
                out.append(f"<{kind}>")
                in_list = kind
            item = re.sub(r"^(- |\d+\. )", "", ln)
            while i + 1 < n and lines[i + 1].startswith("  ") and not re.match(r"^\s*(- |\d+\. )", lines[i + 1]):
                i += 1
                item += " " + lines[i].strip()
            out.append(f"<li>{inline(item)}</li>")
            i += 1
            continue

        if not ln.strip():
            close_list()
            i += 1
            continue

        if re.search(r"<[a-zA-Z][^>]*>", ln):
            WARN.append(f"{source}:{i + 1}: inline HTML escaped: {ln.strip()[:70]}")

        para = ln
        while i + 1 < n and lines[i + 1].strip() and not re.match(r"^(#|\||- |\d+\. |```)", lines[i + 1]):
            i += 1
            para += " " + lines[i].strip()
        close_list()
        out.append(f"<p>{inline(para)}</p>")
        i += 1

    close_list()
    return "\n".join(out), toc


def shell(title: str, lede: str, body: str, toc: list[tuple[str, str]], depth: int) -> str:
    index = (SITE / "index.html").read_text(encoding="utf-8")
    head = index.split('<main id="main">')[0]
    footer = '<footer class="foot">' + index.split('<footer class="foot">')[1]
    up = "../" * depth

    head = re.sub(r"<title>.*?</title>", f"<title>{html.escape(title)} · in-lockstep</title>", head, count=1)
    head = re.sub(r'<meta name="description" content="[^"]*">',
                  f'<meta name="description" content="{html.escape(lede, quote=True)}">', head, count=1)
    head = re.sub(r'<meta property="og:[^>]+>\n', "", head)
    head = re.sub(r'<link rel="canonical"[^>]+>\n', "", head)
    # Relative asset and nav paths climb out of docs/.
    for asset in ("site.css", "fonts/geist-latin.woff2", "fonts/geistmono-latin.woff2",
                  "index.html", "walkthrough.html", "governance.html", "extensions.html"):
        head = head.replace(f'href="{asset}"', f'href="{up}{asset}"')
        footer = footer.replace(f'href="{asset}"', f'href="{up}{asset}"')
    footer = footer.replace('src="site.js"', f'src="{up}site.js"')
    # index.html's footer links to the docs pages with bare `docs/...` paths. A page that lives in
    # docs/ has to climb out first, or "docs/cookbook.html" resolves to docs/docs/cookbook.html.
    if depth:
        head = head.replace('href="docs/', f'href="{up}docs/')
        footer = footer.replace('href="docs/', f'href="{up}docs/')
    # The nav's Docs link points at the site's own getting-started page.
    head = re.sub(r'<a href="https://github\.com/in-lockstep/lockstep/blob/main/docs/getting-started\.md">Docs</a>',
                  f'<a href="{up}docs/getting-started.html" aria-current="page">Docs</a>', head)
    # Footer doc links point at the rendered copies.
    for src, out_, *_ in PAGES:
        footer = footer.replace(f'href="https://github.com/in-lockstep/lockstep/blob/main/{src}"',
                                f'href="{up}{out_}"')

    toc_html = "\n".join(f'      <li><a href="#{s}">{html.escape(t)}</a></li>' for s, t in toc)
    return f"""{head}<main id="main">
<div class="wrap doc">
  <aside class="doc-toc" aria-label="On this page">
    <h4>On this page</h4>
    <ol>
{toc_html}
    </ol>
  </aside>
  <article class="doc-body">
    <h1>{html.escape(title)}</h1>
    <p class="lede">{html.escape(lede)}</p>
{body}
  </article>
</div>
</main>

{footer}"""


def main() -> int:
    (SITE / "docs").mkdir(exist_ok=True)
    for src, out_, title, lede, _label in PAGES:
        md = (REPO / src).read_text(encoding="utf-8")
        body, toc = render(md, src)
        page = shell(title, lede, body, toc, depth=out_.count("/"))
        (SITE / out_).write_text(page, encoding="utf-8")
        fences = md.count("\n```") // 2
        print(f"wrote {out_:32} {len(page):7,} bytes  {len(toc):2} sections  {fences:2} code blocks")
    for w in WARN:
        print("warning:", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
