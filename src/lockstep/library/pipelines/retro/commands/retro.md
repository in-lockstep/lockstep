---
name: retro
description: Read what the pipelines actually did and propose what to change about them
parameters:
  - name: days
    default: "30"
    description: How far back the window reaches
  - name: branch
    default: pipeline-history
    description: The branch the run ledger lives on
guardrails: [retro]
github:
  triggers:
    workflow_dispatch: true
    schedule: '0 6 * * 1'
---

## Steps

Two steps, and the split between them is the point. Every number is computed by code, so two runs
over the same window produce the same figures — a trend an agent averaged for itself would be a
different trend each time, and one nobody could check. The agent's job is the part arithmetic cannot
do: saying what the movement probably means and what to do about it.

1. **Read what the pipelines did** → builtin: run-history
   - id: history
   - args: --branch="{branch}" --days="{days}" --output={output_dir}/history.json

2. **Say what to change** → agent: retro-analyst
   - input: {output_dir}/history.json
   - output: {output_dir}/retro.json
