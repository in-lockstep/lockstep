---
name: fix
description: Reproduce a reported bug, fix it, and prove the fix with the failing test
parameters:
  - name: issue
    default: ""
    description: The bug to fix — a number on GitHub, a key on Jira
  - name: source
    default: github
    description: Which tracker to read from
  - name: suite
    default: pytest
    description: The repository's own test runner
guardrails: [fixing]
github:
  triggers:
    workflow_dispatch: true
  # A reviewer who disagrees with a proposed fix says so in review comments and types `/fix`. The
  # same pipeline runs again with those comments as input.
  command:
    name: "/fix"
    events: [issue_comment, pull_request_review_comment]
    roles: [admin, maintain, write]
    arguments: [issue]
  propose:
    source: "{output_dir}/change"
    destination: .
    branch: "fix/{issue}"
    title: "Fix {issue}"
    labels: "lockstep,bug-fix,needs-review"
---

## Steps

The shape of this pipeline is its argument. A fix is only believable if something failed before it
and passes after it, so the reproducer is written *first* and proven to fail *before* the fix is
written. A pipeline that wrote both together would produce a test that passes either way and a
change nobody can tell worked.

1. **Read the bug report** → builtin: issue-fetch
   - id: bug
   - args: --source="{source}" --issue="{issue}" --output={output_dir}/bug.json

2. **Collect review feedback on an earlier attempt** → builtin: pr-feedback
   - id: feedback
   - args: --pr="{pull_request}" --output={output_dir}/feedback.json

3. **Find the cause** → agent: bug-analyst
   - input: {output_dir}/bug.json
   - output: {output_dir}/analysis.json
   - context-files: {output_dir}/feedback.json

4. **Write a test that fails because of it** → agent: reproducer-writer
   - input: {output_dir}/analysis.json
   - output: {output_dir}/change/tests

5. **Prove it fails today** → builtin: run-suite
   - id: reproduce
   - args: --overlay={output_dir}/change --suite="{suite}" --expect=fail --output={output_dir}/reproduced.json

6. **Write the fix** → agent: fix-writer
   - input: {output_dir}/analysis.json
   - output: {output_dir}/change/src
   - context-files: {output_dir}/reproduced.json, {output_dir}/feedback.json

7. **Prove the suite passes now** → builtin: run-suite
   - id: validate
   - args: --overlay={output_dir}/change --suite="{suite}" --expect=pass --output={output_dir}/validated.json

8. **Review the fix** → agent: fix-reviewer
   - input: {output_dir}/validated.json
   - output: {output_dir}/change/REVIEW.md
   - context-files: {output_dir}/analysis.json
