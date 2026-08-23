---
name: common
description: What this pipeline adds to the shipped baseline
enforce:
  permissions: read-all
  deny-tools: [write_file, delete_*, create_*, update_*]
---

You MUST NOT state a file path, a symbol, or a line number you have not read. In this pipeline an
unverified path becomes a patch against a file that may not exist.
