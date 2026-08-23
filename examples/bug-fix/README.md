# bug-fix pipeline

The worked example from [the extension guide](../../docs/extending.md). It fetches triaged bugs from
Jira, analyzes them against the application source, writes reproducers, writes fixes, validates them,
reviews them adversarially, and opens a pull request.

A reviewer who disagrees with a proposed fix says so in review comments and types `/fix APP-412`.
The same pipeline runs again, narrowed to that bug, with their comments as input.

It exists to demonstrate both extension points:

- **`extensions/`** — a `pipeline-exec` extension contributing three builtins through entry points:
  `jira-fetch`, `apply-patch` (a trust boundary, enforced in code) and `run-suite`.
- **`extensions/actions/setup-target/`** — a composite action checking out the application being
  fixed, inserted into the compiled workflow by an overlay.

```bash
lockstep lint --root .
lockstep doctor --root .
lockstep compile --root .

cd extensions && uv run --with-editable . python -m pytest tests -q
```

Four agents read source and propose changes. **None of them can write anything.** One job at the end
holds the only write permission in the pipeline.
