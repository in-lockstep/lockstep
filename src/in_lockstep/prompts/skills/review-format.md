---
name: review-format
description: The shape a review is written in
---

Write one JSON object to your output path:

```json
{
  "title": "Security",
  "summary": "One or two sentences. What you looked for, and what you concluded.",
  "findings": [
    {"path": "src/files.py", "line": 84, "comment": "What is wrong and what an attacker or user does about it."}
  ]
}
```

`line` is the line **in the new file**, as the diff numbers it. A finding that names a `path` and a
`line` becomes an inline comment anchored there; one without a line still appears in the review body,
so omit it rather than guessing — a wrong anchor is worse than none.

`findings` is empty when there is nothing to report, and `summary` then says so in a sentence. A
review that manufactures a concern to look useful is the reason people mute review bots.

Do not include the review's heading or any marker. The pipeline adds those, and the marker is how
your next review revises this one instead of appearing beneath it.
