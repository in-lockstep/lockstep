---
name: data-handling
description: What may leave this organization
sealed: true
enforce:
  permissions: read-all
  deny-tools: [write_file, create_*, update_*, delete_*]
---

NEVER reproduce customer records, credentials, or internal hostnames in output that leaves this
organization's infrastructure.

You MUST NOT quote a value you cannot tell is synthetic. Test fixtures in this organization are
seeded from production, so a plausible-looking order id usually belongs to somebody.
