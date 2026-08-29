---
name: change-tools
description: How a change is handed back when the agent holds tools
---

You have tools. The change is what you stage with them, not what you describe in your answer.

`write_file` and `delete_file` **stage** a change; they do not touch the repository. Everything you
stage is collected into one change set, checked against the repository's protected-path rules, and
applied — or opened as a pull request — after this session ends. Two consequences worth knowing:

- Staging the same path twice keeps only the last version, so correcting yourself is free.
- A refusal is not a failure of the run. Some paths are not writable by an agent under any grant —
  CI configuration, the lifecycle definition, packaging, anything outside the repository root. The
  refusal names the rule. Do something else, or explain in `unfinished` why the ticket cannot be
  done without it. Do not look for another way in; there isn't one, and the same rule is checked
  again before anything is applied.

When you are finished, your final message must be JSON and nothing else — no prose around it, no
fenced block:

```
{
  "summary": "one or two sentences: what you changed and why",
  "notes": ["anything a reviewer should know about the approach"],
  "unfinished": ["what you could not do, and why"]
}
```

The files are already staged by then. The JSON is your report on them, not the delivery mechanism,
so do not paste file contents into it.
