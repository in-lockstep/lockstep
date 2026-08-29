---
name: review-revision
description: Revising a review you already posted
---

When the input has `"revision": true`, you have reviewed this pull request before. Your earlier
review is in `previous_review`, and `new_commits` lists what changed since.

You are answering one question: **what do those commits change about what you said?**

- A finding that has been addressed is reported as addressed, in a sentence, and dropped from
  `findings`. Repeating it is how a reviewer who did the work gets told they did not.
- A finding that still stands is repeated, unchanged, so it does not look resolved.
- A finding the new commits introduced is new.

Do not re-review the parts of the change nobody touched. Your earlier conclusion about them still
holds, and re-deriving it wastes a turn and risks contradicting yourself for no reason.
