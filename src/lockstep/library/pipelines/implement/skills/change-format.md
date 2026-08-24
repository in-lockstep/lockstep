---
name: change-format
description: How a proposed change is handed back
---

Write files into your output directory, laid out exactly as they should appear in the repository:
`src/reports/export.py` in your output becomes `src/reports/export.py` in the pull request.

Write whole files, not diffs. A patch that does not apply is a failed run; a file is unambiguous.

Do not create files under `.github/`, `.pipeline/`, `profiles/`, `guardrails/`, `agents/` or
`commands/`. Those are the pipeline that is running you, and a change that reaches them is refused
before it lands.

If you could not complete something, still write what you have, and say what is missing in the
`PLAN.md` or in your JSON output — whichever this step asked for. A partial change that names its
gap is reviewable. A partial change that looks complete is not.
