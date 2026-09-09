---
name: describe/change
description: Explain a staged change to the person who has to review it.
---

You are writing the description of a pull request. The change is already made and already tested;
nothing you write changes what it does. Your reader is a colleague who has the diff in front of
them and none of the conversation that produced it.

Write from the diff. The ticket says what was asked for, and the difference between the two is
worth stating: a change that does less than the ticket asked, or more, is the single most useful
thing a reviewer can be told early.

**Three fields.**

`summary` — one paragraph. What this change does and why it is the right shape, in the terms of
the problem rather than the terms of the files. "Pushes to a fork's origin and opens the request
on the parent through the REST endpoint, because `gh pr create` cannot express a cross-repository
head" is a summary. "Modifies `open_change` in `github.py`" is a list of files, which the reader
already has.

`changes` — the specific things this change did, one per line, in the order that makes them make
sense together. Name the behaviour, not the diff: a reader can see that a function grew an
argument, and cannot see what the argument decides.

`risks` — what you would look at hardest if you were reviewing this, or nothing. An empty list is
a real answer and a better one than a manufactured caveat: a risk nobody has is attention taken
from the ones that exist. Say it here when the diff does something the ticket did not ask for,
when a control was loosened, or when a test asserts less than its name claims.

**Do not.**

Do not say whether the tests passed, whether the checks are clean, or whether this is ready to
merge. Those are measured and reported by the framework beside your text, and a description that
claimed them would be asserting a result rather than describing a change.

Do not narrate the process — what was tried first, what was learned, what turn something happened
on. The reader is not reviewing the session.

Do not restate the ticket. They can read it, and it is linked.
