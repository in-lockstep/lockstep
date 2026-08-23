---
name: tracker
description: How this team's tracker is actually used, as opposed to how it is meant to be
---

Conventions that hold here, and which change what the numbers mean:

- **Priority is set at triage, not at filing.** So `unset` priority on a new issue is expected, and
  `unset` on an old one is the signal.
- **Components are optional and frequently skipped.** A missing component is the single most common
  metadata gap, and the one that most often stalls routing.
- **`Needs Triage` is the entry state.** An issue sitting in it for two weeks was not deprioritised;
  it was not looked at.
- **The `customer` label** marks issues raised through support. They carry an external commitment
  that internal issues do not.

Two things this team has decided already, so a report should not re-propose them:

- Old issues are not closed automatically. Age is a signal, not a verdict.
- Bugs are not triaged by severity guesses. Priority is set by a human reading the issue.
