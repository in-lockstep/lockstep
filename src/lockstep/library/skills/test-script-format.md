---
name: test-script-format
description: The JSON test-script format the run-tests builtin executes
---

A test script is one JSON object. This is the format `pipeline-exec test-runner` parses, so a script
that does not match it is skipped with a warning rather than run.

```json
{
  "storyId": "get-status-200",
  "summary": "GET /status/200 returns 200",
  "testType": "api",
  "tags": ["contract"],
  "executionTier": 1,
  "setupSteps": [],
  "testSteps": [
    {
      "step": 1,
      "tool": "api",
      "action": "http_request",
      "params": { "method": "GET", "url": "/status/200" },
      "expected": "200 OK"
    }
  ],
  "teardownSteps": []
}
```

- `storyId` names the script and its report. It must be unique across the suite.
- `testType` is `api`, `ui` or `cli`, and chooses which executor runs the steps.
- `tags` are matched against the tag toggles in your tags file, which is how a script gets skipped
  in an environment that cannot run it.
- `executionTier` orders execution: tiers run in sequence, and scripts within a tier run in
  parallel. Use it when one script's output is another's input.
- `url` may be a path; the runner resolves it against the profile's `api_url`.
- `params` may carry `body`, `headers`, and `no_auth` for deliberately unauthenticated requests.
- `expected` is prose the runner records in the report and checks the response against.
- Anything a script creates in `setupSteps` it must remove in `teardownSteps`.
