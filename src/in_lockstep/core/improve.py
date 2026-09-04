"""What a repository declares about the prompt text a trend could be about.

The ledger says which findings keep coming back. It does not say which prompt fragment is
responsible for them, and nothing in a finding id can be made to say it: `review.security` names a
lens, not a file, and the file that composes that lens is a decision somebody made in Python. So
the join is declared here rather than inferred, and a trend nobody declared a body for is
attributed to a dash.

That is the whole reason this type exists. The tempting alternative — guess the body from the
finding id's prefix, or from a fragment whose name looks similar — produces an attribution that is
wrong silently, and a wrong attribution is worse than none: it points the next prompt change at
text that had nothing to do with the evidence, and the measurement afterwards would be real
arithmetic over the wrong subject.

Vocabulary only. `core` may import nothing of ours but `core`, and this file imports nothing at
all, so the layer that reads the ledger and the layer that composes prompts can both name an
`Improvable` without either one reaching the other.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Improvable:
    """A prompt body a trend may be attributed to, and the findings it claims to answer.

    `verb` is a plain string rather than `core.verbs.Verb`. A `Verb` is interned in a registry that
    `Verb.forget_custom()` clears, and membership in `SHIPPED_VERBS` changes what `ls` and the
    repository receipt say about the framework's own surface. None of that should follow from a
    repository naming which of its prompts it considers improvable, so this carries the name and
    leaves the registry alone.
    """

    #: Repository-relative path to the `.md` body. Never `.lockstep/lockstep.py`, and nothing here
    #: enforces that — the guard does, and the caller prints its verdict beside this path, because
    #: a type that quietly excluded a path would hide the one fact a reader needs.
    body: str
    #: The verb whose prompt composes this body: "review", "implement". A label for the reader.
    verb: str
    #: The composition label, e.g. "review/security". What the run's own output calls this
    #: fragment, so a person can line the two up.
    label: str
    #: Finding ids this body claims to answer, matched exactly.
    answers: tuple[str, ...] = ()

    def answers_for(self, finding: str) -> bool:
        """Whether this body claims the named finding.

        Exact membership, deliberately. A prefix test would let `review.` claim every review
        finding there will ever be, including ones added after this declaration was written, and
        the declaration would then be making a promise about evidence nobody had seen.
        """
        return finding in self.answers
