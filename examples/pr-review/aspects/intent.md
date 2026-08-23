---
name: intent
title: Intent
summary: Whether the change does what it says it does
---

Read the title, the description, and the linked issue. Then read the diff. Report where they
disagree.

Concretely: behaviour the description promises that the diff does not implement. Behaviour the diff
implements that nobody asked for. A change whose stated purpose is one thing and whose largest effect
is another.

Also report what the change *implies* that its author may not have noticed: a signature that other
callers depend on, a default that changes for existing users, an error that becomes silent.

If the diff does what it says, say so in one sentence. A review that manufactures a concern to seem
useful teaches people to skip it.
