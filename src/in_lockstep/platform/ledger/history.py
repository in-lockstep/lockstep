"""The ledger on an orphan branch.

A run record is the project's evidence: what was spent, what was decided, who approved it. Writing
it into the working tree makes it either untracked — which is what happened, so every local run's
record was lost and CI's survived ninety days as an artifact — or a commit on the branch under
review, which puts framework output into the diff a human is trying to read.

An orphan branch is the answer the compiler-era pipeline already used (its `pipeline-history`
branch, deleted on 2026-09-02 once this ledger had replaced it), and it is the right one: the
records share a repository with the code they describe, travel with a clone, and are reachable for
as long as the branch is, while touching no branch anybody works on.

**Nothing is checked out.** Every write goes through plumbing — `hash-object`, a temporary index,
`write-tree`, `commit-tree`, `update-ref` — with `GIT_INDEX_FILE` pointed at a scratch file. The
working tree and the real index are never read and never modified, so a run can record itself in
the middle of whatever the developer had going on.

**Pushing is a separate act.** A local run appends to a local ref and stops. Reaching a remote
needs credentials and is a side effect nobody asked for when they typed a command in a terminal;
`push()` is called when a caller means it, which in practice is CI.

**Reading falls back to the remote-tracking ref.** A CI checkout and a fresh clone create no local
branch for a ref that is not HEAD, so `refs/heads/lockstep-history` is absent on every machine
except the one that wrote it -- and for as long as the ledger read that ref and nothing else, every
reader in CI read nothing: the nightly sweep found every artifact outstanding, the learning loop
found no trend in an empty census, and a second engineer's `report` said "no records yet" while
the shared branch held thirty-four paid runs (#307). So a read that finds no local ref reads
`refs/remotes/<remote>/<branch>` instead, says which it read, and creates nothing: the local ref
comes into being by a write or by `pull()`, both of which are acts somebody asked for.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ...privileged.redact import Redact

DEFAULT_BRANCH = "lockstep-history"
RECORDS = "records"
#: Where an acknowledged rewrite is written down: one file per acknowledged commit, appended
#: like a record and protected like one, because `verify` walks the whole tree and a note that
#: was later edited is a contradiction in its own right.
ACKNOWLEDGED = "acknowledged"


@dataclass(frozen=True)
class Acknowledgement:
    """A rewrite somebody stood behind by name: which commit, who, why, and what it rewrote.

    Not an allow-list. `verify` still finds every contradiction; this is the shape in which one
    of them stops being an alarm, and it is printed where the alarm was. A tamper flag nobody
    can acknowledge is one everybody learns to read past, and then the next one is read past too
    (#295).
    """

    commit: str
    by: str
    reason: str
    ts: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Divergence:
    """Which ref a read came from, and how the local and remote-tracking refs differ in records.

    Counted in records, not commits, because a reconcile or a pull adds commits that carry nothing
    new. `None` on either count means one of the two refs does not exist, which a reader renders
    as a dash: a clone that never fetched the branch is not "0 behind".
    """

    read: str
    local_only: int | None
    remote_only: int | None


@dataclass(frozen=True)
class Pulled:
    """What one `pull()` did: the commit the local ref ended on, and how many records it gained."""

    commit: str
    gained: int
    created: bool


class HistoryError(RuntimeError):
    """Git refused, and continuing would mean claiming a record was kept when it was not."""


#: Run ids that only a test or a worked example mints. Two shapes, and exactly the two that have
#: reached this repository's real ledger: `triage-412` is the id `tests/in_lockstep/test_cli.py`
#: records under, and `wayfinder-*-local` are the example's own runs; both arrived on
#: `lockstep-history` in the 2026-09-02 reconcile and `report` counted `triage 1 run(s)` for a
#: verb that has never run here (#312). Enumerated rather than inferred from the id's shape,
#: because a rule that refused every id without the CLI's timestamp would refuse the records this
#: module's own tests push, and the ids the framework mints are a convention, not a contract.
FIXTURE_RUN_ID = re.compile(r"^(?:triage-\d+|[A-Za-z0-9_.-]+-local)$")


def refuse_fixture_ids(run_ids: Iterable[str], *, already: Collection[str] = ()) -> None:
    """Raise if any run id about to be PUBLISHED is one only a fixture mints.

    Publishing means crossing to a branch other clones read: a push, the reconcile a rejected push
    makes, and a bundle absorbed on the way to one. A local append accepts anything, because a
    test's own tmp-root ledger is exactly where a fixture id belongs. `already` exempts ids the
    destination holds, so a branch that still carries the four that leaked can be pushed to and
    pulled from until the person who removes them does -- a refusal that also blocked the cleanup
    would be a refusal nobody could clear.
    """
    # Both sides may be record file names (`<run id>.json`) or bare run ids; compare bare ids.
    held = {_bare(name) for name in already}
    offending = sorted({_bare(r) for r in run_ids if FIXTURE_RUN_ID.match(_bare(r)) and _bare(r) not in held})
    if offending:
        raise HistoryError(
            f"refusing to publish {len(offending)} record(s) whose run id only a fixture mints: "
            f"{', '.join(offending)}. A test or example wrote to this repository's ledger instead "
            f"of its own tmp root; delete the record(s) locally and fix the test."
        )


def _bare(name: str) -> str:
    return name[: -len(".json")] if name.endswith(".json") else name


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

    @property
    def remote_ref(self) -> str:
        """Where a fetch leaves the remote's copy of the branch. Read when the local ref is absent."""
        return f"refs/remotes/{self.remote}/{self.branch}"

    def _commit_at(self, ref: str) -> str | None:
        return self._try("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") or None

    def resolved(self) -> tuple[str, str] | None:
        """`(ref, commit)` a read comes from: the local branch, or the remote-tracking ref when no
        local branch exists, or None when neither does. The fallback is the read path for every
        checkout that did not write the branch -- CI, a fresh clone, a second engineer -- and it
        creates nothing: a read that left a ref behind would be a write nobody asked for (#307)."""
        for ref in (self.ref, self.remote_ref):
            commit = self._commit_at(ref)
            if commit:
                return ref, commit
        return None

    def head(self) -> str | None:
        """The commit a read starts from, or None if neither the local nor the remote-tracking
        ref exists yet. Writers advance the LOCAL ref from it; see `_advance`."""
        found = self.resolved()
        return found[1] if found else None

    def _advance(self, commit: str, parent: str | None) -> None:
        """Move the local ref to `commit`, compare-and-swap against what it was.

        `parent` is what `head()` returned when the write began. When that came from the local
        ref, the swap is against it, so two runs finishing at once cannot silently drop one
        another's record. When it came from the remote-tracking ref there is no local ref, and
        `""` says "must not exist": the first write on such a checkout creates the local branch
        from the remote's commit plus the new record, in one step, or fails if something else
        created it first.
        """
        expected = parent if parent and self._commit_at(self.ref) else ""
        self._git("update-ref", self.ref, commit, expected)

    def divergence(self) -> Divergence:
        """How the local and remote-tracking refs differ, in records, and which one a read uses."""
        found = self.resolved()
        local = self._commit_at(self.ref)
        remote = self._commit_at(self.remote_ref)
        if local is None or remote is None:
            return Divergence(read=found[0] if found else "", local_only=None, remote_only=None)
        mine, theirs = self._record_names(local), self._record_names(remote)
        return Divergence(read=self.ref, local_only=len(mine - theirs), remote_only=len(theirs - mine))

    def _record_names(self, commit: str) -> set[str]:
        return set((self._try("ls-tree", "--name-only", f"{commit}:{RECORDS}") or "").splitlines())

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

        self._advance(commit, parent)

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

        What comes back is every contradiction nobody has acknowledged. An acknowledged one is a
        commit somebody stood behind by name, in a note appended to this branch (`acknowledge`),
        and `acknowledged_rewrites` returns those with the note — so a reader still sees the
        rewrite, under the name of the person who explained it, instead of an alarm that has
        been read past for days (#295). The note covers exactly the commit it names: a rewrite
        in any other commit, before or after, is still an alarm.
        """
        acknowledged = self.acknowledgements()
        return [line for commit, line in self._contradictions() if commit not in acknowledged]

    def acknowledged_rewrites(self) -> list[Acknowledgement]:
        """The contradictions somebody acknowledged, each with who, why, and what it rewrote."""
        notes = self.acknowledgements()
        lines: dict[str, list[str]] = {}
        for commit, line in self._contradictions():
            if commit in notes:
                lines.setdefault(commit, []).append(line)
        return [
            Acknowledgement(
                commit=commit,
                by=str(notes[commit].get("by", "")),
                reason=str(notes[commit].get("reason", "")),
                ts=str(notes[commit].get("ts", "")),
                lines=tuple(found),
            )
            for commit, found in lines.items()
        ]

    def _contradictions(self) -> list[tuple[str, str]]:
        """Every commit that modified or deleted a file after its append, as (commit, line)."""
        head = self.head()
        if head is None:
            return []
        raw = self._git("log", "--format=%H", "--name-status", "--diff-filter=MD", head)
        problems: list[tuple[str, str]] = []
        commit = ""
        for line in raw.splitlines():
            if not line.strip():
                continue
            if "\t" not in line:
                commit = line.strip()
                continue
            status, path = line.split("\t", 1)
            verb = "modified" if status.startswith("M") else "deleted"
            problems.append((commit, f"{path} was {verb} after being appended (commit {commit[:12]})"))
        return problems

    # -- acknowledging a rewrite -------------------------------------------------------

    def acknowledgements(self) -> dict[str, dict[str, object]]:
        """Every acknowledgement on the branch, keyed by the full commit it stands behind."""
        head = self.head()
        if head is None:
            return {}
        listing = self._try("ls-tree", "--name-only", f"{head}:{ACKNOWLEDGED}") or ""
        out: dict[str, dict[str, object]] = {}
        for name in sorted(listing.splitlines()):
            raw = self._try("show", f"{head}:{ACKNOWLEDGED}/{name}")
            if raw:
                note = json.loads(raw)
                if isinstance(note, dict) and note.get("commit"):
                    out[str(note["commit"])] = note
        return out

    def acknowledge(self, commit: str, *, reason: str, by: str) -> Acknowledgement:
        """Stand behind one rewrite by name. Appended, never edited; returns the note it wrote.

        Refused in every case where the note would say nothing checkable: no name or no reason
        (a shrug on the record), a commit that is not on this branch, a commit that rewrote
        nothing (nothing to acknowledge, and a note about it would be the allow-list this is not),
        and a commit already acknowledged (the first note stands, and a second would have to edit
        it — which `verify` would flag). What the commit rewrote is copied into the note, so a
        reader of the note alone knows what was stood behind.
        """
        from datetime import UTC, datetime

        if not reason.strip() or not by.strip():
            raise HistoryError("an acknowledgement names who and why, or it is a shrug on the record")
        head = self.head()
        if head is None:
            raise HistoryError("there is no history to acknowledge anything on")
        full = self._try("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
        if not full or self._try("merge-base", "--is-ancestor", full, head) is None:
            raise HistoryError(f"{commit} is not a commit on {self.branch}")
        rewrote = [line for at, line in self._contradictions() if at == full]
        if not rewrote:
            raise HistoryError(
                f"{full[:12]} rewrote nothing after its append; there is nothing to acknowledge"
            )
        already = self.acknowledgements().get(full)
        if already is not None:
            raise HistoryError(f"{full[:12]} is already acknowledged by {already.get('by', '?')}")
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        note = {"commit": full, "by": by.strip(), "reason": reason.strip(), "ts": stamp, "rewrote": rewrote}
        payload = self.redact.text(json.dumps(note, indent=2, sort_keys=True)) + "\n"
        blob = self._git("hash-object", "-w", "--stdin", stdin=payload)
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index"
            self._git("read-tree", head, index=index)
            self._git(
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},{ACKNOWLEDGED}/{full}.json",
                index=index,
            )
            tree = self._git("write-tree", index=index)
        subject = f"acknowledge {full[:12]}: rewrote {len(rewrote)} record(s)"
        made = self._git(*self._identity(), "commit-tree", tree, "-p", head, "-m", subject)
        self._advance(made, head)
        return Acknowledgement(
            commit=full, by=by.strip(), reason=reason.strip(), ts=stamp, lines=tuple(rewrote)
        )

    # -- publishing ----------------------------------------------------------------

    def push(self) -> str:
        """Send the branch to the remote. Called when somebody means it, never on every run.

        Two runs that both recorded produce divergent orphan histories, and git rejects the second
        push. That is the ordinary case rather than a rare one — a repository with a chat-ops
        trigger has concurrent runs by design — so a rejection is reconciled once rather than
        reported as a failure the user has to resolve by hand.
        """
        if self._commit_at(self.ref) is None:
            # The local ref, not `head()`: a checkout reading the remote-tracking ref has nothing
            # of its own to publish, and pushing the remote's commit back at it would say "pushed".
            raise HistoryError("there is no history here to push")
        # Before the push and not only inside the reconcile a rejection triggers: a first push to
        # an empty remote is fast-forward and would carry a fixture record straight through.
        theirs = self._commit_at(self.remote_ref)
        refuse_fixture_ids(
            (str(r.get("run_id", "")) for r in self.records()),
            already=self._record_names(theirs) if theirs else (),
        )
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
        fetched = self._try("fetch", self.remote, f"+{self.ref}:{_REMOTE_SCRATCH}")
        if fetched is None:
            # The remote has no such branch, so the rejection was about something else and
            # pretending otherwise would loop.
            raise HistoryError(
                f"could not push {self.branch} and could not fetch it from {self.remote} either"
            )
        remote_head = self._git("rev-parse", _REMOTE_SCRATCH)
        self._git("update-ref", "-d", _REMOTE_SCRATCH)
        mine = self.records()
        refuse_fixture_ids((str(r.get("run_id", "")) for r in mine), already=self._record_names(remote_head))

        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index"
            self._git("read-tree", remote_head, index=index)
            # Acknowledgements travel with the records they explain. Both rebuilds below start
            # from the OTHER side's tree and add this side's records, which would drop a note this
            # side made -- and a note that vanished on push would be a flag that came back.
            self._carry(index, str(self.head()), ACKNOWLEDGED)
            for record in mine:
                run_id = str(record.get("run_id", "run"))
                payload = json.dumps(record, indent=2, sort_keys=True, default=repr) + "\n"
                blob = self._git("hash-object", "-w", "--stdin", stdin=payload)
                # Refuse to replace an existing record the remote already holds. Identical content
                # produces the same blob, which is a no-op; different content means two runs
                # shared a run id and one would silently overwrite the other (#319).
                remote_existing = self._try("rev-parse", f"{remote_head}:{self.path_for(run_id)}")
                if remote_existing is not None and remote_existing != blob:
                    raise HistoryError(
                        f"run id {run_id!r} already exists on the remote with different content"
                        f" — the remote holds {remote_existing[:12]}, this clone has {blob[:12]};"
                        f" re-record under a distinct run id to publish this record"
                    )
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

    def pull(self) -> Pulled:
        """Bring the remote's records onto the local ref, and push nothing.

        The other half of `push()`, for the engineer who did not write the records: a fetch, then
        the same record-by-record fold `_merge_ref` does, so a run id both sides carry is kept
        once and an acknowledgement either side made travels. Nothing to fold is nothing done --
        no merge commit is made for a remote the local ref already contains -- and a clone with
        no local branch gets one pointing at the remote's commit, because that is what pulling
        means. Reading never does this; `report` says how far the two refs differ instead (#307).
        """
        fetched = self._try("fetch", self.remote, f"+{self.ref}:{self.remote_ref}")
        if fetched is None:
            raise HistoryError(f"could not fetch {self.branch} from {self.remote}")
        theirs = self._commit_at(self.remote_ref)
        if theirs is None:
            raise HistoryError(f"{self.remote} has no {self.branch} to pull")
        mine = self._commit_at(self.ref)
        before = self._record_names(mine) if mine else set()
        if mine is None:
            self._git("update-ref", self.ref, theirs, "")
            return Pulled(commit=theirs, gained=len(self._record_names(theirs)), created=True)
        if self._try("merge-base", "--is-ancestor", theirs, mine) is not None:
            return Pulled(commit=mine, gained=0, created=False)
        self._merge_ref(self.remote_ref, subject=f"pull history from {self.remote}")
        after = self._git("rev-parse", self.ref)
        return Pulled(commit=after, gained=len(self._record_names(after) - before), created=False)

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

    def absorb(self, path: str | Path, *, run_id: str = "") -> str:
        """Take a bundle's history into this clone, then reconcile it with whatever is here.

        `run_id` is the host's id for the run that made the bundle, and it goes into the absorb
        commit's subject so `absorbed_runs` can read it back: that is what lets a sweep take an
        artifact in once even when the bundle carried no record naming its run (#294).
        """
        source = Path(path)
        if not source.is_file():
            raise HistoryError(f"no history bundle at {source}")
        if self.head() is None:
            # Through the scratch ref even here, so a bundle that would create the branch is
            # checked the same way one that merges into it is.
            self._git("fetch", str(source), f"+{self.ref}:{_INCOMING_SCRATCH}")
            try:
                refuse_fixture_ids(self._record_names(self._git("rev-parse", _INCOMING_SCRATCH)))
                self._git("update-ref", self.ref, self._git("rev-parse", _INCOMING_SCRATCH), "")
            finally:
                self._try("update-ref", "-d", _INCOMING_SCRATCH)
            if run_id:
                self.note_absorbed(run_id)
        else:
            # Forced, and deleted once folded. Each bundle is a different runner's orphan
            # history, so the second bundle of a sweep is never a descendant of the first, and a
            # plain fetch into a ref the first left behind is refused non-fast-forward: the first
            # dispatched sweep absorbed one bundle and reported 219 failures (#323). The scratch
            # ref is a temporary, and a temporary that outlives its use is a dependency between
            # absorbs nobody meant.
            self._git("fetch", str(source), f"+{self.ref}:{_INCOMING_SCRATCH}")
            try:
                head = self.head()
                refuse_fixture_ids(
                    self._record_names(self._git("rev-parse", _INCOMING_SCRATCH)),
                    already=self._record_names(head) if head else (),
                )
                self._merge_ref(_INCOMING_SCRATCH, run_id=run_id)
            finally:
                self._try("update-ref", "-d", _INCOMING_SCRATCH)
        return self._git("rev-parse", self.ref)

    def note_absorbed(self, run_id: str) -> None:
        """An empty absorb: a commit with the same tree that only says the run was looked at.

        For an artifact that carried no bundle, or a bundle that created the branch outright and
        so made no merge commit. Without it the sweep would download the same empty artifact
        every day until it expired, and "once" would be a claim about the ordinary case only.
        """
        head = self.head()
        if head is None or not run_id:
            return
        tree = self._git("rev-parse", f"{head}^{{tree}}")
        commit = self._git(*self._identity(), "commit-tree", tree, "-p", head, "-m", _absorb_subject(run_id))
        self._advance(commit, head)

    def absorbed_runs(self) -> set[str]:
        """Which runs' artifacts this branch has already taken in, read from the absorb commits'
        own subjects — the second half of "once", beside the `ci_run` a record carries."""
        head = self.head()
        log = (self._try("log", "--format=%s", head) or "") if head else ""
        return set(_ABSORBED.findall(log))

    def _merge_ref(self, other: str, *, run_id: str = "", subject: str = "") -> None:
        """Fold another history's records into this one. Same rule as `_reconcile`."""
        head = self.head()
        listing = (self._try("ls-tree", "--name-only", f"{other}:{RECORDS}") or "").splitlines()
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "index"
            self._git("read-tree", str(head), index=index)
            for name in sorted(listing):
                blob = self._git("rev-parse", f"{other}:{RECORDS}/{name}")
                # Refuse to replace a record already present with different content. Identical
                # blobs are the same file and require no action; different blobs mean two distinct
                # runs shared a run id and one would silently erase the other (#319).
                local_existing = self._try("rev-parse", f"{head}:{RECORDS}/{name}")
                if local_existing is not None and local_existing != blob:
                    incoming_run_id = name[: -len(".json")] if name.endswith(".json") else name
                    raise HistoryError(
                        f"run id {incoming_run_id!r} already exists locally with different content"
                        f" — local holds {local_existing[:12]}, incoming has {blob[:12]};"
                        f" re-record under a distinct run id to publish this record"
                    )
                self._git(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{blob},{RECORDS}/{name}",
                    index=index,
                )
            self._carry(index, other, ACKNOWLEDGED)
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
            subject or _absorb_subject(run_id),
        )
        self._advance(commit, head)

    def _carry(self, index: Path, source: str, subdir: str) -> None:
        """Copy every entry of `subdir` from `source`'s tree into the index being built."""
        listing = self._try("ls-tree", "--name-only", f"{source}:{subdir}") or ""
        for name in sorted(listing.splitlines()):
            blob = self._git("rev-parse", f"{source}:{subdir}/{name}")
            self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{subdir}/{name}", index=index)


#: Scratch refs a fetch lands on before its commits are folded in. Written with a forced refspec
#: and deleted afterwards, so no fold depends on the one before it (#323).
_INCOMING_SCRATCH = "refs/lockstep/incoming"
_REMOTE_SCRATCH = "refs/lockstep/remote-history"

#: The absorb commit's subject, with the run it took in when the caller knew it. Read back by
#: `absorbed_runs`, so the subject is a format and not prose: change both or neither.
_ABSORBED = re.compile(r"^absorb history from a bundle \(run (\S+)\)$", re.M)


def _absorb_subject(run_id: str) -> str:
    return f"absorb history from a bundle (run {run_id})" if run_id else "absorb history from a bundle"


def _safe(run_id: str) -> str:
    """A run id is a path component here, and run ids are partly caller-supplied."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in run_id).strip("-.")
    return cleaned or "run"
