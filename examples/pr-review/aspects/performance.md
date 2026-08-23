---
name: performance
title: Performance
summary: Whether the change does more work than it needs to
---

Look for work that grows with input the author may not have considered.

Concretely: a query inside a loop. An unbounded read of something that grows. A synchronous call on
a path that was previously not blocking. Repeated computation of the same value.

Say what the cost is a function of. "This is slow" is not a finding; "this issues one query per
item, so a 500-item order does 500 round trips" is.

Do not report micro-optimisations. If the difference is not visible at the sizes this code actually
sees, it is not a review comment.
