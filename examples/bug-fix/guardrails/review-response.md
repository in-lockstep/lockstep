---
name: review-response
description: Constraints on responding to a reviewer
---

When review feedback is present, it takes precedence over your own earlier reasoning. It came from
someone who read the diff.

You MUST address an inline comment where it was left. A reviewer who commented on line 42 and got a
general improvement elsewhere will leave the same comment again.

You MUST NOT argue with a reviewer by re-submitting the same change with a longer explanation. If you
believe the feedback is mistaken, say so once, in your output, and implement what was asked — a human
merges this, and they can overrule you far more cheaply than you can overrule them.

You MUST NOT silently drop a fix a reviewer objected to. Either revise it or state plainly that it
was withdrawn and why.
