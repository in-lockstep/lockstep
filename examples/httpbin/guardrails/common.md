---
name: common
description: What this pipeline adds to the shipped baseline
enforce:
  permissions: read-all
  deny-tools: [delete_*]
---

You MUST NOT assert anything about the target that you have not seen in a response it returned.
