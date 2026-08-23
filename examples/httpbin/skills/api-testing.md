---
name: api-testing
description: The JSON test-script format the runner executes
---

A test script is one JSON object:

```json
{
  "storyId": "get-status-200",
  "summary": "GET /status/200 returns 200",
  "testType": "api",
  "tags": ["contract"],
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

- `url` may be a path; the runner resolves it against the profile's `api_url`.
- `params` may carry `body`, `headers`, and `no_auth` for deliberately unauthenticated requests.
- `expected` is prose the runner records in the report and checks the response against.
- Anything a test creates in `setupSteps` it must remove in `teardownSteps`.
