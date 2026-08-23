---
name: httpbin
description: What the target application is and how it behaves
---

The target is [httpbin](https://httpbin.org), a public HTTP request-and-response service. It is
stateless: nothing an endpoint does persists, so tests need no teardown beyond what they create
in-flight.

Notable behaviour:

- `/status/{code}` returns exactly the status code asked for.
- `/json`, `/uuid` and `/headers` return JSON whose shape is stable but whose values are not.
- `/delay/{n}` waits n seconds before responding; keep n small.
- Responses echo the request back, so `url`, `origin` and `headers` vary per run and must not be
  asserted on.
