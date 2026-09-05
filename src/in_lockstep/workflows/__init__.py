"""The processes the framework ships, for an adopter to register rather than copy.

    from in_lockstep.workflows import fix, implement

    implement.register()
    fix.register()

`init --implement` writes those lines. Reading the source is `in-lockstep show-workflow
implement`, which prints it from the module that is actually imported — so what you read is what
runs. There is deliberately no flag that writes a copy into an adopter's module: a copy is a fork,
and every fix to it then lands twice and reaches nobody who scaffolded before it.
"""

from . import fix, implement

__all__ = ["fix", "implement"]
