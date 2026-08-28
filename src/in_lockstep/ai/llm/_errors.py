"""Error classification by type and HTTP status — never by substring.

The vendored providers all did `if "429" in msg or "rate" in msg.lower()`. That matches the word
"gene*rate*d", "accu*rate*", and "tempe*rate*ure", so ordinary content errors were classified as
rate limits and retried four times; and `if "401" in msg` matches any request id containing 401.
GATE-RETRY-3 pins the exact case.

Classification order is: explicit status code -> SDK exception type -> transport exception type.
An unrecognised error is re-raised unchanged rather than guessed at.
"""

from __future__ import annotations

from .interface import (
    AuthenticationError,
    ContextLengthError,
    LLMError,
    ModelNotFoundError,
    RateLimitError,
    TransientError,
)

# Anything the model could not have caused and a retry could plausibly fix.
_TRANSIENT_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _request_id_of(exc: BaseException) -> str:
    value = getattr(exc, "request_id", None)
    return value if isinstance(value, str) else ""


def _retry_after_of(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _is_context_length(exc: BaseException, status: int | None) -> bool:
    """Only a 400-class error may be a context-length error, and only on an explicit signal.

    Kept narrow deliberately: this is the one place a message is consulted, because no SDK exposes
    a distinct type for it, and misclassifying it as retryable would re-send a prompt guaranteed to
    fail again. A 400 that is not recognised stays a plain LLMError.
    """
    if status not in (400, 413, 422):
        return False
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("context length", "context_length", "too many tokens", "maximum context",
                       "prompt is too long", "exceeds the maximum")
    )


def classify(exc: BaseException, *, provider: str) -> LLMError | None:
    """Map a provider exception onto the typed hierarchy, or None to re-raise unchanged."""
    if isinstance(exc, LLMError):
        return exc

    status = _status_of(exc)
    request_id = _request_id_of(exc)
    common: dict[str, object] = {"status_code": status, "provider": provider,
                                 "request_id": request_id}
    message = str(exc)

    if status in (401, 403):
        return AuthenticationError(message, **common)  # type: ignore[arg-type]
    if status == 404:
        return ModelNotFoundError(message, **common)  # type: ignore[arg-type]
    if _is_context_length(exc, status):
        return ContextLengthError(message, **common)  # type: ignore[arg-type]
    if status == 429:
        return RateLimitError(message, retry_after=_retry_after_of(exc), **common)
    if status is not None and status in _TRANSIENT_STATUS:
        return TransientError(message, **common)  # type: ignore[arg-type]

    # Transport-level failures carry no status. httpx is the only client library the vendored
    # providers share, so it is the only one matched by type here.
    name = type(exc).__name__
    if name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
                "RemoteProtocolError", "ReadError", "TimeoutException"):
        return TransientError(message, **common)  # type: ignore[arg-type]
    if isinstance(exc, TimeoutError):
        return TransientError(message, **common)  # type: ignore[arg-type]

    if status is not None and 400 <= status < 500:
        return LLMError(message, **common)  # type: ignore[arg-type]
    return None
