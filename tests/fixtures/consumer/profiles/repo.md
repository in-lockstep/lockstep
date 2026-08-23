---
name: repo
description: This repository
contexts: [repo]
github:
  environment: repo
  secrets: []
  vars: [REPO_URL]
---

url=${REPO_URL}
