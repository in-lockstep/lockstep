---
name: validate-api
description: Generate a contract test for every endpoint, run them all, and publish the report
parameters:
  - name: endpoints
    default: ""
    description: Comma-separated endpoint keys to limit the run to
  - name: skip-generation
    default: false
    description: Run the committed tests without regenerating any
guardrails: [common]
github:
  triggers:
    workflow_dispatch: true
    schedule: '0 6 * * 1-5'
  # Generated tests reach the repository through review. Once merged they run for nothing.
  propose:
    source: "{output_dir}/test-scripts"
    destination: test-scripts
    branch: pipeline/contract-tests
    title: "Generated contract tests"
---

## Steps

1. **List the API surface** → script: scripts/list-endpoints.py
   - id: list-endpoints
   - args: --output={output_dir}/endpoints.json --only="{endpoints}"
   - fingerprint: curl -sf {api_url}/spec.json | shasum -a 256 | cut -d' ' -f1

2. **Write a contract test for each endpoint** → agent: test-writer
   - foreach: endpoint in {output_dir}/endpoints.json
   - output: {output_dir}/test-scripts
   - parallel: 4
   - min-success-rate: 0.9
   (if not --skip-generation)

3. **Check the generated tests are well formed** → builtin: validate-schema
   - args: --dir={output_dir}/test-scripts --require=storyId,testSteps

4. **Run the contract tests** → builtin: test-runner
   - args: --scripts-dir={output_dir}/test-scripts --run-dir={output_dir}/runs/current --parallel=4

5. **Render and publish the report** → builtin: report
   - args: --run-dir={output_dir}/runs/current --output-dir={output_dir}
