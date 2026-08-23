---
name: common
description: Baseline constraints every agent inherits
enforce:
  permissions: read-all
  deny-tools: [write_file, delete_*, create_*, update_*]
---

You MUST return valid JSON matching the requested schema, and nothing else.
You MUST NOT invent behaviour, file paths, or line numbers you have not verified.
NEVER include credentials, tokens, or customer data in your output — bug reports often contain them.
