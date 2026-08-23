---
name: data-handling
description: What may leave this organization
sealed: true
enforce:
  permissions: read-all
  deny-tools: [write_file, create_*, update_*, delete_*]
  # Ceilings, not bands. A band bounds what a consumer may do to *our* agent; these bound every
  # agent in a consuming repository, including ones we will never see. The run cap is the one that
  # bounds a bill — per-agent ceilings do not, because a repository under them can add more agents.
  max-turns: 8
  max-ai-credits: 200
  per-run-ai-credits: 200
---

NEVER reproduce customer records, credentials, or internal hostnames in output that leaves this
organization's infrastructure.

You MUST NOT quote a value you cannot tell is synthetic. Test fixtures in this organization are
seeded from production, so a plausible-looking order id usually belongs to somebody.
