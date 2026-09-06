"""Who ran a local run, when the repository chose to say.

A run carries an identity under CI (`ci_actor`, what the host said) or when a grant was needed
(`approval.by`, what a person claimed). A local run carried neither, so forty-five of this
repository's fifty-six records sat under `—` in every report and in no spread — the consistency
question could not be asked of the runs a team mostly makes (`GATE-TEAM-2`, #289).

Opt-in, and never detected. #164 named the remedy and refused to make it a default: a report that
named people who never chose to be named is the leaderboard the pseudonyms exist to refuse. So a
repository writes one line, `lockstep.identity = GitAuthor()`, and the framework records what that
line says. Nothing here runs unless the line is there.

A third source with a third name. What the line claims is written as `identity`, not as
`ci_actor` (the host said it) and not as `approval.by` (a grant was given), so `history --explain`
and `metrics.actor_of` can say it was the repository's own configuration that named the person.
The git author is a claim a person made about themselves, and `_provenance` keeps claims apart
from facts the host computed. `Identity` is a protocol, so a team that names people another way
binds its own line without forking; `GitAuthor` is the one that ships.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


class Identity(Protocol):
    """What `lockstep.identity` must be able to do: say who is running, or say nothing.

    Empty is absent, and absent is written nowhere. A record without the field is a run the
    repository did not opt in for, or one whose claim could not be made — and a reader cannot
    tell those apart by design, because either way nobody said who ran it.
    """

    def claim(self, root: str) -> str:
        """The identity to record for a run rooted at `root`, or "" when it cannot be said."""
        ...


@dataclass(frozen=True)
class GitAuthor:
    """The configured git author, as git would write it: `Name <email>`.

    Configured, not detected. Git will invent an author from the login and the hostname when
    nothing is set, and an invented `tpouyer@Tims-MacBook.local` is a fact about a machine, not a
    claim a person made — so both `user.name` and `user.email` must be set, the same two git
    itself requires before it will author a commit, and anything less records nothing.

    The whole ident rather than the email alone, because it is what git puts on the commits the
    same person makes here, so the ledger and the log name the person the same way. Two spellings
    are two askers in every report, deliberately (`actor_of` says why), and this one is chosen to
    match the spelling that already exists in the repository's own history.
    """

    def claim(self, root: str) -> str:
        name = _config(root, "user.name")
        email = _config(root, "user.email")
        if not name or not email:
            return ""
        return f"{name} <{email}>"


def _config(root: str, key: str) -> str:
    """One `git config --get`, or "" — an unset key, a missing git, or a root that is not a
    directory all mean the same thing here: nothing to claim."""
    try:
        done = subprocess.run(
            ["git", "config", "--get", key], cwd=root, capture_output=True, text=True, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""
