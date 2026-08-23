"""Error types. Every user-facing failure carries a stable code and, where possible, a hint."""

from __future__ import annotations


class LockstepError(Exception):
    """Base class for all compiler errors. Codes are stable and documented."""

    code = "LS000"

    def __init__(self, message: str, *, hint: str | None = None, location: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.location = location

    def render(self) -> str:
        head = f"{self.code}: {self.message}"
        if self.location:
            head = f"{self.code}: {self.location} — {self.message}"
        if self.hint:
            head += f"\n      hint: {self.hint}"
        return head


class SpecError(LockstepError):
    """The authored spec is malformed or internally inconsistent."""

    code = "LS100"


class MissingDefinition(SpecError):
    code = "LS101"


class BadStepSyntax(SpecError):
    code = "LS102"


class EmitError(LockstepError):
    """The spec is valid but cannot be lowered onto this target."""

    code = "LS200"


class UnmappedProvider(EmitError):
    code = "LS201"


class MatrixTooLarge(EmitError):
    code = "LS202"


class OverlayError(LockstepError):
    code = "OVL400"


class OverlayAnchorNotFound(OverlayError):
    """A patch anchor matched nothing. Loud at compile time, never a silent no-op."""

    code = "OVL404"


class DriftError(LockstepError):
    """Committed output does not match a fresh compile of spec + overlays + pins."""

    code = "LS900"
