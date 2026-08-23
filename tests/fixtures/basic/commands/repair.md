---
name: repair
description: Analyze test failures, repair the scripts, and retest until convergence
parameters:
  - name: attempt
    default: ""
    description: Label for this repair attempt
guardrails: [common]
github:
  triggers:
    workflow_dispatch: true
  converged-from: check-convergence
---

## Steps

1. **Collect failures** → script: scripts/collect-failures.py
   - args: --output={output_dir}/failures.json

2. **Check convergence** → script: scripts/check-convergence.py
   - id: check-convergence
   - args: --input={output_dir}/failures.json
