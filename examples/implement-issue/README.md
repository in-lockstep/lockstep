# implement-issue pipeline

The worked example from [the chat-ops guide](../../docs/implementing-issues.md). Given an issue key
and a branch it interprets the requirements, plans the change, writes tests, writes the code, waits
for the repository's own CI, and opens a pull request with the plan as a comment.

A reviewer then replies with ordinary PR review comments and types `/implement`. The same pipeline
runs again with those comments as input.

```bash
lockstep lint --root .
lockstep doctor --root .
lockstep compile --root .

cd extensions && uv run --with-editable . python -m pytest tests -q
```

Four agents read source and propose changes. **None can write anything.** A gate job checks who asked
before any of it runs, and one job at the end holds the only write permission.
