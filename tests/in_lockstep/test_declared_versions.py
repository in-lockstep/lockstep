"""A sandbox declares which version of each program its image carries, and bindings must agree.

`executables=("python", "python3")` answers *may I run this*. What broke was *which one am I
running*: this repository handed a model `python` 3.12 through `run_script` and `python` 3.11 or
3.13 through its suite -- which pair depending on where the run happened -- and nothing could see
it, because nothing had been asked (#419).

Two checks, and only one of them costs anything. `DOC184` compares what the module DECLARES, so it
is arithmetic over the file: no container, no runtime, the same answer everywhere, and an ERROR.
`DOC183` asks whether a declaration still matches its image, needs the probe, and stays advisory --
a laptop with no container runtime must not fail `doctor`.
"""

from __future__ import annotations

from typing import Any

from in_lockstep.adapters.sandbox import Sandbox
from in_lockstep.core.types import Test, Validate
from in_lockstep.core.verbs import declared_executables
from in_lockstep.doctor import Report, Severity, _executable_versions_agree


class _Module:
    """The parts of a lifecycle module `_sandboxes` reads."""

    def __init__(self, workshop: Any = None, **verbs: Any) -> None:
        by_verb = {Test: verbs.get("test"), Validate: verbs.get("validate")}
        self.workshop = type("_W", (), {"commands": workshop})() if workshop else None

        class _Container:
            @staticmethod
            def has(verb: type) -> bool:
                return by_verb.get(verb) is not None

            @staticmethod
            def resolve(verb: type) -> Any:
                return type("_A", (), {"sandbox": by_verb[verb]})()

        self.container = _Container()


def _codes(report: Report) -> list[tuple[str, Severity]]:
    return [(f.code, f.severity) for f in report.checks]


def test_a_tuple_declares_names_and_a_mapping_declares_versions() -> None:
    """Naming a program without its version stays supported: empty means unasked, as it always
    has. Every existing reader takes the names, and `tuple(mapping)` is its keys."""
    assert declared_executables(Sandbox(executables=("make", "python"))) == {"make": "", "python": ""}
    both = Sandbox(executables={"make": "GNU Make 4.3", "python": "Python 3.12.7"})
    assert declared_executables(both) == {"make": "GNU Make 4.3", "python": "Python 3.12.7"}
    assert tuple(both.executables) == ("make", "python"), "a reader that wants names is unaffected"


def test_doc184_two_bindings_that_disagree_about_a_version_is_an_error() -> None:
    """The bug this exists for, in its own shape: a model explores on one interpreter and its tests
    run on another. An ERROR because `report.ok` is `not self.errors` and the work jobs run plain
    `doctor` -- a warning here would print beside `DOC130` and change nothing."""
    report = Report()
    _executable_versions_agree(
        report,
        _Module(
            workshop=Sandbox(image="a", executables={"python": "Python 3.12.7"}),
            test=Sandbox(image="b", executables={"python": "Python 3.11.14"}),
        ),
    )

    assert _codes(report) == [("DOC184", Severity.ERROR)]
    said = report.checks[0].message
    assert "python" in said and "3.12.7" in said and "3.11.14" in said
    assert "workshop" in said and "Test" in said, "which bindings disagree is the actionable half"
    assert not report.ok, "an error is what stops a run; this must fail doctor"


def test_doc184_needs_no_container_runtime() -> None:
    """Arithmetic over the module (O7). It is what makes an error affordable, and it is the check
    that would have caught the real bug -- those three Pythons were three declarations nobody
    compared, not three images nobody probed."""
    report = Report()
    _executable_versions_agree(
        report,
        _Module(
            workshop=Sandbox(image="a", executables={"uv": "uv 0.5.11"}),
            test=Sandbox(image="b", executables={"uv": "uv 0.4.0"}),
        ),
    )
    assert [c for c, _s in _codes(report)] == ["DOC184"]


def test_bindings_that_agree_say_nothing() -> None:
    report = Report()
    same = {"python": "Python 3.12.7", "make": "GNU Make 4.3"}
    _executable_versions_agree(
        report,
        _Module(workshop=Sandbox(image="a", executables=same), test=Sandbox(image="b", executables=same)),
    )
    assert report.checks == []


def test_a_program_only_one_binding_names_is_not_a_finding() -> None:
    """Pairwise over SHARED programs. A `Test` image carrying `pytest` that a linter's image does
    not is ordinary, and a check that fired on it would be switched off within a week."""
    report = Report()
    _executable_versions_agree(
        report,
        _Module(
            workshop=Sandbox(image="a", executables={"python": "Python 3.12.7"}),
            test=Sandbox(image="b", executables={"pytest": "pytest 8.0.0"}),
        ),
    )
    assert report.checks == []


def test_a_binding_that_names_no_version_is_unasked_not_disagreeing() -> None:
    """Empty means unasked. A repository declaring names only keeps working exactly as before, and
    is not told its bindings disagree on the strength of one of them staying silent."""
    report = Report()
    _executable_versions_agree(
        report,
        _Module(
            workshop=Sandbox(image="a", executables={"python": "Python 3.12.7"}),
            test=Sandbox(image="b", executables=("python",)),
        ),
    )
    assert report.checks == []
