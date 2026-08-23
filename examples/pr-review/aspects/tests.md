---
name: tests
title: Tests
summary: Whether the change is actually covered
---

Judge whether the tests here would fail if the change were wrong.

Concretely: a new branch or condition with no test that exercises it. A test that asserts the
function was called rather than what it produced. A test whose assertions would pass against the old
code as well as the new. Error paths with no coverage at all.

Say which specific case is untested and what it would look like. "Needs more tests" is not
actionable; "nothing covers the path where `items` is empty, which is the case the issue described"
is.

Existing tests the change breaks or weakens matter more than missing new ones.
