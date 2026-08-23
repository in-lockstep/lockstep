---
name: patch-format
description: The diff format the pipeline applies
---

Emit a unified diff with `a/` and `b/` prefixes, exactly as `git diff` produces:

```diff
diff --git a/src/app/orders.py b/src/app/orders.py
--- a/src/app/orders.py
+++ b/src/app/orders.py
@@ -42,7 +42,7 @@ def total(items):
-    return sum(item.price for item in items)
+    return sum(item.price for item in items if item.price is not None)
```

Rules the applying step enforces, so a diff that breaks them is rejected rather than negotiated:

- Paths are relative to the repository root.
- Context lines must match the file as it currently is. Stale context does not apply.
- Nothing under `.github/`, `.pipeline/`, `commands/`, `agents/`, `guardrails/`, or `pipeline.yaml`.
  A fix that edits CI configuration is not a fix.
