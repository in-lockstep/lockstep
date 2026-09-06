---
name: review-format
description: The shape a review is written in
---

Reply with one JSON object and nothing else — no prose around it, no code fence:

```json
{
  "verdict": "One or two sentences. What you looked for, and what you concluded.",
  "findings": [
    {
      "path": "src/files.py",
      "line": 84,
      "summary": "What is wrong, in one line.",
      "detail": "What an attacker or user does about it, and why it follows from this change.",
      "severity": "warning"
    }
  ]
}
```

`summary` is the line a reader sees in the table; `detail` is the paragraph behind it. `severity`
is one of `error`, `warning` or `note`.

`line` is the line **in the new file**, as the diff numbers it. A finding that names a `path` and a
`line` becomes an inline comment anchored there; one without a line still appears in the review body,
so omit it rather than guessing — a wrong anchor is worse than none.

`findings` is empty when there is nothing to report, and `verdict` then says so in a sentence. A
review that manufactures a concern to look useful is the reason people mute review bots.

Do not include the review's heading or any marker. The pipeline adds those, and the marker is how
your next review revises this one instead of appearing beneath it.
