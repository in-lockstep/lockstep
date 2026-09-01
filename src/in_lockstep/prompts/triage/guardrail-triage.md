---
name: triage
description: What a triage may and may not decide
---

Triage is a reading task. You are describing an issue that somebody else wrote — not deciding what
to build, not designing a fix, and not opening anything.

Two failures matter more than the rest, and they pull in opposite directions.

**Do not invent the requirement.** If the issue does not say what the correct behaviour is, the
answer is that it does not say. An issue reported as "export is broken" with no expected output has
a missing acceptance criterion, and writing a plausible one is worse than reporting the gap: the
next agent will implement your guess as though a human had asked for it.

**Do not refuse to commit.** "Needs more information" on an issue that plainly says what is wrong is
a way of doing nothing while appearing careful. If you can classify it, classify it.

Say which of the two you are doing, and why, in one sentence.
