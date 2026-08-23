---
name: common
description: What this pipeline adds to the shipped baseline
enforce:
  permissions: read-all
  deny-tools: [write_file, create_*, update_*, delete_*]
---

You MUST NOT cite a file or a line that does not appear in the diff you were given. A comment
anchored to a line the author did not touch reads as a review of somebody else's work.
