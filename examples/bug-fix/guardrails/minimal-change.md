---
name: minimal-change
description: Constraints on what a fix may be
---

A fix MUST change as little as possible to make the reproducer pass.

You MUST NOT reformat code you are not fixing, rename anything for clarity, upgrade a dependency, or
"tidy" adjacent code. Every unrelated line in a diff is a line a reviewer has to read and a risk
nobody asked for.

You MUST NOT modify tests to make them pass. If the reproducer is wrong, say so — do not weaken it.

You MUST NOT touch CI configuration, workflow files, or this pipeline's own definitions.
