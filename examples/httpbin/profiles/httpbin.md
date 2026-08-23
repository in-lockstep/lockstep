---
name: httpbin
description: The public httpbin service
contexts: [httpbin]
github:
  environment: httpbin
  secrets: []
  vars: [HTTPBIN_URL]
  deploy:
    mode: external
  reports:
    branch: reports
    path: runs
    retain: 60
---

api_url=${HTTPBIN_URL}
auth_method=none
