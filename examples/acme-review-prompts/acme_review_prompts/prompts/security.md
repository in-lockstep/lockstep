---
model: ""
---
# Security review — Acme

Review this change for security defects that a person could exploit. Report what you can point at
in the diff; a reviewer who cannot find the line you mean cannot act on the finding.

## What this codebase gets wrong

**SQLAlchemy sessions.** A `Session` that outlives a request outlives its transaction boundary.
Look for one stored on a module global, cached on a class, or captured in a closure that is
returned. The symptom in production is a connection holding a lock across an unrelated request.

**Raw SQL through `text()`.** Parameters go in the bind, never in the f-string. `text(f"... {x}")`
is the finding, and it stays the finding when `x` looks internal — an internal value is one
refactor from a request field.

**Bare `except`.** A swallowed exception around an authorisation check reads as a pass. Where the
`try` covers a permission decision, say so and name the decision.

## What is not a finding here

A missing input length check on a field the database already constrains. A dependency version that
is merely old. Anything you would have to run the code to confirm — say what you suspect and what
would settle it, and do not report it as established.
