---
name: codebase-navigation
description: How to find things in an unfamiliar repository
---

Start from the symptom, not the file tree. Grep for the error message, the log line, or the literal
string in the bug report. That usually lands within one function of the cause.

Then widen: read the function's callers before its callees. Most bugs are wrong assumptions at a
boundary, not wrong logic in a leaf.

Use history. `git log -S"<symbol>"` finds when a line arrived, and the commit message often explains
what it was for — which tells you whether a fix would break the thing it was written for.

Cite what you find as `path/to/file.py:120-134`. Anyone reading you should reach the same lines
without searching.
