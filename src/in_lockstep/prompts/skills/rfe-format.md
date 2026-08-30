---
name: rfe-format
description: The shape an enhancement draft is written in
---

Write one JSON object to your output path:

```json
{
  "title": "Imperative, under 80 characters, specific enough to tell apart from every other ticket",
  "problem": "What is hard, slow, missing or risky today. What happens now, not what should happen.",
  "proposal": "The change being requested, concrete. Where a mechanism is your reading rather than the request, say so inline.",
  "acceptance_criteria": ["Checkable statements — each something a reviewer could verify or fail"],
  "open_questions": ["Decisions that must be made before work starts and the idea does not make"],
  "labels": ["labels the idea plainly supports"]
}
```

`title`, `problem` and `proposal` are required. A title that names the mechanism ("add a
`--json` flag to report") beats one that names the wish ("better report output").

`acceptance_criteria` carries only what the idea supports. A criterion for scope the requester
never mentioned is gold-plating; if the decision matters, it is an open question instead.

`open_questions` empty means the draft is ready to pick up as it stands. Do not invent a
question to appear thorough, and do not omit one to appear finished.
