"""GitHub. Contains a GitLocal rather than replacing it."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ...core.changes import ChangeGuard
from ...core.types import ChangeSet
from ..ledger.reconcile import RunArtifact
from .base import (
    MAX_CHANGES_READ,
    MAX_REMARK_CHARS,
    MAX_REMARKS,
    ChangeRequest,
    Diff,
    GitLocal,
    Ref,
    Remark,
    TargetRefused,
    branch_for,
    change_body,
    conventional_subject,
    is_run_branch,
    is_run_branch_for,
    is_run_branch_of,
    ticket_from_branch,
    title_line,
    trailers_from,
)

#: What `gh pr view <n>` says when `<n>` is an issue or nothing at all -- the one failure that is
#: an answer. Anything else it says (`Not logged in`, `Bad credentials`, a timeout) is a failure
#: to ask, and `change_refs` raises it rather than reading it as "not a pull request".
_NOT_A_PULL_REQUEST = re.compile(
    r"no pull requests? found|not a pull request|Could not resolve to a PullRequest|HTTP 404", re.I
)


class GitHubScm:
    #: GitHub draws issue and pull-request numbers from ONE sequence, so a number is one or the
    #: other and never both. That is what lets a comment be resolved to the work it is about from
    #: its number alone, and it is a fact about the host rather than about this adapter — hence a
    #: flag a caller can read instead of a `isinstance` check it would have to keep updated.
    shared_numbering = True

    def __init__(
        self,
        root: str | Path = ".",
        *,
        guard: ChangeGuard | None = None,
        token: str = "",
        repo: str = "",
    ) -> None:
        # Host features layer over plain git; they do not replace it.
        self.local = GitLocal(root, guard=guard)
        self.root = Path(root)
        self.token = token
        #: Whose conversation this adapter reads and answers in, when that is not the repository
        #: `gh` infers from the checkout. See `for_repo` for which calls honour it and which
        #: deliberately do not.
        self.repo = repo

    def for_repo(self, repo: str) -> GitHubScm:
        """This adapter, reading the conversation on another repository -- what a fork needs to
        implement from the ticket, and the review of the last attempt, that live on the parent.

        **The rule, because half the calls here must not move.** A call that addresses a NUMBER
        follows the narrowing: an issue or pull request number belongs to a repository, and asking
        the wrong one is either an error or, worse, an answer about somebody else's #17. A call
        about THIS CHECKOUT'S OWN RUNS does not: `run_artifacts`, `download_artifact`,
        `delivery_rows` and `open_changes_by_workflow` are about the CI that is executing, which is
        the fork's, whoever the change is for.

        And the write does not, deliberately. `open_change` takes `target` as an argument of its
        own, so where a branch and a pull request are created is named at the call that creates
        them rather than inherited from an adapter somebody narrowed three functions earlier. One
        spelling per act: this one is for reading and answering, that one is for writing.
        """
        import copy

        clone = copy.copy(self)
        # A shallow copy on purpose: the same checkout, the same guard, the same credential. What
        # differs is one string.
        clone.repo = repo
        return clone

    def _at(self) -> tuple[str, ...]:
        """`--repo <slug>`, or nothing at all -- for the `gh pr` calls that address a number."""
        return ("--repo", self.repo) if self.repo else ()

    def _path(self, path: str) -> str:
        """A `gh api` path with the repository filled in. `gh` substitutes `{owner}/{repo}` from
        the checkout, which is the inference this exists to replace, so the narrowed adapter puts
        its own slug there instead."""
        return path.replace("{owner}/{repo}", self.repo) if self.repo else path

    def diff(self, base: Ref, head: Ref = "HEAD") -> Diff:
        return self.local.diff(base, head)

    def _gh(self, *args: str) -> tuple[int, str, str]:
        env = None
        if self.token:
            import os

            env = {**os.environ, "GH_TOKEN": self.token}
        result = subprocess.run(
            ["gh", *args], cwd=self.root, capture_output=True, text=True, timeout=60, env=env
        )
        return result.returncode, result.stdout, result.stderr

    def _gh_json(self, *args: str) -> Any:
        code, out, err = self._gh(*args)
        if code != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed: {err.strip()}")
        return json.loads(out) if out.strip() else None

    def origin_slug(self) -> str:
        """`owner/repo` for the repository this CHECKOUT pushes to, or empty when nothing says.

        Not the repository `gh` would infer: from a fork `gh` infers the parent, which is right for
        reading an issue and wrong for naming a head, and that single inference serving two
        opposite purposes is the whole of #373.

        Two sources, in the order `hosted_tickets` already reads GitLab's: the host's own statement
        first (`GITHUB_REPOSITORY` is what Actions says the workflow is running for, and a fork's
        run says the fork), then the origin remote's URL, which is all a laptop has. A checkout of
        some OTHER repository under a workflow would make the two disagree, and the failure is
        loud — GitHub refuses a `head_repo` that does not hold the branch — rather than a change
        opened somewhere nobody looked.
        """
        import os

        stated = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if stated:
            return stated
        return owner_repo_from_remote(self.local.git("config", "--get", "remote.origin.url").strip())

    def _target_default_branch(self, target: str) -> str:
        """Refuse unless the credential may push to `target`; answer with its default branch.

        ONE request answers both questions, and it is made before anything local is written. The
        rights half is what a fork's CI needs: its workflow token can read the parent and cannot
        write it, so without this the run does its work, pays for its model calls, pushes a branch
        and only then learns it was never going to be able to open anything. The default-branch
        half is not a convenience: `POST /repos/{owner}/{repo}/pulls` REQUIRES `base`, where
        `gh pr create` defaults it from the repository — so every propose call, which passes no
        base at all, would 422 on the one path this exists to add.
        """
        code, out, err = self._gh("api", f"repos/{target}")
        if code != 0:
            raise TargetRefused(
                "scm.no_rights_on_target",
                f"the credential cannot read {target}: {err.strip() or f'gh exited {code}'}",
            )
        data = json.loads(out) if out.strip() else {}
        data = data if isinstance(data, dict) else {}
        permissions = data.get("permissions")
        if not (isinstance(permissions, dict) and permissions.get("push")):
            raise TargetRefused(
                "scm.no_rights_on_target",
                f"the credential can read {target} but not write it. A fork's workflow token has "
                f"no rights on the repository it forked from; only a person's credential, or a "
                f"token the fork declares, can open a change there.",
            )
        return str(data.get("default_branch") or "")

    async def open_change(
        self,
        cs: ChangeSet,
        *,
        title: str,
        body: str = "",
        ticket: str = "",
        workflow: str = "",
        run_id: str = "",
        base: Ref = "",
        draft: bool = False,
        target: str = "",
    ) -> ChangeRequest:
        branch = branch_for(workflow or "change", run_id or "run", ticket=ticket)
        # Refused at the framework rather than relying on the token's scope, because the token is
        # ambient and can write any branch.
        self.local.assert_run_scoped(branch)

        # Which repository this change is FOR, decided before a byte is written. An empty target —
        # every caller before #373 — changes nothing at all: the branch goes to origin and
        # `gh pr create` infers the rest exactly as it always has. A target that names origin is
        # the same path, so a trampoline can pass the flag unconditionally.
        origin = self.origin_slug()
        elsewhere = bool(target) and target != origin
        if elsewhere and not origin:
            # The cross-repository form has to name the repository holding the branch, and there is
            # nothing to name. Refused rather than sent empty: GitHub answers an empty `head_repo`
            # by looking for the branch on the TARGET, so the guess does not fail — it opens the
            # wrong change, or none, with no indication which happened.
            raise TargetRefused(
                "scm.unknown_origin",
                f"cannot open on {target}: nothing says which repository this checkout pushes to. "
                f"Set GITHUB_REPOSITORY, or give origin a GitHub URL.",
            )
        # Before the checkout, so a refusal leaves the working tree and the remote as it found them.
        target_base = self._target_default_branch(target) if elsewhere else ""

        # Conventional Commits: this commit and the pull-request title it becomes are created by a
        # workflow, so both must be one. A summary that already declares a type is kept as is.
        title = conventional_subject(title, workflow=workflow)

        # `base` decides both where the branch grows from and where the pull request points.
        # Without it every change targeted the default branch, which no backport can accept.
        # Two spellings, deliberately: the git start-point may need `origin/<base>` (a CI checkout
        # has the release line only as a remote-tracking ref), while `gh pr create --base` must
        # get the bare branch name the API knows.
        if base:
            self.local.git("checkout", "-B", branch, self.local.start_point(base), check=True)
        else:
            self.local.git("checkout", "-B", branch)
        self.local.apply(cs, workflow_id=workflow)

        trailers = {"In-Lockstep-Run": run_id}
        if ticket:
            trailers["Ticket"] = ticket
        self.local.commit(title, trailers=trailers)
        self.local.git("push", "-u", "origin", branch, check=True)

        rendered = change_body(body, trailers)
        # `title_line`, not `title`: the commit above may carry a body, a pull-request title may
        # not, and GitHub refuses one over 256 characters at the very end — after the branch is
        # pushed and the model is paid for.
        subject = title_line(title)
        if elsewhere:
            return self._open_across_repositories(
                target,
                origin,
                branch=branch,
                subject=subject,
                rendered=rendered,
                base=base or target_base,
                draft=draft,
                trailers=trailers,
            )
        args = ["pr", "create", "--title", subject, "--body", rendered, "--head", branch]
        if base:
            args += ["--base", base]
        if draft:
            # Opened not-yet-asking-for-review. An AI change starts here and is marked ready once
            # its tests pass, so a red or unverified change never lands in a human's review queue.
            args += ["--draft"]
        code, out, err = self._gh(*args)
        if code != 0:
            raise RuntimeError(f"could not open a pull request: {err.strip()}")
        url = out.strip().splitlines()[-1] if out.strip() else ""
        number = _number_from(url)
        return ChangeRequest(
            id=url or branch,
            url=url,
            branch=branch,
            title=subject,
            number=number,
            trailers=trailers,
            draft=draft,
        )

    def _open_across_repositories(
        self,
        target: str,
        origin: str,
        *,
        branch: str,
        subject: str,
        rendered: str,
        base: str,
        draft: bool,
        trailers: dict[str, str],
    ) -> ChangeRequest:
        """A fork's branch, opened on the repository it forked from, through the REST endpoint.

        `gh pr create` cannot express this. A fork sharing its parent's owner cannot be named as
        the head at all — `gh pr create` reports `Head sha can\'t be blank` — and the field that
        does express it, `head_repo`, is one the porcelain does not expose. Verified on
        in-lockstep/query-fork against in-lockstep/query-original: every `gh pr create` spelling
        fails and this one opens the request.

        `-F` for `draft`, not `-f`: `-f` sends the string "true", which the API reads as a string
        where it wants a boolean.
        """
        if not base:
            # The endpoint requires it and the target did not say what its default branch is. A
            # refusal rather than a guess at `main`: a repository whose default is something else
            # would get a change request opened against a branch that is not the one it merges to.
            raise RuntimeError(f"{target} did not report a default branch; pass base= explicitly")
        args = [
            "api",
            f"repos/{target}/pulls",
            "-f",
            f"title={subject}",
            "-f",
            f"body={rendered}",
            "-f",
            f"head={branch}",
            "-f",
            f"head_repo={origin}",
            "-f",
            f"base={base}",
        ]
        if draft:
            args += ["-F", "draft=true"]
        code, out, err = self._gh(*args)
        if code != 0:
            raise RuntimeError(f"could not open a pull request on {target}: {err.strip()}")
        data = json.loads(out) if out.strip() else {}
        data = data if isinstance(data, dict) else {}
        url = str(data.get("html_url") or "")
        number = data.get("number")
        return ChangeRequest(
            id=url or branch,
            url=url,
            branch=branch,
            title=subject,
            number=number if isinstance(number, int) else _number_from(url),
            trailers=trailers,
            draft=draft,
            # Carried, because `mark_ready` runs later and against this repository rather than the
            # one the checkout would infer.
            repo=target,
        )

    async def mark_ready(self, change: ChangeRequest) -> None:
        """Take the pull request out of draft — it is asking for human review now. Keyed on the
        number the request carries; a request with none (an open_change that returned only a branch)
        is left as it is rather than guessed at."""
        if change.number is None:
            return None
        # `--repo` only when the request says it was opened elsewhere: without a target this is the
        # call it has always been, and with one it must not be `gh`'s inference deciding which
        # repository's #17 goes out of draft.
        where = ["--repo", change.repo] if change.repo else []
        code, _out, err = self._gh("pr", "ready", str(change.number), *where)
        if code != 0:
            raise RuntimeError(f"could not mark PR #{change.number} ready: {err.strip()}")

    async def comment(self, target: int, body: str) -> None:
        self._gh("pr", "comment", str(target), *self._at(), "--body", body)

    async def upsert_comment(self, target: int, body: str, marker: str) -> None:
        """One sticky comment per marker: edit the framework's own prior comment in place rather
        than adding one per run, so a re-review updates the thread instead of burying it.

        `gh api` substitutes `{owner}`/`{repo}` from the checkout, so this needs no repository
        argument -- unless the adapter was narrowed with `for_repo`, when `_path` fills in that
        repository instead, because the number being commented on is that repository's. The marker
        rides at the end of the body, invisible in the rendered markdown, and is how the next run
        finds this comment among the thread's.
        """
        marked = f"{body}\n\n{marker}" if marker not in body else body
        # `--paginate`, because the framework's own comment is the newest one and the endpoint
        # returns the OLDEST thirty first: on a PR with more than thirty comments, a single page
        # never contains our marker, so every run would post a fresh duplicate — the exact
        # thread-burying this exists to prevent. `--paginate` merges every page into one array.
        existing = self._gh_json(
            "api", "--paginate", self._path(f"repos/{{owner}}/{{repo}}/issues/{target}/comments")
        )
        for comment in existing if isinstance(existing, list) else []:
            if marker in str(comment.get("body", "")):
                cid = comment.get("id")
                self._api_write(
                    "-X",
                    "PATCH",
                    self._path(f"repos/{{owner}}/{{repo}}/issues/comments/{cid}"),
                    "-f",
                    f"body={marked}",
                )
                return
        self._api_write(
            self._path(f"repos/{{owner}}/{{repo}}/issues/{target}/comments"), "-f", f"body={marked}"
        )

    async def mark_parked(self, number: int, run_id: str, resume: str, waiting_for: str) -> None:
        """The park, where the person will act (§13.1): the `lockstep:parked` label -- what a
        resume trampoline filters on -- and the park as fenced JSON in a sticky comment. A comment
        rather than the body: the pull request may be a person's, and its body is their text."""
        import json

        from ...core.human import PARKED_LABEL
        from ..report import marker

        # The label has to exist before it can be applied; `--force` makes creating it idempotent.
        self._gh(
            "label",
            "create",
            PARKED_LABEL,
            *self._at(),
            "--force",
            "--description",
            "an in-lockstep run is waiting here",
        )
        code, _out, err = self._gh("pr", "edit", str(number), *self._at(), "--add-label", PARKED_LABEL)
        if code != 0:
            raise RuntimeError(f"could not label #{number}: {err.strip()}")
        block = json.dumps(
            {"In-Lockstep-Run": run_id, "Resume": resume, "Waiting-For": waiting_for}, indent=2
        )
        body = (
            f"## in-lockstep is waiting here\n\n{waiting_for}. When that has happened, the run continues as "
            f"`{resume}`; locally, `in-lockstep resume --run {run_id} --as approved --by <you>`.\n\n"
            f"```json\n{block}\n```"
        )
        await self.upsert_comment(number, body, marker("parked"))

    async def clear_parked(self, number: int, run_id: str) -> None:
        """The person acted: the label comes off and the sticky comment says so, in place."""
        from ...core.human import PARKED_LABEL
        from ..report import marker

        self._gh("pr", "edit", str(number), *self._at(), "--remove-label", PARKED_LABEL)
        await self.upsert_comment(
            number, f"## in-lockstep resumed\n\nRun `{run_id}` continued.", marker("parked")
        )

    async def changes_for(self, ticket: str) -> tuple[ChangeRequest, ...]:
        """The OPEN pull requests this framework opened for `ticket`, newest first.

        Matched on the head branch, which `branch_for` wrote — never on the body or the title. A
        pull request that merely says "fixes #218" is somebody else's, and its conversation must
        not arrive as though a reviewer of *our* change had written it.

        Open only, and that is a control rather than an omission. A reviewer who wants the next run
        to read their feedback leaves the pull request open; closing it is how you say "start over,
        ignore that thread", and a merged one is a conversation that already concluded. Both are
        decisions a person makes with a button they already have.
        """
        raw = self._gh_json(
            "pr",
            "list",
            *self._at(),
            "--state",
            "open",
            "--limit",
            "60",
            "--json",
            "number,url,title,headRefName,isDraft",
        )
        rows = [r for r in (raw if isinstance(raw, list) else []) if isinstance(r, dict)]
        mine = [r for r in rows if is_run_branch_for(str(r.get("headRefName") or ""), ticket)]
        # Newest first, and by number rather than by the order `gh` happened to return: the most
        # recent attempt is the one a reviewer was looking at, and it should not be the one the
        # cap drops.
        mine.sort(key=lambda r: int(r.get("number") or 0), reverse=True)
        return tuple(
            ChangeRequest(
                id=str(r.get("url") or ""),
                url=str(r.get("url") or ""),
                branch=str(r.get("headRefName") or ""),
                title=str(r.get("title") or ""),
                number=int(r.get("number") or 0) or None,
                draft=bool(r.get("isDraft")),
            )
            for r in mine[:MAX_CHANGES_READ]
        )

    def open_changes_by_workflow(self, workflow: str, *, limit: int = 200) -> tuple[ChangeRequest, ...]:
        """Every OPEN pull request this framework has on `workflow`'s branches, newest first.

        Not on the `Scm` port, for the reason `delivery_rows` writes down: the local git host has
        no pull requests at all, and a caller asks for this with `getattr` and says why it could
        not when the answer is no.

        Not `changes_for` either, which looks close enough to reuse and is not. That one is keyed
        on a TICKET, and it stops at `MAX_CHANGES_READ` because its output goes into a prompt. A
        count capped at three is a ceiling that stops counting before it stops anything.

        Raises when the host returned exactly as many rows as it was asked for, because there may
        be more and this number is a ceiling: an undercount lets a run through, which is the single
        failure it exists to prevent. Refusing on a listing that might be short is the same posture
        as refusing when the host cannot list at all.
        """
        raw = self._gh_json(
            "pr", "list", "--state", "open", "--limit", str(limit),
            "--json", "number,url,title,headRefName,isDraft",
        )  # fmt: skip
        rows = raw if isinstance(raw, list) else []
        if len(rows) >= limit:
            raise RuntimeError(
                f"pull request listing truncated at {limit}; the count would be a floor, not a ceiling"
            )
        mine = [
            row
            for row in rows
            if isinstance(row, dict) and is_run_branch_of(str(row.get("headRefName") or ""), workflow)
        ]
        return tuple(
            ChangeRequest(
                id=str(r.get("url") or ""),
                url=str(r.get("url") or ""),
                branch=str(r.get("headRefName") or ""),
                title=str(r.get("title") or ""),
                number=int(r.get("number") or 0) or None,
                draft=bool(r.get("isDraft")),
            )
            for r in sorted(mine, key=lambda r: int(r.get("number") or 0), reverse=True)
        )

    def run_artifacts(self, name: str) -> tuple[RunArtifact, ...]:
        """Every artifact uploaded under `name`, expired ones included, as the sweep and
        `report --scm` need them: which run made each, and whether the bytes are still there.

        Not on the `Scm` port, for the reason `delivery_rows` gives: a forge capability the local
        git host cannot have. Expired artifacts are listed rather than filtered, because a record
        that expired before anyone absorbed it is a record that is gone, and the count has to say so.
        """
        code, out, err = self._gh(
            "api", "--paginate",
            f"repos/{{owner}}/{{repo}}/actions/artifacts?name={name}&per_page=100",
            "--jq", ".artifacts[] | {id, expired, created_at, run_id: .workflow_run.id}",
        )  # fmt: skip
        if code != 0:
            raise RuntimeError(f"gh api actions/artifacts failed: {err.strip()}")
        found: list[RunArtifact] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("id") is None:
                continue
            found.append(
                RunArtifact(
                    id=int(row["id"]),
                    run_id=str(row.get("run_id") or ""),
                    expired=bool(row.get("expired")),
                    created_at=str(row.get("created_at") or ""),
                )
            )
        return tuple(found)

    def download_artifact(self, artifact_id: int, into: Path) -> Path:
        """Fetch one artifact's zip and unpack it under `into`, which is returned.

        Bytes, not text: `_gh` decodes, and a zip is not a string. Extracted from memory rather
        than written to disk first, so the only files this leaves behind are the artifact's own.
        """
        import io
        import os
        import zipfile

        env = {**os.environ, "GH_TOKEN": self.token} if self.token else None
        result = subprocess.run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/artifacts/{artifact_id}/zip"],
            cwd=self.root,
            capture_output=True,
            timeout=300,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gh api actions/artifacts/{artifact_id}/zip failed: "
                f"{result.stderr.decode('utf-8', 'replace').strip()}"
            )
        with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
            archive.extractall(into)
        return into

    def delivery_rows(self, *, limit: int = 200) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Pull requests this framework opened, and issues it filed, as rows for `metrics.delivery`.

        Not on the `Scm` port, deliberately. It is a reporting convenience rather than something a
        workflow needs, and putting it on the port would oblige every host — including the local
        git one, which has no pull requests at all — to answer a question only a hosted forge can.
        `report --scm` asks for it with `getattr` and prints why it could not when the answer is no,
        which is what "degrade with a reason" means here.

        ALL states, unlike `changes_for`, which is deliberately open-only: this is asking what
        happened to the work, and a merged pull request is the answer that matters most.

        The branch filter is `is_run_branch`, so a pull request that merely mentions our work — or
        one a person opened from our branch's diff — is not counted as something this framework
        delivered. Issues are matched on the `ai-generated` label, which is the one this framework
        applies itself and which is write-gated on GitHub.
        """
        raw = self._gh_json(
            "pr", "list", "--state", "all", "--limit", str(limit),
            "--json", "number,headRefName,createdAt,mergedAt,closedAt",
        )  # fmt: skip
        pulls = [
            {
                "number": row.get("number"),
                "created_at": row.get("createdAt"),
                "merged_at": row.get("mergedAt"),
                "closed_at": row.get("closedAt"),
            }
            for row in (raw if isinstance(raw, list) else [])
            if isinstance(row, dict) and is_run_branch(str(row.get("headRefName") or ""))
        ]

        raw = self._gh_json(
            "issue", "list", "--state", "all", "--limit", str(limit),
            "--label", "ai-generated", "--json", "number,createdAt,closedAt,comments",
        )  # fmt: skip
        issues = []
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            # First response is the first comment by anybody. `gh` returns them oldest first; an
            # issue nobody answered carries None rather than a zero, so it lowers the denominator
            # instead of pulling the median toward "answered instantly".
            comments = row.get("comments") or []
            first = comments[0].get("createdAt") if comments and isinstance(comments[0], dict) else None
            issues.append(
                {
                    "number": row.get("number"),
                    "created_at": row.get("createdAt"),
                    "closed_at": row.get("closedAt"),
                    "first_response_at": first,
                }
            )
        return pulls, issues

    async def ticket_of(self, number: int) -> str | None:
        """The ticket a change request was opened for. `None` when `number` is not one at all.

        Three answers, because a caller has three different things to do about them. `None` means
        the number is an issue — GitHub draws issue and pull-request numbers from one sequence, so
        it is one or the other and never both, which is what makes resolving a comment's location
        unambiguous. A key means the pull request records the work it belongs to. An empty string
        means it IS a pull request and names no ticket — somebody's hand-opened branch — and the
        caller has to say so rather than treat the pull request's own number as a ticket and fail
        two steps later with a confusing error.

        The recorded trailers first, the branch second. The trailers are what `open_change` wrote
        and are exact; the branch is the fallback for a body somebody edited, and declines rather
        than guesses when its shape is ambiguous.
        """
        try:
            raw = self._gh_json("pr", "view", str(number), *self._at(), "--json", "body,headRefName")
        except RuntimeError:
            # `gh pr view` on an issue number fails, which is the answer rather than an error.
            return None
        data = raw if isinstance(raw, dict) else {}
        if not data:
            return None
        ticket = trailers_from(str(data.get("body") or "")).get("Ticket", "")
        return ticket or ticket_from_branch(str(data.get("headRefName") or ""))

    async def change_refs(self, number: int) -> tuple[str, str] | None:
        """The base branch and the head commit of one change request. `None` when it is not one.

        Needed because a comment event does not carry them. On `issue_comment` GitHub sets
        `GITHUB_BASE_REF` empty and `GITHUB_SHA` to the default branch's tip, so a workflow reacting
        to `/review` on a pull request has the number and nothing else — a review run from those
        variables would diff the default branch against itself and report a clean bill of health for
        a change it never read. That is the failure worth naming: not an error, an all-clear.

        `headRefOid` and not `headRefName`: a branch name resolves to whatever the branch points at
        when the job runs, and a review whose subject moved between the comment and the checkout has
        reviewed something nobody asked about. The base stays a NAME because that is what it is —
        the caller resolves it against whatever it actually fetched.

        Not on the `Scm` port, like `delivery_rows` and for the same reason: the local git host has
        no change requests to have refs for.
        """
        try:
            raw = self._gh_json("pr", "view", str(number), *self._at(), "--json", "baseRefName,headRefOid")
        except RuntimeError as e:
            # `gh pr view` on an issue number fails, which is the answer rather than an error —
            # the same three-way reading `ticket_of` above documents. Only that failure, though:
            # a `gh` that could not ask at all (no token, no network, a revoked credential) is
            # not an answer about the number, and folding it into `None` had the first real
            # `/review` here refused as "not a change request" when the job simply carried no
            # `GH_TOKEN` (#346). Absent is not zero: that one is raised with gh's own words.
            if _NOT_A_PULL_REQUEST.search(str(e)):
                return None
            raise
        data = raw if isinstance(raw, dict) else {}
        base, head = str(data.get("baseRefName") or ""), str(data.get("headRefOid") or "")
        return (base, head) if base and head else None

    async def merged_change(self, number: int) -> tuple[str, str] | None:
        """The merge commit and the moment one pull request was merged, or `None` when it was
        not -- still open, closed unmerged, or not a pull request at all. `report --around #N`
        reads it. Not on the `Scm` port, like `change_refs` and for the same reason."""
        try:
            raw = self._gh_json(
                "pr", "view", str(number), *self._at(), "--json", "state,mergedAt,mergeCommit"
            )
        except RuntimeError as e:
            if _NOT_A_PULL_REQUEST.search(str(e)):
                return None
            raise
        data = raw if isinstance(raw, dict) else {}
        if str(data.get("state") or "") != "MERGED":
            return None
        commit = data.get("mergeCommit")
        sha = str(commit.get("oid") or "") if isinstance(commit, dict) else ""
        merged_at = str(data.get("mergedAt") or "")
        return (sha, merged_at) if sha and merged_at else None

    async def remarks(self, number: int) -> tuple[Remark, ...]:
        """Everything said on one pull request: the thread, the review verdicts, the line notes.

        Two calls because GitHub keeps them in two places, and the second is worth the extra
        request: `gh pr view` returns conversation comments and review summaries but not the notes
        pinned to a file and a line, which are the most actionable thing a reviewer writes.

        The framework's own sticky review comment is included rather than filtered out. It is what
        the human was reading when they replied, so dropping it leaves their "the second one is
        right" pointing at nothing. Its invisible marker is stripped, because a marker is noise in
        a prompt and meaning only to `upsert_comment`.
        """
        out: list[Remark] = []
        view = self._gh_json("pr", "view", str(number), *self._at(), "--json", "comments,reviews")
        data = view if isinstance(view, dict) else {}

        for c in (data.get("comments") or [])[:MAX_REMARKS]:
            out.append(Remark(author=_login(c.get("author")), body=_clean(c.get("body")), kind="comment"))
        for r in (data.get("reviews") or [])[:MAX_REMARKS]:
            state = str(r.get("state") or "")
            body = _clean(r.get("body"))
            # A bare COMMENTED review with no body is the envelope around line notes and says
            # nothing itself; an APPROVED or CHANGES_REQUESTED with no body is a verdict and does.
            if body or state.upper() in ("APPROVED", "CHANGES_REQUESTED"):
                out.append(Remark(author=_login(r.get("author")), body=body, kind="review", state=state))

        # `per_page` rather than `--paginate`: one page is the cap, and a pull request with three
        # hundred line notes should cost one request and arrive truncated, not cost thirty and
        # arrive too big for the curator anyway.
        try:
            notes = self._gh_json(
                "api", self._path(f"repos/{{owner}}/{{repo}}/pulls/{number}/comments?per_page={MAX_REMARKS}")
            )
        except RuntimeError:
            # A token without `pull-requests: read` reaches the two above through the issues
            # endpoint and fails here. Returning what was gathered beats losing all of it.
            notes = None
        for n in notes if isinstance(notes, list) else []:
            if not isinstance(n, dict):
                continue
            out.append(
                Remark(
                    author=_login(n.get("user")),
                    body=_clean(n.get("body")),
                    kind="line",
                    path=str(n.get("path") or ""),
                    line=_line_of(n),
                )
            )
        return tuple(out)

    def _api_write(self, *args: str) -> None:
        code, _out, err = self._gh("api", *args)
        if code != 0:
            raise RuntimeError(f"could not post PR comment: {err.strip()}")


def owner_repo_from_remote(url: str) -> str:
    """The `owner/repo` in a GitHub remote URL, or empty when the shape is unfamiliar.

    Both spellings git actually writes: `git@github.com:owner/repo.git` and
    `https://github.com/owner/repo.git`. Empty rather than guessed, the rule GitLab's
    `project_from_remote` already follows — a wrong slug here names a real repository that is not
    this one.
    """
    url = url.strip().removesuffix(".git")
    if not url:
        return ""
    if "://" in url:
        _host, _, path = url.split("://", 1)[1].partition("/")
        return path.strip("/")
    # scp-like: everything after the colon. A local path (`/tmp/origin`) has no colon and no
    # scheme, and falls through to empty, which is the honest answer for a remote that is not a
    # host at all.
    if ":" in url:
        return url.split(":", 1)[1].strip("/")
    return ""


def _number_from(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _login(actor: Any) -> str:
    """A `@login` from either shape GitHub uses — `author` from `gh`, `user` from the REST API."""
    login = str((actor or {}).get("login", "")) if isinstance(actor, dict) else ""
    return f"@{login}" if login else ""


def _clean(body: Any) -> str:
    """A comment body as a prompt should see it: capped, and without the framework's own marker.

    The marker is an HTML comment, invisible where a human reads it and meaningless to a model —
    it exists so `upsert_comment` can find its own comment again, and carrying it into a prompt
    would be teaching the model a token it must never emit.
    """
    import re

    text = re.sub(r"<!--\s*in-lockstep:[^>]*-->", "", str(body or "")).strip()
    return text[:MAX_REMARK_CHARS]


def _line_of(note: dict[str, Any]) -> int | None:
    """Where a line note is pinned. `line` is null once the comment goes outdated, and
    `original_line` is where it was written — which is the one a reader wants either way."""
    for key in ("line", "original_line"):
        value = note.get(key)
        if isinstance(value, int):
            return value
    return None
