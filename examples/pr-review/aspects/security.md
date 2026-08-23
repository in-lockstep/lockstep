---
name: security
title: Security
summary: Whether this change introduces a way in
---

Look for the ways this diff could be exploited, not for the absence of best practices.

Concretely: input that reaches a query, a filesystem path, a shell, or a template without being
constrained. Authorization checks that a new code path bypasses. Secrets that reach a log, an error
message, or a response body. Data crossing a trust boundary that the receiving side assumes is
already validated.

Say what an attacker would do, in order. "This is unsanitized" is not a finding; "a `name` of
`../../etc/passwd` reaches `open()` on line 84" is.

Do not report the absence of a control that this codebase does not use anywhere. That is a design
discussion, not a review of this change.
