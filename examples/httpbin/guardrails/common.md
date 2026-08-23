---
name: common
description: Baseline constraints every agent inherits
enforce:
  permissions: read-all
  deny-tools: [delete_*]
---

You MUST return valid JSON and nothing else — no prose before or after it.
You MUST NOT invent behaviour that is absent from the input you were given.
NEVER include credentials, tokens, or personal data in your output.
