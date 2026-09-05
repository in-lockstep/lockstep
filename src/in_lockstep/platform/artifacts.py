"""The ChangeSet as it travels between two jobs.

§4.2 commits every inter-verb type to lossless serialization, and this is the one place that
promise is cashed: an unprivileged job produces a `ChangeSet`, a privileged job reads it back and
applies it. The two halves have to agree on the format, and until now they agreed by both being
private functions in `cli.py` — which is agreement by proximity rather than by contract, and it
put the writer out of reach of a `@workflow` that wanted to emit one.

The split in `write` is the part worth reading. The metadata is redacted and the file contents are
not. `summary` is model prose, which is where a credential quoted out of a tool result would
surface. `contents` is the change itself and must survive verbatim — masking a source file that
happens to match a credential shape would corrupt the file the framework was asked to write, and
would protect nothing, because `apply` writes those same bytes at the other end. `ChangeGuard` is
the control on that path, not `Redact`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.types import ChangeAuthor, ChangeSet, FileChange, TestVerdict
from ..privileged.redact import Redact

FILENAME = "changeset.json"

# The artifact DIRECTORY names. Scaffolded workflows declared these themselves and the trampoline
# YAML names them again in `${RUNNER_TEMP}/implement/changeset`, so the two agreed by coincidence
# and a rename in one place was a silent break in the other. Defined once, here, beside the reader
# and writer that use them.
CHANGESET = "changeset"
ATTEMPT = "attempt"
FIX_CHANGESET = "fix-changeset"

#: The verdict fields carried in the artifact. Counts and a status — no model prose, no file
#: contents — so unlike `summary` they need no redaction.
_VERDICT_FIELDS = ("status", "decided", "total", "passed", "failed", "skipped")


class MalformedArtifact(Exception):
    """The artifact is not a ChangeSet. Loud, because the alternative is applying half of one."""


def payload_path(artifact: str | Path) -> Path:
    """A directory holds `changeset.json`; a file is one. Both spellings are used in the wild."""
    path = Path(artifact)
    if path.is_dir() or not path.suffix:
        return path / FILENAME
    return path


def write_changeset(
    artifact: str | Path,
    changeset: ChangeSet,
    *,
    redact: Redact | None = None,
    verdict: TestVerdict | None = None,
) -> Path:
    """Serialize, metadata masked, contents verbatim. Returns where it landed.

    `verdict`, when the change was tested before it was staged, rides alongside so the propose job
    can report it. Omitted when no Test ran, which `read_verdict` reads back as "not tested".
    """
    mask = redact or Redact()
    path = payload_path(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "summary": mask.text(changeset.summary),
        "ticket": mask.text(changeset.ticket),
        "changes": [
            {
                "path": c.path,
                "contents": c.contents,
                "author": c.author.value,
                **({"symlink_target": c.symlink_target} if c.symlink_target else {}),
            }
            for c in changeset.changes
        ],
    }
    if verdict is not None:
        document["verdict"] = {field: getattr(verdict, field) for field in _VERDICT_FIELDS}
    path.write_text(json.dumps(document, indent=2) + "\n")
    return path


def read_changeset(artifact: str | Path) -> ChangeSet:
    """Read one back. Everything in it is untrusted: a previous job wrote it, which is not trust."""
    path = payload_path(artifact)
    if not path.exists():
        raise MalformedArtifact(f"no changeset at {path}")
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise MalformedArtifact(f"{path} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise MalformedArtifact(f"{path} is not a changeset object")

    changes = []
    for raw in data.get("changes", []) or []:
        if not isinstance(raw, dict) or "path" not in raw:
            raise MalformedArtifact(f"{path} contains an entry with no path")
        changes.append(
            FileChange(
                path=str(raw["path"]),
                contents=raw.get("contents"),
                # Defaulting to AGENT is the fail-closed choice: FRAMEWORK entries skip the guard,
                # so an artifact that omits the field must not be able to claim the exemption.
                author=ChangeAuthor(raw.get("author", "agent")),
                symlink_target=raw.get("symlink_target"),
            )
        )
    return ChangeSet(
        changes=tuple(changes),
        summary=str(data.get("summary", "")),
        ticket=str(data.get("ticket", "")),
    )


def read_verdict(artifact: str | Path) -> TestVerdict | None:
    """The test verdict written alongside the ChangeSet, or None if the change was not tested.

    A sibling of `read_changeset` rather than a change to its return type: `apply` and
    `apply-inline` read the same artifact and neither wants a verdict foisted on its signature.
    Tolerant like `read_changeset` — a missing or malformed verdict reads as "not tested" rather
    than raising, because the change still applies whether or not a suite ran over it.
    """
    path = payload_path(artifact)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return None
    raw = data.get("verdict") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        return TestVerdict(
            status=str(raw.get("status", "")),
            decided=bool(raw.get("decided", False)),
            total=int(raw.get("total", 0) or 0),
            passed=int(raw.get("passed", 0) or 0),
            failed=int(raw.get("failed", 0) or 0),
            skipped=int(raw.get("skipped", 0) or 0),
        )
    except (ValueError, TypeError):
        # A verdict whose counts are not numbers is not a verdict. Read it as "not tested" rather
        # than let a non-numeric field crash the propose job — the change still applies; the body
        # just cannot claim a result over it.
        return None
