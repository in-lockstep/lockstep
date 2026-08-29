"""Who is allowed to ask for a run.

A chat-ops trigger is an unauthenticated entry point wearing a familiar interface: anyone who can
see a repository can type a comment, so the comment cannot be the authorization. Something has to
decide, and the decision is security-critical enough that it should not live as `grep` inside a
YAML `if:`.

So it lives here, where it can be tested against the shapes GitHub actually sends.

Two sources, and they answer different questions. `author_association` is computed by the host and
says how the commenter relates to the repository — `MEMBER` means a member of the owning
organisation, `OWNER` the owner. CODEOWNERS says who is accountable for the code, which is a
different set: an outside collaborator can own a directory without being in the org at all.
Either is sufficient; neither is checkable from the other.

What this deliberately does NOT do is trust anything in the comment body. The body selects a
command; it never selects who may run one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Associations GitHub computes that mean "inside the organisation". `COLLABORATOR` is not here:
#: it means push access without org membership, which is a weaker and broader statement than the
#: request "an org member or a code owner" — and a collaborator who should qualify is exactly the
#: person CODEOWNERS names.
ORG_ASSOCIATIONS: frozenset[str] = frozenset({"OWNER", "MEMBER"})

#: A line in CODEOWNERS is a path followed by owners. Teams appear as `@org/team`, individuals as
#: `@handle`, and an email address is also legal.
_OWNER = re.compile(r"@([A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9._-]+)?)")


@dataclass(frozen=True)
class Owners:
    """What CODEOWNERS names, split by what can be checked here."""

    handles: frozenset[str] = frozenset()
    #: `@org/team` entries. Resolving one needs an API call and a token that can read team
    #: membership, which a workflow's default token cannot do — so they are carried separately
    #: and reported rather than silently treated as "not an owner".
    teams: frozenset[str] = frozenset()


def parse_codeowners(text: str) -> Owners:
    """Every owner named anywhere in the file, ignoring which paths they own.

    Path-scoped on purpose it is not: the question here is "is this person accountable for this
    repository", not "for this file". A code owner of one directory being able to ask for an
    implementation of an issue is the intended reading, and a path-scoped check would need to know
    which files the run will touch — which is not known until after it has run.
    """
    handles: set[str] = set()
    teams: set[str] = set()
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        # Skip the pattern; owners are the @-prefixed tokens after it.
        for match in _OWNER.finditer(stripped):
            name = match.group(1)
            (teams if "/" in name else handles).add(name.lower())
    return Owners(handles=frozenset(handles), teams=frozenset(teams))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    #: Everything considered, so a refusal can be read without re-running it.
    considered: tuple[str, ...] = field(default_factory=tuple)


def authorize(
    *,
    actor: str,
    association: str,
    codeowners: str = "",
    allowed_associations: frozenset[str] = ORG_ASSOCIATIONS,
) -> Decision:
    """Whether this person may ask for a run, and why.

    Returns a decision rather than raising, because the caller is a CLI that has to print it: a
    refusal that says which of the two routes was checked is the difference between "add them to
    CODEOWNERS" and "invite them to the org".
    """
    login = actor.strip().lstrip("@").lower()
    if not login:
        return Decision(False, "no actor was supplied, so nothing could be checked")

    # Bots first, and unconditionally. A bot that can comment can comment on its own output, and
    # a trigger that answers its own comments is a loop that spends money. There is no association
    # or CODEOWNERS entry that should lift this.
    if login.endswith("[bot]") or login.endswith("-bot"):
        return Decision(
            False,
            f"{actor!r} is a bot. A trigger a bot can fire is a loop, and this one spends money.",
        )

    normalized = association.strip().upper()
    owners = parse_codeowners(codeowners)
    considered = (
        f"association={normalized or '(none)'}",
        f"codeowner={'yes' if login in owners.handles else 'no'}",
        f"teams-not-checked={len(owners.teams)}",
    )

    if normalized in allowed_associations:
        return Decision(True, f"{actor} is {normalized} of this repository", considered)
    if login in owners.handles:
        return Decision(True, f"{actor} is named in CODEOWNERS", considered)

    detail = ""
    if owners.teams:
        # Said out loud rather than left as a silent "no". Someone refused while sitting in a team
        # that owns the code will otherwise conclude the gate is broken, and they are half right.
        detail = (
            f" CODEOWNERS also names {len(owners.teams)} team(s) "
            f"({', '.join('@' + t for t in sorted(owners.teams))}), and team membership cannot be "
            f"resolved here — it needs a token that can read the organisation. Name the person "
            f"directly, or add them to the organisation."
        )
    return Decision(
        False,
        f"{actor} is {normalized or 'not associated with this repository'} and is not named in "
        f"CODEOWNERS.{detail}",
        considered,
    )
