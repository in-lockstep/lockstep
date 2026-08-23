---
name: fix-bugs
description: Fetch triaged bugs, reproduce them, fix them, validate the fixes, review them, and open pull requests
parameters:
  - name: jql
    default: "project = APP AND type = Bug AND status = Triaged AND labels = auto-fix"
    description: Which bugs to work on
  - name: limit
    default: "5"
    description: How many bugs to attempt in one run
  - name: dry-run
    default: false
    description: Do everything except open pull requests
guardrails: [common]
github:
  triggers:
    workflow_dispatch: true
    schedule: '0 4 * * 1'
  propose:
    source: "{output_dir}/fixes"
    destination: proposed-fixes
    branch: pipeline/bug-fixes
    title: "Proposed bug fixes"
    labels: "pipeline,bug-fix,needs-review"
---

## Steps

1. **Fetch triaged bugs** → builtin: jira-fetch
   - id: fetch-bugs
   - args: --jql="{jql}" --limit={limit} --output={output_dir}/bugs.json

2. **Analyze each bug against the source** → agent: bug-analyst
   - foreach: bug in {output_dir}/bugs.json
   - output: {output_dir}/analyses
   - parallel: 3
   - min-success-rate: 0.8

3. **Write a reproducer for each analyzed bug** → agent: reproducer-writer
   - foreach: analysis in {output_dir}/analyses.json
   - output: {output_dir}/reproducers
   - parallel: 3

4. **Prove each reproducer fails before the fix** → builtin: run-suite
   - id: prove-reproducers
   - args: --repo={output_dir}/target --suite=pytest --expect=fail --output={output_dir}/reproduced.json

5. **Write a fix for each reproduced bug** → agent: fix-writer
   - foreach: bug in {output_dir}/reproduced.json
   - output: {output_dir}/patches
   - parallel: 2
   - min-success-rate: 0.5

6. **Apply the patches** → builtin: apply-patch
   - id: apply
   - args: --patch={output_dir}/patches/combined.patch --repo={output_dir}/target

7. **Prove the suite passes after the fix** → builtin: run-suite
   - id: validate-fixes
   - args: --repo={output_dir}/target --suite=pytest --expect=pass --output={output_dir}/validated.json

8. **Review the fixes** → agent: fix-reviewer
   - input: {output_dir}/validated.json
   - output: {output_dir}/review.json
   - context-files: {output_dir}/patches/combined.patch

9. **Assemble what passed review** → script: scripts/assemble-fixes.py
   - args: --review={output_dir}/review.json --patches={output_dir}/patches --output={output_dir}/fixes
   (if not --dry-run)
