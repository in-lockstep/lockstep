---
name: repo
description: What this codebase is
---

A Python service. `ruff`, `mypy --strict` and `pytest` run on every pull request, so anything those
tools would catch is already caught.

All database access goes through `src/repo.py`.
