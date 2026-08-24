---
name: triage
description: Read one issue and say what it is, how urgent it is, and what it is missing
parameters:
  - name: issue
    default: ""
    # The comment is on the issue, so the payload already says which. `/implement` on issue #18
    # means #18; `/implement 42` anywhere still means 42, because anything explicit wins.
    from-event: issue-number
    description: The issue to triage — a number on GitHub, a key on Jira
  - name: source
    default: github
    description: Which tracker to read from
guardrails: [triage]
github:
  triggers:
    workflow_dispatch: true
    issues: [opened, reopened]
---

## Steps

The shape is deliberately two steps. Everything tracker-specific happens in the first one and comes
out in one shape, so the agent that does the thinking has never heard of Jira or of GitHub — which
is also what lets its eval cases be about triage rather than about an API.

1. **Read the issue** → builtin: issue-fetch
   - id: issue
   - emits: writeback
   - args: --source="{source}" --issue="{issue}" --output={output_dir}/issue.json

2. **Triage it** → agent: triage-analyst
   - input: {output_dir}/issue.json
   - output: {output_dir}/triage.json

3. **Write it back to Jira** → builtin: jira-update
   (if jira in {issue.writeback})
   - id: write-back
   - args: --issue="{issue}" --from={output_dir}/triage.json --name=triage --comment --labels --priority

On GitHub there is no third step: the agent's conclusions reach the issue through gh-aw's safe
outputs, so the write happens inside step 2 and this one is skipped. The condition asks the fetch
step what it left outstanding rather than reading the `source` parameter back, because the question
is what happened rather than what was requested.
