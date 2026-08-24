"""The last stage of compilation, which this compiler does not perform.

`lockstep compile` emits an agent as markdown; `gh aw compile` turns that markdown into the
`.lock.yml` a runner actually executes. Everything the drift gate proved up to this point — that the
committed output is a function of the spec, that no overlay widened the security surface — stopped
one layer above the file that runs. A reviewer approved a turn limit and a tool deny-list in a
document that is not what GitHub reads.

So the seam is inside the gate now. `compile` produces the lock files, `compile --check` regenerates
them from the committed markdown and compares byte for byte, and a missing `gh aw` is an error
rather than a skip: a check that cannot verify the artifact has not verified the artifact.

Two properties make this workable, both established by running the real tool rather than assumed:
`gh aw compile` is deterministic — the same markdown produces byte-identical output across runs —
and its "safe update" approval of new secrets and actions is recorded *inside* the lock file, so a
committed lock file is its own baseline and a regeneration beside it needs no interactive approval.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import LockstepError

LOCK_SUFFIX = ".lock.yml"
AGENT_PREFIX = "aw-"


class GhAwError(LockstepError):
    code = "LS600"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=300, check=False)
    except FileNotFoundError as exc:
        raise GhAwError(
            "`gh` is not installed, so the compiled workflows cannot be verified",
            hint="install the GitHub CLI and the agentic-workflows extension: "
            "`gh extension install github/gh-aw`. This is not optional for a check — the "
            "`.lock.yml` files are what actually run, and nothing else in this repository can "
            "confirm they match the markdown they were generated from",
        ) from exc
    except subprocess.SubprocessError as exc:
        raise GhAwError(f"`{' '.join(args)}` did not complete: {exc}") from exc


def version(cwd: Path | None = None) -> str:
    """The installed `gh aw` version, as `vX.Y.Z`."""
    result = _run(["gh", "aw", "version"], cwd or Path.cwd())
    if result.returncode != 0:
        raise GhAwError(
            "the agentic-workflows extension is not installed",
            hint="`gh extension install github/gh-aw` — the compiled `.lock.yml` files cannot "
            "be produced or verified without it",
        )
    # gh-aw prints its version on stderr, so both streams are read rather than assumed.
    for token in (result.stdout + " " + result.stderr).split():
        if token.startswith("v") and token[1:2].isdigit():
            return token
    raise GhAwError(f"could not read a version out of {(result.stdout + result.stderr).strip()!r}")


def require(expected: str, *, cwd: Path | None = None) -> str:
    """The installed version, refusing one that differs from what the manifest pins.

    A lock file is a function of the markdown *and* of the tool that compiled it, so comparing
    against output from a different version answers a question nobody asked. Refusing is better
    than reporting drift that is really a version difference — that reads as a spec problem and
    sends the reader to the wrong file.
    """
    installed = version(cwd)
    if expected and installed != expected:
        raise GhAwError(
            f"gh-aw {installed} is installed but this pipeline pins {expected}",
            hint=f"install {expected} to match what the committed lock files were built with, or "
            f"set capabilities.gh-aw to {installed} and recompile — a lock file compiled by a "
            "different version is a different file, and comparing them proves nothing",
        )
    return installed


def compile_locks(workflows: Path, *, approve: bool = False) -> dict[str, str]:
    """Run `gh aw compile` over a directory of agent markdown and return the lock files it wrote.

    Runs in a throwaway copy so a verification never edits the repository it is verifying, and so a
    partial failure leaves nothing behind. The copy carries the existing lock files with it: they
    are the baseline gh-aw's safe-update check compares against, and without them every secret and
    action reads as newly introduced.
    """
    agents = sorted(p.name for p in workflows.glob(f"{AGENT_PREFIX}*.md"))
    if not agents:
        return {}

    with tempfile.TemporaryDirectory(prefix="lockstep-ghaw-") as scratch:
        root = Path(scratch)
        target = root / ".github" / "workflows"
        target.parent.mkdir(parents=True)
        shutil.copytree(workflows, target)
        # `gh aw compile` with no arguments requires a repository to sit in.
        init = _run(["git", "init", "--quiet"], root)
        if init.returncode != 0:
            raise GhAwError(f"could not prepare a scratch repository: {init.stderr.strip()[:200]}")

        args = ["gh", "aw", "compile"]
        if approve:
            # Records the current secrets and actions as reviewed, inside the lock file itself.
            args.append("--approve")
        result = _run(args, root)
        if result.returncode != 0:
            raise GhAwError(
                "`gh aw compile` failed",
                hint=_readable(result.stdout, result.stderr),
            )
        produced = {p.name: p.read_text(encoding="utf-8") for p in sorted(target.glob(f"*{LOCK_SUFFIX}"))}

    missing = [name for name in agents if _lock_name(name) not in produced]
    if missing:
        # gh-aw reports this as a warning and carries on, which would leave a workflow referencing
        # a file that was never written — an invalid workflow, discovered by GitHub rather than here.
        raise GhAwError(
            f"`gh aw compile` produced no lock file for {', '.join(missing)}",
            hint="run `gh aw compile --approve` in this repository and commit the result. gh-aw "
            "withholds output when an agent introduces a secret or an action it has not recorded "
            "as reviewed, which is a change worth looking at before it is approved",
        )
    return produced


def _lock_name(markdown: str) -> str:
    return markdown.removesuffix(".md") + LOCK_SUFFIX


def _readable(stdout: str, stderr: str) -> str:
    """gh-aw writes its diagnosis to stdout; the useful lines are the ones that are not decoration."""
    lines = [line.strip() for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
    return " · ".join(lines[:8]) or "no output"
