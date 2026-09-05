"""The processes the framework ships, for an adopter to register rather than copy.

    from in_lockstep.workflows import fix, implement

    implement.register()
    fix.register()

`init --implement` writes those lines. `init --eject` writes the source instead, for a repository
that wants to own and edit its own process — which is a real position rather than a fallback, and
the one ADR 0001 deleted a compiler to protect.
"""

from . import fix, implement

__all__ = ["fix", "implement"]
