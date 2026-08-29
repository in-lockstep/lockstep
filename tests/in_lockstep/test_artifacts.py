"""The ChangeSet between two jobs.

This is the only place §4.2's "every inter-verb type serializes losslessly" is actually cashed,
and the two halves live in different JOBS — so a mismatch does not show up as a type error, it
shows up as a privileged job applying half a change or none of one.

It used to be two private functions in `cli.py` agreeing by proximity. Now one module owns the
format and both halves call it, which is what makes a round trip assertable at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from in_lockstep.core.types import ChangeAuthor, ChangeSet, FileChange, TestVerdict
from in_lockstep.platform.artifacts import (
    MalformedArtifact,
    payload_path,
    read_changeset,
    read_verdict,
    write_changeset,
)
from in_lockstep.privileged.redact import Redact, SecretRegistry


def _changeset() -> ChangeSet:
    return ChangeSet(
        changes=(
            FileChange(path="src/a.py", contents="x = 1\n", author=ChangeAuthor.AGENT),
            FileChange(path="src/gone.py", contents=None, author=ChangeAuthor.AGENT),
        ),
        summary="did the thing",
        ticket="#59",
    )


def test_a_changeset_survives_the_round_trip(tmp_path: Path) -> None:
    write_changeset(tmp_path / "out", _changeset())
    assert read_changeset(tmp_path / "out") == _changeset()


def test_a_directory_and_a_file_are_both_accepted(tmp_path: Path) -> None:
    """Both spellings are used in the wild, and a job passing the wrong one is a wasted run."""
    assert payload_path(tmp_path).name == "changeset.json"
    assert payload_path(tmp_path / "cs.json").name == "cs.json"


def test_metadata_is_masked_and_file_contents_are_not(tmp_path: Path) -> None:
    """The split this module exists to make.

    `summary` is model prose, which is where a credential quoted out of a tool result surfaces.
    `contents` is the change: masking a source file that happens to match a credential shape would
    corrupt the file the framework was asked to write — and would protect nothing, because `apply`
    writes those same bytes at the other end.
    """
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    registry = SecretRegistry()
    registry.add(secret)

    changeset = ChangeSet(
        changes=(FileChange(path="src/a.py", contents=f'TOKEN = "{secret}"\n'),),
        summary=f"the provider rejected {secret}",
        ticket="#1",
    )
    path = write_changeset(tmp_path / "out", changeset, redact=Redact(registry))
    text = path.read_text()

    assert secret not in text.split('"changes"')[0], "the summary carried a credential through"
    assert read_changeset(tmp_path / "out").changes[0].contents == f'TOKEN = "{secret}"\n'


def test_an_absent_artifact_says_so(tmp_path: Path) -> None:
    with pytest.raises(MalformedArtifact, match="no changeset"):
        read_changeset(tmp_path / "nothing")


def test_invalid_json_is_named_rather_than_raised_as_a_parse_error(tmp_path: Path) -> None:
    (tmp_path / "changeset.json").write_text("{not json")
    with pytest.raises(MalformedArtifact, match="not valid JSON"):
        read_changeset(tmp_path)


def test_an_entry_with_no_path_is_refused(tmp_path: Path) -> None:
    """Applying half a changeset is worse than applying none of one."""
    (tmp_path / "changeset.json").write_text('{"changes": [{"contents": "x"}]}')
    with pytest.raises(MalformedArtifact, match="no path"):
        read_changeset(tmp_path)


def test_an_omitted_author_defaults_to_agent(tmp_path: Path) -> None:
    """Fail-closed: FRAMEWORK entries skip the guard, so an artifact must not claim that by omission."""
    (tmp_path / "changeset.json").write_text('{"changes": [{"path": "a.py", "contents": "x"}]}')
    assert read_changeset(tmp_path).changes[0].author is ChangeAuthor.AGENT


def test_a_verdict_rides_the_artifact_and_reads_back(tmp_path: Path) -> None:
    """The verdict of testing the staged change crosses the job split alongside the ChangeSet."""
    verdict = TestVerdict(status="succeeded", decided=True, total=42, passed=42)
    write_changeset(tmp_path / "out", _changeset(), verdict=verdict)
    assert read_verdict(tmp_path / "out") == verdict
    # And the ChangeSet itself is unaffected by the extra key.
    assert read_changeset(tmp_path / "out") == _changeset()


def test_no_verdict_reads_back_as_not_tested(tmp_path: Path) -> None:
    """No Test bound means no verdict written, which reads back as None — never a phantom green."""
    write_changeset(tmp_path / "out", _changeset())
    assert read_verdict(tmp_path / "out") is None


def test_read_verdict_is_tolerant_of_a_missing_or_malformed_artifact(tmp_path: Path) -> None:
    assert read_verdict(tmp_path / "nothing") is None
    (tmp_path / "changeset.json").write_text('{"verdict": "not an object"}')
    assert read_verdict(tmp_path) is None


def test_read_verdict_treats_a_non_numeric_count_as_not_tested(tmp_path: Path) -> None:
    """A count that is not a number must not crash the propose job — it reads as 'not tested'."""
    (tmp_path / "changeset.json").write_text('{"changes": [], "verdict": {"total": "lots"}}')
    assert read_verdict(tmp_path) is None
