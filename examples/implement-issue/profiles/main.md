---
name: main
description: This repository and its issue tracker
contexts: [repository]
github:
  environment: main
  secrets: [JIRA_API_TOKEN]
  vars: [JIRA_BASE_URL]
  deploy:
    mode: external
  reports:
    branch: reports
    path: runs
    retain: 60
---

jira_base_url=${JIRA_BASE_URL}
jira_api_token=${JIRA_API_TOKEN}
