---
name: review-format
description: The shape a review is written in
---

Reply with one JSON object and nothing else — no prose around it, no code fence:

```json
{
  "statement": "Two to four sentences. What this change is, and what you make of it through your lens.",
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

`statement` is what you made of the change **as a whole**, from your angle. It is not a summary
of the findings below it — it is the thing the table cannot say: the judgement those findings
were drawn from, and the reason an empty table is meaningful. Say what the change does, what you
weighed, and where you landed. Name what you checked and found sound, not only what you did not
check.

`findings` is empty when there is nothing to report, and the statement is then the whole of your
review — which is exactly when it matters most. "No findings" from a reviewer that read the diff
carefully and one that skimmed it are the same two words; your statement is what tells them
apart. A review that manufactures a concern to look useful is the reason people mute review bots,
and a review that says nothing when it found nothing is the reason they stop reading them.

Do not include the review's heading or any marker. The pipeline adds those, and the marker is how
your next review revises this one instead of appearing beneath it.
