"""The catalog: a static file in a git repository, and nothing else.

There is no service here, no accounts, and no ranking to defend. A catalog is an `index.toml`
listing packs and pointing at receipts committed beside it — the shape a Homebrew tap has, for the
same reason: the index answers "what exists and what does it hold", and every question that needs
to be trusted is answered by re-deriving locally rather than by believing the listing.

Three properties are the design.

**The catalog is an install-time artifact.** Nothing reads it during a run. A run of a repository
that installed a pack is identical to a run of one that vendored the same class by hand, which is
what keeps `--strategy <name>` from ever becoming a string a ticket could steer.

**The listing carries receipts, not claims.** An entry names a receipt file derived by
`pack describe`, and `add` compares it against what it derives here. A published receipt is
therefore falsifiable: it is not what the author said, it is what their code did, checkable by
anyone who installs the pack.

**Criteria are stated, and are not an endorsement.** A catalog may declare that it lists only packs
whose receipts pass the checks in `CRITERIA`. Meeting them says the pack keeps the framework's
guardrails and can be measured before it is trusted. It says nothing about whether the code is any
good, and a catalog that implied otherwise would be transferring a judgement nobody made.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

#: Where a repository records the catalogs it reads. Committed, because a source is somebody's
#: decision about where this repository will look for code, and that belongs in review — the same
#: argument the standards layer makes about a dependency being a reviewable diff.
SOURCES_FILE = "market.toml"

#: Everything an index entry may carry. Refused rather than ignored when it grows a key, for the
#: reason `pack.toml` gives: a key that is silently accepted arrives, gets documented, and becomes
#: load-bearing. An index describes; it does not configure.
ENTRY_KEYS = frozenset({"name", "distribution", "index", "kind", "summary", "source", "receipt"})

MAX_INDEX_BYTES = 1_000_000


class MarketError(RuntimeError):
    """A catalog cannot be read, or says something it may not say."""


@dataclass(frozen=True)
class Source:
    """A catalog this repository reads. `name` is how conflicts are reported, not a namespace."""

    name: str
    url: str


@dataclass(frozen=True)
class Entry:
    """One pack in a catalog. Every field is a pointer; none of it is trusted on its own."""

    name: str
    distribution: str
    kind: str = ""
    summary: str = ""
    index: str = ""
    source: str = ""
    receipt: str = ""


@dataclass(frozen=True)
class Catalog:
    """A parsed `index.toml`, and where it came from."""

    source: Source
    entries: tuple[Entry, ...] = ()
    #: Whether the catalog claims to apply `CRITERIA` to what it lists. A claim, and the reason
    #: `market lint` exists: a criterion nobody re-checks is a sentence in a README.
    criteria: bool = False


def parse_index(text: str, source: Source) -> Catalog:
    """Parse a catalog, refusing an entry that carries anything an index may not carry."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise MarketError(f"catalog {source.name!r} is not readable TOML: {e}") from e

    entries: list[Entry] = []
    for raw in data.get("pack") or ():
        if not isinstance(raw, dict):
            raise MarketError(f"catalog {source.name!r} has a [[pack]] that is not a table")
        unknown = sorted(set(raw) - ENTRY_KEYS)
        if unknown:
            raise MarketError(
                f"catalog {source.name!r} entry {raw.get('name', '(unnamed)')!r} carries "
                f"{', '.join(unknown)}, which an index may not: it describes a pack, and what runs "
                f"is decided by a line in lockstep.py."
            )
        if not raw.get("name") or not raw.get("distribution"):
            raise MarketError(
                f"catalog {source.name!r} has an entry without a name and a distribution, so "
                f"nothing could be installed from it"
            )
        entries.append(Entry(**{key: str(value) for key, value in raw.items()}))

    index = data.get("index") or {}
    return Catalog(
        source=source,
        entries=tuple(sorted(entries, key=lambda e: e.name)),
        criteria=bool(index.get("criteria", False)) if isinstance(index, dict) else False,
    )


# -- sources -------------------------------------------------------------------------


def sources(root: Path) -> list[Source]:
    """The catalogs this repository reads, in name order."""
    path = root / ".lockstep" / SOURCES_FILE
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise MarketError(f"{path} is not readable TOML: {e}") from e
    found = [
        Source(name=str(name), url=str(entry.get("url", "")))
        for name, entry in sorted((data.get("source") or {}).items())
        if isinstance(entry, dict)
    ]
    return [source for source in found if source.url]


