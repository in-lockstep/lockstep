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

1. **Collect failures** → builtin: collect-failures
   - args: --run-dir={output_dir}/runs/current --output={output_dir}/failures.json

2. **Repair each failing script** → script: scripts/repair-script.py
   - foreach: failure in {output_dir}/failures.json
   - output: {output_dir}/repairs
   - args: --failure={item.key} --output={output_dir}/repairs/{item.key}.json
   - parallel: 2

3. **Check convergence** → builtin: check-convergence
   - id: check-convergence
   - args: --run-dir={output_dir}/runs/current
