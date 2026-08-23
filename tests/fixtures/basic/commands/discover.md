---
name: discover
description: Discover the application's API and UI structure
parameters:
  - name: profile
    description: App profile to target
state: true
guardrails: [common]
github:
  triggers:
    workflow_dispatch: true
---

## Steps

1. **Discover API surface** → script: scripts/discover-api.py
   - args: --output={output_dir}/api-endpoints.json --api-url={api_url}
   - fingerprint: curl -sf {api_url}/openapi.json | sha256sum | cut -d' ' -f1

2. **Discover UI structure** → script: scripts/discover-ui.py
   - args: --output={output_dir}/ui-structure.json --state={state_db}
   - post: echo "discovery complete"
