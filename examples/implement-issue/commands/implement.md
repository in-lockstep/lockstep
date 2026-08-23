---
name: implement
description: Implement one issue on a branch, validate it against the repository's own CI, and open a pull request carrying the plan
parameters:
  - name: issue
    default: ""
    description: The issue key to implement
  - name: branch
    default: ""
    description: The branch to implement it on
  - name: instruction
    default: ""
    description: Extra direction for this run, beyond the issue itself
guardrails: [common]
github:
  triggers:
    workflow_dispatch: true
  # Chat ops. The same pipeline is reachable two ways: dispatched with inputs, or invoked from a
  # comment on the pull request it produced. `roles` decides who may invoke it — a comment trigger
  # runs with the repository's token, and anyone who can comment can fire one.
  command:
    name: "/implement"
    events: [issue_comment, pull_request_review_comment]
    roles: [admin, maintain, write]
    arguments: [issue, branch]
  propose:
    source: "{output_dir}/change"
    destination: .
    branch: "{branch}"
    title: "Implement {issue}"
    labels: "pipeline,needs-review"
---

## Steps

1. **Fetch the issue** → builtin: issue-fetch
   - id: fetch-issue
   - args: --issue="{issue}" --output={output_dir}/issue.json

2. **Collect review feedback from the pull request** → builtin: pr-feedback
   - id: feedback
   - args: --pr="{pull_request}" --output={output_dir}/feedback.json

3. **Interpret the requirements** → agent: requirements-analyst
   - input: {output_dir}/issue.json
   - output: {output_dir}/requirements.json
   - context-files: {output_dir}/feedback.json

4. **Plan the change** → agent: planner
   - input: {output_dir}/requirements.json
   - output: {output_dir}/plan.json
   - context-files: {output_dir}/feedback.json

5. **Write tests the repository's CI will run** → agent: test-writer
   - input: {output_dir}/plan.json
   - output: {output_dir}/change/tests
   - context-files: {output_dir}/requirements.json

6. **Write the change** → agent: change-writer
   - input: {output_dir}/plan.json
   - output: {output_dir}/change/src
   - context-files: {output_dir}/requirements.json, {output_dir}/feedback.json

7. **Render the plan for review** → script: scripts/render-plan.py
   - args: --plan={output_dir}/plan.json --output={output_dir}/change/PLAN.md
