"""The suite has to pass where the FRAMEWORK runs it, not only where a person does.

A person runs `pytest` from the repository. Every model run this framework makes runs it from a
materialised worktree, inside a container, over HEAD plus a staged change — and a linked worktree's
`.git` is a *file* pointing at `<repo>/.git/worktrees/<id>`, a path the container does not mount.
`adapters/worktree.py` has warned about that gitlink since it was written: a normal suite runs,
because pytest does not read `.git`, but git-dependent tooling in the suite cannot resolve the
repository.

That warning came true and cost a run. Four tests in `test_fork_propose.py` called `git ls-remote`
with no `cwd=`, so it inherited the process's working directory; every one passed on a laptop, in a
worktree, and in CI's container job, and every one failed inside a model's green run. Run
34363672287 was told `tdd.not_green` -- "the implementation did not make the staged test pass" --
about a change whose own tests passed and whose suite is green everywhere a human looks. $11.29,
and the work was discarded.

What breaks is narrower than "git needs a cwd", so the rule below is narrow too. Measured against
git 2.x with a dangling gitlink as the working directory:

    git init <path>          ok      creates a repository; discovers nothing
    git clone <src> <dst>    ok      same
    git --version            ok
    git -C <path> ...        ok      -C moves before discovery
    git ls-remote <path>     FAILS   reads the ambient repository's config first,
                                     `fatal: not a git repository: (null)`, exit 128

So a git call in this suite either says where it runs -- `cwd=` or `-C` -- or is one of the forms
that provably needs no repository. Anything else depends on ambient state that is a repository for
a person and a broken gitlink for the framework, and the difference is invisible until a paid run
finds it.

The sibling rule, in CLAUDE.md and not mechanised here because a grep cannot see it: a fixture whose
git commands name `main` must pin the branch with `git branch -M main`. Same class of trap -- green
for the person who wrote it, red only where it matters.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]

#: The first argv word after `git` for the forms that need no ambient repository. `-C` is here
#: because it moves before discovery, which is the same guarantee `cwd=` gives.
NEEDS_NO_REPOSITORY = frozenset({"init", "clone", "--version", "-C"})

#: How a subprocess is spawned. `check_output` and `Popen` are here so the rule cannot be sidestepped
#: by spelling the call differently.
SPAWNERS = frozenset({"run", "check_output", "Popen", "call"})


def _git_calls_without_a_place(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in SPAWNERS:
            continue
        argv = node.args[0]
        if not isinstance(argv, ast.List) or not argv.elts:
            continue
        head = argv.elts[0]
        if not (isinstance(head, ast.Constant) and head.value == "git"):
            continue
        if any(keyword.arg == "cwd" for keyword in node.keywords):
            continue
        words = [e.value for e in argv.elts[1:] if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if words and words[0] in NEEDS_NO_REPOSITORY:
            continue
        found.append((node.lineno, "git " + " ".join(words[:3])))
    return found


def test_a_git_call_in_this_suite_says_where_it_runs() -> None:
    """The rule this file exists for. A call that inherits the working directory is a call that
    passes for whoever wrote it and fails where the framework runs the suite."""
    offenders = [
        f"{path.relative_to(TESTS)}:{line}  {snippet}"
        for path in sorted(TESTS.rglob("*.py"))
        if path != Path(__file__)
        for line, snippet in _git_calls_without_a_place(path)
    ]
    assert not offenders, (
        "these git calls inherit the process working directory, which is a materialised worktree "
        "with a dangling gitlink in every model run:\n  " + "\n  ".join(offenders)
    )


def test_the_check_can_see_an_offender() -> None:
    """The control, in the file rather than in a commit message: a rule whose detector matches
    nothing passes forever and protects nothing."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        offender = Path(tmp) / "test_planted.py"
        offender.write_text(
            "import subprocess\n\n\ndef test_x() -> None:\n"
            '    subprocess.run(["git", "ls-remote", "--heads", "/somewhere"], check=True)\n'
        )
        assert _git_calls_without_a_place(offender) == [(5, "git ls-remote --heads /somewhere")]

        allowed = Path(tmp) / "test_allowed.py"
        allowed.write_text(
            "import subprocess\n\n\ndef test_x(tmp_path) -> None:\n"
            '    subprocess.run(["git", "init", "-q"], check=True)\n'
            '    subprocess.run(["git", "-C", "/x", "status"], check=True)\n'
            '    subprocess.run(["git", "log"], cwd="/x", check=True)\n'
        )
        assert _git_calls_without_a_place(allowed) == []
