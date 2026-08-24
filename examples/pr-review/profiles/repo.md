---
name: repo
description: This repository
contexts: [repo]
github:
  environment: repo
  # Where the telemetry goes. Declared here rather than guessed: the compiler refuses a `${NAME}`
  # it has not been told is a secret or a var.
  secrets: [OTEL_AUTHORIZATION]
  vars: [OTEL_ENDPOINT]
  deploy:
    mode: external
---

review_event=COMMENT
