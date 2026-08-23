# triage-report pipeline

The worked example from [the publishing guide](../../docs/publishing-reports.md). It runs a JQL
query, counts the backlog deterministically, has an agent write the commentary, renders a page, and
opens a pull request against `gh-pages`. Merging it publishes the report.

```bash
lockstep lint --root .
lockstep doctor --root .
lockstep compile --root .

cd extensions && uv run --with-editable . python -m pytest tests -q
```

One agent, three deterministic steps. The counting is a script so the published numbers are
arithmetic; the rendering is a script so the agent's output is text placed into a template rather
than markup it wrote.
