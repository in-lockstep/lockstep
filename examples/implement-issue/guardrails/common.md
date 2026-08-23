---
name: common
description: What this pipeline adds to the shipped baseline
enforce:
  permissions: read-all
  deny-tools: [write_file, delete_*, create_*, update_*]
---

You MUST NOT state a file path or a symbol you have not read. A change written against a file you
assumed exists fails at apply time, long after the reasoning that produced it is out of view.
