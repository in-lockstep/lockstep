---
name: review
description: Review a pull request, one review per requested aspect
parameters:
  - name: pr
    default: ""
    description: Pull request number, when dispatched rather than commented
  - name: aspects
    default: ""
    description: Which aspects to review, space separated. Empty reviews all of them.
  - name: force
    default: false
    description: Review again even where the pull request has not moved
guardrails: [common]
github:
  triggers:
    workflow_dispatch: true
  # `/review security intent` asks for two reviews. How many is not known until somebody types it,
  # so the words after the command arrive as a list rather than as named arguments.
  command:
    name: "/review"
    events: [issue_comment, pull_request_review_comment]
    roles: [admin, maintain, write]
    # Who counts as part of this project. On a public repository the permission check alone is not
    # enough — add CONTRIBUTOR here to let outside contributors review their own pull requests.
    associations: [OWNER, MEMBER, COLLABORATOR]
---

## Steps

1. **Work out what was asked for** → script: scripts/select-aspects.py
   - id: select
   - args: --requested="{positional}" --aspects-dir=aspects --output={output_dir}/aspects.json

2. **Fetch the pull request** → builtin: pr-diff
   - id: diff
   - args: --pr="{pull_request}" --output={output_dir}/diff.json

3. **Drop the aspects the pull request has not moved past** → builtin: review-state
   - id: state
   - args: --pr="{pull_request}" --aspects={output_dir}/aspects.json --output={output_dir}/pending.json

4. **Review one aspect** → agent: pr-reviewer
   - foreach: aspect in {output_dir}/pending.json
   - output: {output_dir}/reviews
   - parallel: 4
   - min-success-rate: 0.75
   - context-files: {output_dir}/diff.json

5. **Post one review per aspect** → script: scripts/post-reviews.sh
   - id: post
   - args: --pr="{pull_request}" --reviews={output_dir}/reviews --diff={output_dir}/diff.json
