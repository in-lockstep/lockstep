"""The writers that leave the process, and the only place redaction has to be remembered.

`Redact` knows *what* to mask. This module decides *where*, and the two are separate for a reason:
the first draft of this design listed the sinks, and the list missed five of them — stdout and
stderr, OTel span attributes, checkpoint files, the artifact handed between trampoline jobs, and
notification bodies. A list of sinks is written once and the code grows around it.

So the rule is inverted. Instead of asking each writer to remember redaction, the writers live
here and redact by construction, and `tests/in_lockstep/test_sinks.py` walks the AST of every
module looking for a raw write that went around them. Adding an unwrapped writer fails the build;
adding one deliberately means naming it in that test's allowlist with a reason, which is a diff a
reviewer sees rather than a habit that spreads.

Standard output is handled differently from the rest, and the difference is the point. There are
sixty-odd `click.echo` calls in the CLI, and wrapping each would be a rule that holds until
somebody writes the sixty-first. Wrapping the *stream* holds for calls nobody has written yet.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from .redact import Redact


class RedactingStream:
    """A text stream that masks secrets on the way out.

    Wrapping the stream rather than the call sites is what makes this default-deny: a `print` or
    `click.echo` added next year is covered without anyone remembering it was supposed to be.

    Delegation rather than `io.TextIOBase` subclassing, and not for style. `TextIOBase` declares
    `encoding` writeable in its stubs and refuses the assignment at runtime, and the set of
    attributes a consumer reaches for is open — Click checks `encoding` and `isatty`, pytest's
    capture machinery reaches for others. `__getattr__` forwarding is correct for all of them,
    present and future, while an override list is correct only for the ones already written.
    """

    def __init__(self, wrapped: TextIO, redact: Redact | None = None) -> None:
        self._wrapped = wrapped
        self._redact = redact or Redact()

    def write(self, text: str) -> int:
        self._wrapped.write(self._redact.text(text))
        # The caller's accounting is of what it asked to write, not of what came out: returning
        # the masked length would make a successful write look like a partial one.
        return len(text)

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    @property
    def wrapped(self) -> TextIO:
        return self._wrapped


def install_streams(redact: Redact | None = None) -> None:
    """Route stdout and stderr through redaction for the rest of the process.

    Called once, at CLI entry. CI logs are frequently public and are the sink most likely to be
    forgotten, precisely because printing does not feel like writing somewhere.
    """
    redact = redact or Redact()
    if not isinstance(sys.stdout, RedactingStream):
        sys.stdout = RedactingStream(sys.stdout, redact)
    if not isinstance(sys.stderr, RedactingStream):
        sys.stderr = RedactingStream(sys.stderr, redact)


def uninstall_streams() -> None:
    """Restore the original streams. For tests; a process has no reason to undo this."""
    if isinstance(sys.stdout, RedactingStream):
        sys.stdout = sys.stdout.wrapped
    if isinstance(sys.stderr, RedactingStream):
        sys.stderr = sys.stderr.wrapped


@contextmanager
def redacted_streams(redact: Redact | None = None) -> Iterator[None]:
    install_streams(redact)
    try:
        yield
    finally:
        uninstall_streams()


def write_text(path: Path, text: str, *, redact: Redact | None = None) -> None:
    """Write a file, masked. Every on-disk sink in the framework goes through here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((redact or Redact()).text(text))


def write_json(path: Path, payload: Any, *, redact: Redact | None = None, **dumps: Any) -> None:
    """Serialize, then mask.

    In that order deliberately. Masking the structure first would have to guess which values end
    up in the file; masking the serialized form sees exactly the bytes that land on disk —
    including a secret that arrived inside a nested exception repr.
    """
    dumps.setdefault("indent", 2)
    dumps.setdefault("sort_keys", True)
    dumps.setdefault("default", repr)
    write_text(path, json.dumps(payload, **dumps) + "\n", redact=redact)


def append_text(path: Path, text: str, *, redact: Redact | None = None) -> None:
    """Append to a file, masked. For the sinks that accumulate — a transcript grows one
    invocation at a time, and rewriting the whole file to add a line would race a concurrent
    strategy phase writing its own."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write((redact or Redact()).text(text))


def write_text_atomic(path: Path, text: str, *, redact: Redact | None = None) -> None:
    """Same guarantee, for a writer that must not leave a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), suffix=".partial")
    try:
        with os.fdopen(handle, "w") as raw:
            raw.write((redact or Redact()).text(text))
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def attributes(values: Mapping[str, Any], *, redact: Redact | None = None) -> dict[str, Any]:
    """Telemetry attributes, masked.

    A span attribute is a sink even though it never touches a file: it leaves for a collector, and
    a provider error passed verbatim to `record_exception` is a credential in someone's tracing UI.
    """
    masked = (redact or Redact()).value(dict(values))
    assert isinstance(masked, dict)
    return masked


__all__ = [
    "RedactingStream",
    "append_text",
    "attributes",
    "install_streams",
    "redacted_streams",
    "uninstall_streams",
    "write_json",
    "write_text",
    "write_text_atomic",
]