def add_source(root: Path, name: str, url: str) -> Path:
    """Register a catalog. Appends; a name already present is replaced with its new URL."""
    check_url(url)
    path = root / ".lockstep" / SOURCES_FILE
    existing = {source.name: source.url for source in sources(root) if path.is_file()}
    existing[name] = url
    body = "".join(f'[source."{key}"]\nurl = "{value}"\n\n' for key, value in sorted(existing.items()))
    path.parent.mkdir(parents=True, exist_ok=True)

    from .privileged import sink

    sink.write_text_atomic(
        path,
        "# Catalogs this repository reads for extension packs.\n"
        "#\n"
        "# Registering one changes where this repository looks for code, so it is committed and\n"
        "# reviewed. Nothing here installs or binds anything: a catalog is read at `search` and\n"
        "# `add` time, and never during a run.\n\n" + body,
    )
    return path


def check_url(url: str) -> None:
    """https only, and a host. Not a policy setting — a floor.

    A catalog URL decides where a description of installable code comes from. Over plain http that
    description is whatever the network says it is, and the receipt comparison that makes a listing
    falsifiable would be comparing against an attacker's document. `file://` is refused for the
    same reason it would be convenient: a relative path in a repository is not a catalog somebody
    published.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise MarketError(
            f"{url!r} is not an https URL. A catalog says what code to install, so it is fetched "
            f"over a channel that cannot be rewritten in transit."
        )


def fetch(url: str, *, timeout: float = 10.0) -> str:
    """Read a catalog over the network. The one place this module dials out."""
    import urllib.request

    check_url(url)
    request = urllib.request.Request(url, headers={"Accept": "text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - checked above
            raw = response.read(MAX_INDEX_BYTES + 1)
    except Exception as e:  # noqa: BLE001 - every failure here is the same failure to the reader
        raise MarketError(f"could not read the catalog at {url}: {e}") from e
    if len(raw) > MAX_INDEX_BYTES:
        raise MarketError(f"the catalog at {url} is larger than {MAX_INDEX_BYTES} bytes")
    return str(raw.decode("utf-8", errors="replace"))


def read_catalog(source: Source, *, root: Path | None = None) -> Catalog:
    """A catalog by source. A path is read from disk; everything else is fetched.

    A path is how `market lint` reads the catalog somebody is about to publish, and how this
    repository's tests read the worked example. It is deliberately not something `market add`
    accepts: a file on this machine is not a catalog anyone published.
    """
    if root is not None and not source.url.startswith("http"):
        return parse_index((root / source.url).read_text(), source)
    return parse_index(fetch(source.url), source)


# -- criteria -------------------------------------------------------------------------

#: What the project's own catalog requires of a listing, each checked off the receipt rather than
#: read off a README. Entry criteria, never an endorsement — meeting them says the pack keeps the
#: framework's guardrails and can be measured before it is trusted, and says nothing at all about
#: whether the code is any good.
CRITERIA = (
    "a receipt, derived by `pack describe`",
    "prompt packs report imports: none",
    "a corpus, so it can be measured",
    "at least one cassette, so measuring costs nothing",
)


def criteria_failures(entry: Entry, receipt: dict[str, Any] | None) -> list[str]:
    """Which criteria this entry does not meet. Empty is a pass; `None` receipt fails the first."""
    if receipt is None:
        return [CRITERIA[0]]

    failures: list[str] = []
    declared = (receipt.get("declares") or {}).get("kind", "")
    if (entry.kind or declared) == "prompt" and receipt.get("imports") != "none":
        failures.append(CRITERIA[1])
    if not receipt.get("corpus"):
        failures.append(CRITERIA[2])
    if not receipt.get("cassettes"):
        failures.append(CRITERIA[3])
    return failures


def receipt_at(root: Path, relative: str) -> dict[str, Any] | None:
    """Read a receipt an entry points at, refusing a path that leaves the repository.

    An entry's `receipt` field arrives from a catalog somebody else wrote, so it is untrusted
    input that names a file this process will open. `../../` is the obvious shape of that, and
    containment is cheaper to enforce than to reason about — the same instinct `ChangeGuard`
    applies to a path a model proposes.
    """
    import json

    if not relative or relative.startswith(("http://", "https://")):
        return None
    base = root.resolve()
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base):
        raise MarketError(
            f"a catalog entry points at {relative!r}, which leaves this repository. A receipt is "
            f"committed beside the index that names it, not somewhere else on this machine."
        )
    if not candidate.is_file():
        return None
    try:
        loaded = json.loads(candidate.read_text())
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None
