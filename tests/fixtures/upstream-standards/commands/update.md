---
name: update
description: Open a pull request when an upstream this repository inherits has moved
github:
  triggers:
    schedule: "17 6 * * *"
    repository_dispatch:
      types: [upstream-moved]
  propose:
    source: "{output_dir}/recompiled"
    destination: .
    branch: pipeline/upstream-bump
    title: "Update inherited pipelines"
    labels: pipeline,upstream
    reuse-branch: true
---

## Steps

Polling is the baseline because it needs no privileged credential anywhere: this repository asks its
upstreams whether they moved, rather than an upstream holding a token that can write here. The
dispatch trigger is the same work, sooner, for organizations that need same-hour propagation — and
the payload it carries is ignored entirely. Every commit is resolved from this repository's own
`inherits:`, against repositories it already trusts.

1. **Re-resolve every upstream** → script: scripts/repin.py
   - id: repin
   - emits: moved
   - uses-compiler: true

2. **Recompile at the new commits** → script: scripts/recompile.sh
   - id: recompile
   - uses-compiler: true
   - args: --output={output_dir}/recompiled
