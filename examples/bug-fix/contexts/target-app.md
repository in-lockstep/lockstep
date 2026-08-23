---
name: target-app
description: The application being fixed
---

The application is a Python service checked out at `outputs/target`. It uses pytest, with tests
mirroring the source layout under `tests/`.

Conventions worth respecting because reviewers will notice them:

- Type hints everywhere; `mypy --strict` runs in the project's own CI.
- Errors are raised, not returned as sentinels.
- Public functions have docstrings; private helpers usually do not.

The service handles customer orders, so bug reports frequently quote real order payloads.
