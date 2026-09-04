"""Reading what a chat-ops comment asked for.

A comment is a command selector, never a command: `implement.yml` fires on
`startsWith(github.event.comment.body, '/implement')` and passes the body through, and what the
words in it mean is decided HERE.

That split is not stylistic. `test_workflow_triggers.py` holds a trigger to an allowlist of shell
statements precisely so lifecycle logic cannot accumulate in YAML, and "which words mean resume" is
lifecycle logic — the same rule that put ticket resolution in `ticket_for` rather than in a GitHub
expression. A workflow that parsed this would be a workflow nobody can test on a laptop.

The body is untrusted. That is fine and unchanged: it decides which of two shapes a run takes, not
what the run may do. `gate` already decides who may spend money, and resuming spends no more than
starting fresh.
"""

from __future__ import annotations

import re
from collections.abc import Collection

#: How many earlier attempts `--resume` reuses when a depth is not given.
DEFAULT_RESUME_DEPTH = 1

# `--resume` optionally followed by a count. Anchored on the flag rather than searched for loosely,
# so a comment that merely discusses resuming — "should we --resume this?" — is read the same way a
# prefix trigger reads a comment explaining why not to run: as the thing it says.
_RESUME = re.compile(r"(?:^|\s)--resume(?:[=\s]+(\d+))?(?=\s|$)")


class ResumeDepthRefused(Exception):
    """A depth that cannot mean anything. Refused rather than rounded, because a person who typed
    `--resume 0` meant something, and guessing which of "don't resume" or "resume one" they meant is
    how a tool teaches people not to trust it."""


def resume_depth(body: str) -> int:
    """How many earlier attempts this comment asked to reuse. `0` means it asked for none.

    A DEPTH rather than a boolean, because `/implement --resume 2` is a different request from
    `/implement --resume` and collapsing them would make the second unreachable from chat-ops.

    Absent is `0` and that is the whole default: a model handed its own wrong diff will defend it,
    and sometimes the right answer is a clean start. Nobody gets resumed by accident.
    """
    match = _RESUME.search(body or "")
    if match is None:
        return 0
    if match.group(1) is None:
        return DEFAULT_RESUME_DEPTH
    depth = int(match.group(1))
    if depth < 1:
        raise ResumeDepthRefused(
            f"--resume {depth} asks for {depth} earlier attempts, which is not a number of "
            f"attempts. Omit --resume to start clean, or give 1 or more."
        )
    return depth


#: `/review` optionally followed by the lens asked for. Anchored at the start, because the
#: trampoline already matched the prefix and anything else in the body is prose written to a person.
_REVIEW = re.compile(r"^\s*/review(?:\s+(\S+))?")


class AspectRefused(Exception):
    """A `/review` that named no lens, or named one nothing declared.

    Refused rather than defaulted, in both directions. A bare `/review` could mean "every lens",
    which is four paid calls nobody asked for by name, or "the usual one", which is a default this
    framework does not get to choose on somebody's behalf. And an unrecognised name is a typo or a
    probe, and the two want the same answer.
    """


def aspect_from(body: str, *, known: Collection[str]) -> str:
    """Which review lens this comment asked for, resolved against the lenses that exist.

    `known` is the BOUND adapter's lens map, not the shipped one. A repository that replaced its
    lenses gets exactly its own set, and one that added a lens can name it — which is what makes
    `AiReview(lenses=...)` reach chat-ops rather than stopping at the CLI.

    Unlike `resume_depth` above, this decides which prompt composes, so the closed set is doing real
    work rather than being tidy. `design/strategy-selection.md` draws the line where it belongs: it
    is not selection that is unsafe, it is selection ACROSS A CAPABILITY LINE, and every review lens
    shares one posture — a single turn, the diff in the prompt, no tools at all. A comment picks
    among options the repository already declared and cannot introduce one.

    Resolved BEFORE anything spends or records, which is the other half. An aspect that reaches a
    run id earns a ledger record even when the adapter then refuses it, and `blocked` sits inside
    `failure_rate`'s denominator — so a stream of typos from anyone who can comment would deflate
    the repository's own failure rate (#203). It also never reaches `report.marker`, which builds an
    HTML comment by interpolation.
    """
    match = _REVIEW.match(body or "")
    if match is None:
        raise AspectRefused(
            "this comment does not begin with `/review`, so nothing here names a lens. "
            "Guessing one out of prose is how a tool spends money on a comment nobody meant."
        )

    options = ", ".join(sorted(known)) or "none — this repository binds a Review adapter with no lenses"
    asked = (match.group(1) or "").strip()
    if not asked:
        raise AspectRefused(f"`/review` needs a lens. This repository has: {options}.")

    # Case-folded, which is free: the set is closed, so folding cannot admit a name that was not
    # already in it. Matched against the declared spelling so the answer is the repository's own.
    folded = {name.casefold(): name for name in known}
    resolved = folded.get(asked.casefold())
    if resolved is None:
        raise AspectRefused(f"no lens named {asked!r}. This repository has: {options}.")
    return resolved
