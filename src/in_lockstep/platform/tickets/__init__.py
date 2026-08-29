"""Work items, from whichever tracker.

One normalized shape from several trackers, so a workflow reading acceptance criteria does not
need to know which system delivered them. The compiler-era runtime already produced that shape;
what it lacked was a protocol — the choice was a CLI flag and an if/else, which meant a third
tracker was a third branch rather than a third implementation.

Ticket bodies and comments are `UNTRUSTED_EXTERNAL`. Anyone who can file a ticket can write into
a prompt.
"""

from .base import Ticket, TicketDraft, TicketRef, TicketSource, TicketState, TicketType, criteria_from
from .github import GitHubIssues
from .gitlab import GitLabIssues
from .jira import JiraSource, JiraTypeRef

__all__ = [
    "GitHubIssues",
    "GitLabIssues",
    "JiraSource",
    "JiraTypeRef",
    "Ticket",
    "TicketDraft",
    "TicketRef",
    "TicketSource",
    "TicketState",
    "TicketType",
    "criteria_from",
]
