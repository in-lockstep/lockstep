---
name: validate
description: Full validation cycle -- generate tests, run them, repair until convergence
parameters:
  - name: skip-repair
    default: false
    description: Run tests only, with no repair loop
guardrails: [common]
github:
  triggers:
    schedule: '0 3 * * *'
---

## Steps

1. **Generate test scripts** → command: generate-tests

2. **Repair loop** → command: repair
   - max-iterations: 3
   (if not --skip-repair)

3. **Render and publish the report** → builtin: report
   - args: --run-dir={output_dir}/runs/current --output-dir={output_dir}
