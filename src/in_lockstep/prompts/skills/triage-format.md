---
name: triage-format
description: The shape a triage is written in
---

Write one JSON object to your output path:

```json
{
  "kind": "bug | feature | question | chore | unclear",
  "priority": "urgent | high | normal | low",
  "reason": "One sentence. Why this kind and this priority, in terms of the issue's own words.",
  "missing": ["What a person would have to add before this can be worked on"],
  "acceptance_criteria": ["Criteria the issue states, verbatim where it states them"],
  "labels": ["labels to apply"],
  "comment": "What to post on the issue. Markdown, a few sentences."
}
```

`kind` is `unclear` only when the issue does not say enough to place it — not when you are hesitant.

`missing` is empty when nothing is missing. It is not a place for improvements you would like to
see; it is the list of things without which the work cannot start.

`acceptance_criteria` carries what the issue **says**, not what you would add. Where the issue
supplies none, leave it empty and say so in `missing`. The input names its `criteria_source`: when
that is `description` or `guessed from …`, the criteria were inferred by a parser rather than
written in a field somebody filled in, so treat them as the reporter's prose rather than as a
contract, and say which in `reason`.

`comment` is addressed to the person who filed the issue. It says what you concluded and what you
need from them, and it does not restate the issue back at them.
