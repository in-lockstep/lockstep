---
name: my-app
description: Staging deployment of the target application
contexts: [my-app-patterns]
github:
  environment: my-app-staging
  secrets: [APP_PASSWORD, JIRA_API_TOKEN]
  vars: [APP_URL, API_URL, JIRA_BASE_URL]
  deploy:
    mode: external
---

url=${APP_URL}
api_url=${API_URL}
jira_base_url=${JIRA_BASE_URL}
password=${APP_PASSWORD}
auth_method=jwt
