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
