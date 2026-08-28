"""Controls that run outside the middleware chain.

`in-lockstep run --no-middleware` exists for bisecting behaviour, and ledger records are committed
to git. A debugging flag that can switch off the thing keeping credentials out of a permanent,
often public record is not a debugging flag — so redaction, egress policy and residency are not
middleware. They sit here, alongside the kill switch, and nothing in the chain can reach past them.
"""

from .redact import Redact, SecretRegistry, redact_registry

__all__ = ["Redact", "SecretRegistry", "redact_registry"]
