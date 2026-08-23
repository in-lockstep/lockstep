---
name: repository
description: The repository being changed
---

The change is made in this repository, on a branch, and validated by this repository's own CI. That
last point shapes everything: the tests you write are not run by a harness the pipeline controls —
they are run by whatever the project already runs on every pull request.

So a change is only finished when the project's real checks pass. A test that only passes under a
command the pipeline chose is not evidence.

Review feedback arrives as three kinds, and they are not equally specific:

- **Inline comments** carry a file and a line. Address them there.
- **Review summaries** carry an overall judgement and often a requested direction.
- **Discussion comments** are everything else on the pull request.

Where they disagree, the inline comment is usually the more precise instruction.
