---
name: fixing
description: What fixing a bug means here
---

**Fix the cause, not the symptom.** A `try` around the traceback makes the report go away and leaves
the bug. If you cannot find the cause, say that — a report saying "I could not locate this, here is
what I ruled out" is worth more than a change that silences it.

**Do not change the test to fit the fix.** The reproducer was written before the fix and describes
the bug. If it looks wrong, say so; editing it is how a run ends up proving nothing while appearing
green.

**Fix one bug.** Something else you noticed is a separate report. A pull request that fixes two
things is one a reviewer has to approve or reject as a unit.

**Do not touch the machinery.** Workflows, pipeline definitions, guardrails, profiles and pins are
outside every change this pipeline makes.
