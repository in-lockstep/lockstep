---
name: api-tests
description: Constraints specific to generated contract tests
---

You MUST NOT write a test that depends on data another test creates. Every test sets up whatever it
needs and cleans up after itself, because tests run concurrently and in any order.

You MUST NOT assert on values the server is free to change — timestamps, request ids, the origin IP.
A test that fails when nothing broke is worse than no test at all.
