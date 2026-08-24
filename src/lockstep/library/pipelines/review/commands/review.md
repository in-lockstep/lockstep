---
name: review
description: Review a pull request, one review per requested aspect
parameters:
  - name: pr
    default: ""
    description: Pull request number, when dispatched rather than commented
  - name: force
    default: false
    description: Review again even where the pull request has not moved
guardrails: [reviewing]
github:
  triggers:
    workflow_dispatch: true
  command:
    name: "/review"
    events: [issue_comment, pull_request_review_comment]
    roles: [admin, maintain, write]
    # Who counts as part of this project. On a public repository the permission check alone is not
    # enough — add CONTRIBUTOR here to let outside contributors review their own pull requests.
    associations: [OWNER, MEMBER, COLLABORATOR]
---

## Steps

Each aspect is an agent, not a data file. That is what makes a lens testable — `evals/security-reviewer/`
holds diffs with planted vulnerabilities, and the eval gate answers whether that lens finds them —
and it is what lets a lens carry its own model, its own budget, and its own knowledge of a codebase.
Adding an aspect means adding an agent, a step here, and its eval cases.

1. **Fetch the pull request** → builtin: pr-diff
   - id: diff
   - args: --pr="{pull_request}" --output={output_dir}/diff.json

2. **Work out which reviews are still due** → builtin: review-state
   - id: state
   - emits: pending
   - args: --pr="{pull_request}" --requested="{positional}" --available=security,intent,performance,tests --output-dir={output_dir}/pending

3. **Review for security** → agent: security-reviewer
   (if security in {state.pending})
   - input: {output_dir}/pending/security.json
   - output: {output_dir}/reviews/security.json
   - context-files: {output_dir}/diff.json

4. **Review for intent** → agent: intent-reviewer
   (if intent in {state.pending})
   - input: {output_dir}/pending/intent.json
   - output: {output_dir}/reviews/intent.json
   - context-files: {output_dir}/diff.json

5. **Review for performance** → agent: performance-reviewer
   (if performance in {state.pending})
   - input: {output_dir}/pending/performance.json
   - output: {output_dir}/reviews/performance.json
   - context-files: {output_dir}/diff.json

6. **Review for test coverage** → agent: tests-reviewer
   (if tests in {state.pending})
   - input: {output_dir}/pending/tests.json
   - output: {output_dir}/reviews/tests.json
   - context-files: {output_dir}/diff.json

7. **Post one review per aspect** → builtin: post-reviews
   - id: post
   - args: --pr="{pull_request}" --reviews={output_dir}/reviews --pending={output_dir}/pending --diff={output_dir}/diff.json
