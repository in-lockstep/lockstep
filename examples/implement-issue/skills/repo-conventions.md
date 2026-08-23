---
name: repo-conventions
description: How to work out what this repository expects before writing anything
---

Read before you write. The fastest way to have a change rejected is to write correct code in a style
the project does not use.

- **Tests**: open the test file nearest what you are changing. Copy its framework, fixtures, naming
  and assertion style. Do not introduce a second way of doing the same thing.
- **Errors**: find out whether the project raises, returns sentinels, or returns result objects, and
  do that.
- **Types**: if the code is annotated, annotate. If `mypy --strict` runs in the project's CI, your
  change has to pass it.
- **Structure**: put new code where similar code already lives. A correctly-placed change is easier
  to review than a well-organised one somewhere new.
