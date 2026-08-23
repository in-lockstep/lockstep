# Capability actions

The composite actions every compiled workflow references. The compiler emits `uses:` lines pointing
at them by commit SHA, so a pipeline pins the exact behaviour it was reviewed against.

| Action | What it does |
|---|---|
| `restore` | Rehydrates the output tree earlier jobs produced. Jobs do not share a filesystem. |
| `save` | Publishes this job's slice of that tree, including per-leg slices from a matrix. |
| `state/load`, `state/save` | Carry the SQLite state database between jobs in one scope. |
| `step-cache` | The two-layer content-addressed probe: durable artifact first, then `actions/cache`. |
| `step-cache/save` | Publishes a step's outputs to both layers, on a miss only. |

The interesting logic lives in [`pipeline-exec`](../packages/pipeline-exec), which is unit-tested;
these files stay thin on purpose. `tests/test_contract.py` asserts that every input the compiler
passes is declared here, that every output it reads exists, and that every required input is supplied
— so a change on either side fails a build rather than a scheduled run.

## Publishing

Point a pipeline's `capabilities.actions` at wherever these are published, then resolve it to a SHA:

```yaml
capabilities:
  actions: github.com/<owner>/<repo>/actions@v1.0.0
```

`lockstep compile` refuses to emit a floating ref, so `.pipeline/pins.lock` must carry the SHA.
