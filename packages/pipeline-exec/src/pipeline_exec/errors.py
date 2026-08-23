"""Failure modes that should stop a workflow rather than degrade quietly."""

from __future__ import annotations


class ExecError(Exception):
    """A condition the caller must see. Rendered to stderr; exits non-zero."""


class TooManyItems(ExecError):
    pass


class CoverageShortfall(ExecError):
    pass
