---
name: production
description: The production application repository and its issue tracker
contexts: [target-app]
github:
  environment: production
  secrets: [JIRA_API_TOKEN, TARGET_REPO_TOKEN]
  vars: [JIRA_BASE_URL, TARGET_REPO]
  deploy:
    mode: external
  reports:
    branch: reports
    path: runs
    retain: 90
---

jira_base_url=${JIRA_BASE_URL}
jira_api_token=${JIRA_API_TOKEN}
target_repo=${TARGET_REPO}
target_repo_token=${TARGET_REPO_TOKEN}
