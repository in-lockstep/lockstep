---
name: my-app-patterns
description: Conventions specific to the target application
---

The application exposes a REST API under `/api/v1` and authenticates with a JWT bearer token.
List endpoints are paginated with `page` and `page_size` query parameters.
