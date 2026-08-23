---
name: generate-tests
description: Extract user stories from Jira and generate deterministic test scripts
parameters:
  - name: jql
    default: ""
    description: Override JQL to target specific features
  - name: skip-discovery
    default: false
    description: Skip the discovery phase
guardrails: [common]
github:
  triggers:
    schedule: '0 2 * * 1-5'
---

## Steps

1. **Discover application structure** → command: discover
   - profile: my-app
   (if not --skip-discovery)

2. **Fetch issues from Jira** → script: scripts/fetch-issues.py
   - args: --output={output_dir}/jira-issues.json --jql="{jql}"
   - id: fetch-issues

3. **Extract stories from each issue** → agent: story-extractor
   - foreach: issue in {output_dir}/jira-issues.json
   - output: {output_dir}/extracted-stories
   - parallel: 3

4. **Build test manifest** → script: scripts/save-manifest.py
   - args: --input={output_dir}/extracted-stories --output={output_dir}/test-manifest.json
   - on-failure: echo "manifest build failed"

5. **Deploy the app locally** → script: scripts/deploy-local.sh
   - targets: [local]
