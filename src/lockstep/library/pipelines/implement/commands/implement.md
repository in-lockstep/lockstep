---
name: implement
description: Implement one issue on a branch and open a pull request carrying the plan
parameters:
  - name: issue
    default: ""
    description: The issue to implement — a number on GitHub, a key on Jira
  - name: source
    default: github
    description: Which tracker to read from
  - name: branch
    default: ""
    description: The branch to implement it on
guardrails: [implementing]
github:
  triggers:
    workflow_dispatch: true
  # The same pipeline is reachable two ways: dispatched with inputs, or invoked from a comment on
  # the pull request it produced. `roles` decides who may invoke it — a comment trigger runs with
  # the repository's token, and anyone who can comment can fire one.
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
    labels: "lockstep,needs-review"
    # The canonical key the tracker answered with, recorded as an `Issue:` trailer on the commit.
    # From the file rather than from `{issue}`, because that parameter is what somebody typed — a
    # run invoked with `412`, or with a URL, still records `#412`. A commit that cannot find the key
    # fails rather than landing untraceable.
    issue-from: "{output_dir}/issue.json"
---

## Steps

Nothing here writes to the repository. The agents produce files under `{output_dir}/change`, and the
`propose:` block above is what turns those into a pull request — which is why every agent in this
pipeline can be read-only and why a prompt is never the thing standing between a model and `main`.

1. **Read the issue** → builtin: issue-fetch
   - id: fetch-issue
   - args: --source="{source}" --issue="{issue}" --output={output_dir}/issue.json

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

7. **Render the plan for review** → builtin: render-plan
   - args: --plan={output_dir}/plan.json --output={output_dir}/change/PLAN.md
