---
name: reviewer
description: Review a pull request
model: { default: claude-sonnet-4-6, allow: [claude-sonnet-4-6, claude-opus-4-1] }
provider: vertex-claude
max_tool_turns: 4
guardrails: [house]
skills: [verdict]
github:
  max-ai-credits: { default: 60, min: 30, max: 200 }
  timeout-minutes: { default: 20, max: 60 }
---

You review one pull request and report what a maintainer would want to know before merging.
