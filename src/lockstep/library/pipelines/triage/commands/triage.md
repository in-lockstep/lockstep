---
name: triage
description: Read one issue and say what it is, how urgent it is, and what it is missing
parameters:
  - name: issue
    default: ""
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
   - emits: criteria_source
   - args: --source="{source}" --issue="{issue}" --output={output_dir}/issue.json

2. **Triage it** → agent: triage-analyst
   - input: {output_dir}/issue.json
   - output: {output_dir}/triage.json
