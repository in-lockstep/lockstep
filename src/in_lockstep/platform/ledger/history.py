"""The ledger on an orphan branch.

A run record is the project's evidence: what was spent, what was decided, who approved it. Writing
it into the working tree makes it either untracked — which is what happened, so every local run's
record was lost and CI's survived ninety days as an artifact — or a commit on the branch under
review, which puts framework output into the diff a human is trying to read.

An orphan branch is the answer the compiler-era pipeline already used (`origin/pipeline-history`),
and it is the right one: the records share a repository with the code they describe, travel with a
clone, and are reachable forever, while touching no branch anybody works on.

**Nothing is checked out.** Every write goes through plumbing — `hash-object`, a temporary index,
`write-tree`, `commit-tree`, `update-ref` — with `GIT_INDEX_FILE` pointed at a scratch file. The
working tree and the real index are never read and never modified, so a run can record itself in
the middle of whatever the developer had going on.

**Pushing is a separate act.** A local run appends to a local ref and stops. Reaching a remote
needs credentials and is a side effect nobody asked for when they typed a command in a terminal;
`push()` is called when a caller means it, which in practice is CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ...privileged.redact import Redact

DEFAULT_BRANCH = "lockstep-history"
RECORDS = "records"


class HistoryError(RuntimeError):
    """Git refused, and continuing would mean claiming a record was kept when it was not."""


@dataclass
class GitLedger:
    """Append-only run records, as commits on an orphan ref."""

    root: Path = field(default_factory=Path.cwd)
    branch: str = DEFAULT_BRANCH
    remote: str = "origin"
    scope: str = "local"
    redact: Redact = field(default_factory=Redact)

    # -- plumbing ------------------------------------------------------------------

    def _git(self, *args: str, stdin: str | None = None, index: Path | None = None) -> str:
        env = {**os.environ, "GIT_INDEX_FILE": str(index)} if index is not None else None
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if result.returncode != 0:
            raise HistoryError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def _try(self, *args: str) -> str | None:
        try:
            return self._git(*args)
        except HistoryError:
            return None

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"

    def head(self) -> str | None:
        """The commit the history branch points at, or None if it does not exist yet."""
        return self._try("rev-parse", "--verify", "--quiet", f"{self.ref}^{{commit}}") or None

    def path_for(self, run_id: str) -> str:
        """Where a record lives inside the branch. Not a working-tree path — it has none."""
        return f"{RECORDS}/{_safe(run_id)}.json"

    def location(self, run_id: str) -> str:
        return f"{self.branch}:{self.path_for(run_id)}  (local; `in-lockstep history --push` to publish)"

    # -- writing -------------------------------------------------------------------

    async def append(self, run_id: str, record: dict[str, object]) -> None:
        from .store import EPOCH, SCHEMA

        stamped = {"schema": SCHEMA, "epoch": EPOCH, "run_id": run_id, **record}
        # Serialize, then mask. In that order because masking the structure first would have to
        # guess which values reach the file; masking the serialized form sees the bytes that land.
        payload = self.redact.text(json.dumps(stamped, indent=2, sort_keys=True, default=repr)) + "\n"

        blob = self._git("hash-object", "-w", "--stdin", stdin=payload)
        parent = self.head()

        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index"
            if parent:
                # Start from what is already there, so a record does not replace the history.
                self._git("read-tree", parent, index=index)
            self._git(
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},{self.path_for(run_id)}",
                index=index,
            )
            tree = self._git("write-tree", index=index)

        message = f"{run_id}: {record.get('kind', 'run')}"
        args = [*self._identity(), "commit-tree", tree, "-m", message]
        if parent:
            args += ["-p", parent]
        commit = self._git(*args)

        # The old value is passed, so this is a compare-and-swap rather than a blind write: two
        # runs finishing at once cannot silently drop one another's record. `""` means "must not
        # exist", which is how the first commit on an orphan branch is created safely.
        self._git("update-ref", self.ref, commit, parent or "")

    def _identity(self) -> list[str]:
        """`-c` overrides only when the repository has no identity of its own.

        `commit-tree` refuses without one, and a CI runner frequently has none configured — so
        without this the first thing a fresh runner does with the ledger is fail. Where a
        developer HAS an identity, theirs is used: the commit is a record of their run.
        """
        if self._try("config", "user.email"):
            return []
        return [
            "-c",
            "user.name=in-lockstep",
            "-c",
            "user.email=in-lockstep@users.noreply.github.com",
        ]

    async def read(self, run_id: str) -> dict[str, object] | None:
        head = self.head()
        if head is None:
            return None
        raw = self._try("show", f"{head}:{self.path_for(run_id)}")
        return json.loads(raw) if raw else None

    def records(self) -> list[dict[str, object]]:
        """Every record currently on the branch, oldest run id first."""
        head = self.head()
        if head is None:
            return []
        listing = self._try("ls-tree", "--name-only", f"{head}:{RECORDS}") or ""
        out = []
        for name in sorted(listing.splitlines()):
            raw = self._try("show", f"{head}:{RECORDS}/{name}")
            if raw:
                out.append(json.loads(raw))
        return out

    # -- tamper-evidence -------------------------------------------------------------

    def verify(self) -> list[str]:
        """The ways the retained history contradicts append-only, one line per contradiction.

        A record is appended once and never touched again, so any commit on this branch that
        MODIFIES or DELETES a record file is evidence the past was rewritten — git allowed the
        edit but kept the contradiction, and this is what reads it. The auditor's first question
        after "when did this run" is "how do I know this record wasn't rewritten", and the
        answer must be a check, not a shrug.

        What this cannot see, stated rather than implied: a rewrite that REPLACED the chain — a
        force-push of freshly fabricated commits — discards the contradiction along with the
        commits that held it. That detection is the remote's: protect `lockstep-history` against
        force-push and deletion, which `docs/controls-crosswalk.md` lists among what must be
        true before an unattended run. Local truth plus remote protection is the whole control;
        this method is only the local half.

        A legitimate `_reconcile` or `absorb` never modifies an existing record — same run id
        with the same content produces the same blob, which git records as no change at all. A
        modification flagged here therefore means either tampering or two different runs that
        shared a run id, and the second is worth an alarm too: one of those records silently
        replaced the other.
        """
        head = self.head()
        if head is None:
            return []
        raw = self._git("log", "--format=%H", "--name-status", "--diff-filter=MD", self.ref)
        problems: list[str] = []
        commit = ""
        for line in raw.splitlines():
            if not line.strip():
                continue
            if "\t" not in line:
                commit = line.strip()
                continue
            status, path = line.split("\t", 1)
            verb = "modified" if status.startswith("M") else "deleted"
            problems.append(f"{path} was {verb} after being appended (commit {commit[:12]})")
        return problems

    # -- publishing ----------------------------------------------------------------

    def push(self) -> str:
        """Send the branch to the remote. Called when somebody means it, never on every run.

        Two runs that both recorded produce divergent orphan histories, and git rejects the second
        push. That is the ordinary case rather than a rare one — a repository with a chat-ops
        trigger has concurrent runs by design — so a rejection is reconciled once rather than
        reported as a failure the user has to resolve by hand.
        """
        if self.head() is None:
            raise HistoryError("there is no history to push")
        try:
            self._git("push", self.remote, f"{self.ref}:{self.ref}")
        except HistoryError:
            self._reconcile()
            self._git("push", self.remote, f"{self.ref}:{self.ref}")
        return f"{self.remote}/{self.branch}"

    def _reconcile(self) -> None:
        """Replay local records on top of the remote head.

        Not a merge and not a rebase: records are independent files keyed by run id, so "combine"
        means "put mine into their tree". Anything the remote has and this clone does not is
        preserved by starting from the remote tree; anything only this clone has is added.
        """
        fetched = self._try("fetch", self.remote, f"{self.ref}:refs/lockstep/remote-history")
        if fetched is None:
            # The remote has no such branch, so the rejection was about something else and
            # pretending otherwise would loop.
            raise HistoryError(
                f"could not push {self.branch} and could not fetch it from {self.remote} either"
            )
        remote_head = self._git("rev-parse", "refs/lockstep/remote-history")
        mine = self.records()

        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index"
            self._git("read-tree", remote_head, index=index)
            for record in mine:
                run_id = str(record.get("run_id", "run"))
                payload = json.dumps(record, indent=2, sort_keys=True, default=repr) + "\n"
                blob = self._git("hash-object", "-w", "--stdin", stdin=payload)
                self._git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{blob},{self.path_for(run_id)}",
                    index=index,
                )
            tree = self._git("write-tree", index=index)

        commit = self._git(
            *self._identity(),
            "commit-tree",
            tree,
            "-p",
            remote_head,
            "-m",
            f"reconcile {len(mine)} local record(s)",
        )
        self._git("update-ref", self.ref, commit)

    # -- moving history between machines --------------------------------------------

    def bundle(self, path: str | Path) -> Path:
        """Write the branch to a file, so it can travel as a CI artifact.

        The unprivileged job that records has `contents: read` and cannot push; the job that can
        push is a different runner with a fresh checkout, so a commit made in the first one dies
        with it. Same shape as the ChangeSet: the unprivileged half produces, the privileged half
        publishes — and the credential split survives.
        """
        if self.head() is None:
            raise HistoryError("there is no history to bundle")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._git("bundle", "create", str(target), self.ref)
        return target

    def absorb(self, path: str | Path) -> str:
        """Take a bundle's history into this clone, then reconcile it with whatever is here."""
        source = Path(path)
        if not source.is_file():
            raise HistoryError(f"no history bundle at {source}")
        if self.head() is None:
            self._git("fetch", str(source), f"{self.ref}:{self.ref}")
        else:
            self._git("fetch", str(source), f"{self.ref}:refs/lockstep/incoming")
            self._merge_ref("refs/lockstep/incoming")
        return self._git("rev-parse", self.ref)

    def _merge_ref(self, other: str) -> None:
        """Fold another history's records into this one. Same rule as `_reconcile`."""
        head = self.head()
        listing = (self._try("ls-tree", "--name-only", f"{other}:{RECORDS}") or "").splitlines()
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index"
            self._git("read-tree", str(head), index=index)
            for name in sorted(listing):
                blob = self._git("rev-parse", f"{other}:{RECORDS}/{name}")
                self._git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{blob},{RECORDS}/{name}",
                    index=index,
                )
            tree = self._git("write-tree", index=index)
        commit = self._git(
            *self._identity(),
            "commit-tree",
            tree,
            "-p",
            str(head),
            "-p",
            self._git("rev-parse", other),
            "-m",
            "absorb history from a bundle",
        )
        self._git("update-ref", self.ref, commit, str(head))


def _safe(run_id: str) -> str:
    """A run id is a path component here, and run ids are partly caller-supplied."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in run_id).strip("-.")
    return cleaned or "run"
