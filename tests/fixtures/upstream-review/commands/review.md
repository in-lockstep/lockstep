---
name: review
description: Review a pull request on request
guardrails: [house]
github:
  triggers:
    workflow_dispatch: true
---

## Steps

1. **Collect the diff** → script: scripts/collect-diff.py
   - id: diff
   - args: --output={output_dir}/diff.json

2. **Review it** → agent: reviewer
   - input: {output_dir}/diff.json
   - output: {output_dir}/review.json
