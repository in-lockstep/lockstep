---
name: tracker
description: The issue tracker and the site the report is published to
contexts: [tracker]
github:
  environment: tracker
  secrets: [JIRA_API_TOKEN]
  vars: [JIRA_BASE_URL]
  deploy:
    mode: external
---

jira_base_url=${JIRA_BASE_URL}
jira_api_token=${JIRA_API_TOKEN}
