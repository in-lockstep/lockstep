---
name: minimal-change
description: Constraints on the size and shape of a change
---

Change as little as the plan requires.

You MUST NOT reformat code you are not changing, rename for clarity, upgrade a dependency, or tidy
adjacent code. Every unrelated line is a line a reviewer must read and a risk nobody asked for.

You MUST NOT weaken or delete an existing test to make your change pass. If an existing test is
wrong, say so — that is a finding, not an obstacle.

You MUST NOT touch CI configuration, workflow files, or this pipeline's own definitions.
