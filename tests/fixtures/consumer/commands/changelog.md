---
name: changelog
description: Write a release note for a merged pull request
github:
  triggers:
    workflow_dispatch: true
---

## Steps

1. **Write the release note** → agent: changelog-writer
   - output: {output_dir}/note.json
