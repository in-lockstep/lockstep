---
name: triage
description: Search the tracker, write a triage report, and propose it to the site branch
parameters:
  - name: jql
    default: "project = APP AND status = 'Needs Triage' ORDER BY created DESC"
    description: The query defining what gets triaged
  - name: limit
    default: "60"
    description: How many issues to read
  - name: title
    default: "Triage report"
    description: Title shown on the published page
guardrails: [common]
github:
  triggers:
    workflow_dispatch: true
    schedule: '0 7 * * 1'
  # The report is proposed onto the branch GitHub Pages serves. It targets `gh-pages` rather than the
  # branch the run happened on, because a published site's contents have nothing to do with the
  # source that generated them.
  propose:
    source: "{output_dir}/site"
    destination: "."
    base: gh-pages
    branch: report/triage
    title: "{title}"
    labels: "report,triage,needs-review"
---

## Steps

1. **Search the tracker** → builtin: jql-search
   - id: search
   - args: --jql="{jql}" --limit={limit} --output={output_dir}/issues.json

2. **Summarize the backlog deterministically** → script: scripts/summarize.py
   - id: summarize
   - args: --input={output_dir}/issues.json --output={output_dir}/summary.json

3. **Write the triage report** → agent: triage-reporter
   - input: {output_dir}/summary.json
   - output: {output_dir}/report.json
   - context-files: {output_dir}/issues.json

4. **Render the site** → script: scripts/render-site.py
   - id: render
   - args: --report={output_dir}/report.json --summary={output_dir}/summary.json --title="{title}" --output-dir={output_dir}/site
