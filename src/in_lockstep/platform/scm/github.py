"""GitHub. Contains a GitLocal rather than replacing it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ...core.changes import ChangeGuard
from ...core.types import ChangeSet
from .base import (
    MAX_CHANGES_READ,
    MAX_REMARK_CHARS,
    MAX_REMARKS,
    ChangeRequest,
    Diff,
    GitLocal,
    Ref,
    Remark,
    branch_for,
    change_body,
    conventional_subject,
    is_run_branch_for,
)


class GitHubScm:
    def __init__(
        self,
        root: str | Path = ".",
        *,
        guard: ChangeGuard | None = None,
        token: str = "",
    ) -> None:
        # Host features layer over plain git; they do not replace it.
        self.local = GitLocal(root, guard=guard)
        self.root = Path(root)
        self.token = token

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
    ) -> ChangeRequest:
        branch = branch_for(workflow or "change", run_id or "run", ticket=ticket)
        # Refused at the framework rather than relying on the token's scope, because the token is
        # ambient and can write any branch.
        self.local.assert_run_scoped(branch)

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
        args = ["pr", "create", "--title", title, "--body", rendered, "--head", branch]
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
            title=title,
            number=number,
            trailers=trailers,
            draft=draft,
        )

    async def mark_ready(self, change: ChangeRequest) -> None:
        """Take the pull request out of draft — it is asking for human review now. Keyed on the
        number the request carries; a request with none (an open_change that returned only a branch)
        is left as it is rather than guessed at."""
        if change.number is None:
            return None
        code, _out, err = self._gh("pr", "ready", str(change.number))
        if code != 0:
            raise RuntimeError(f"could not mark PR #{change.number} ready: {err.strip()}")

    async def comment(self, target: int, body: str) -> None:
        self._gh("pr", "comment", str(target), "--body", body)

    async def upsert_comment(self, target: int, body: str, marker: str) -> None:
        """One sticky comment per marker: edit the framework's own prior comment in place rather
        than adding one per run, so a re-review updates the thread instead of burying it.

        `gh api` substitutes `{owner}`/`{repo}` from the checkout, so this needs no repository
        argument. The marker rides at the end of the body, invisible in the rendered markdown, and
        is how the next run finds this comment among the thread's.
        """
        marked = f"{body}\n\n{marker}" if marker not in body else body
        # `--paginate`, because the framework's own comment is the newest one and the endpoint
        # returns the OLDEST thirty first: on a PR with more than thirty comments, a single page
        # never contains our marker, so every run would post a fresh duplicate — the exact
        # thread-burying this exists to prevent. `--paginate` merges every page into one array.
        existing = self._gh_json("api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{target}/comments")
        for comment in existing if isinstance(existing, list) else []:
            if marker in str(comment.get("body", "")):
                cid = comment.get("id")
                self._api_write(
                    "-X", "PATCH", f"repos/{{owner}}/{{repo}}/issues/comments/{cid}", "-f", f"body={marked}"
                )
                return
        self._api_write(f"repos/{{owner}}/{{repo}}/issues/{target}/comments", "-f", f"body={marked}")

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
        view = self._gh_json("pr", "view", str(number), "--json", "comments,reviews")
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
                "api", f"repos/{{owner}}/{{repo}}/pulls/{number}/comments?per_page={MAX_REMARKS}"
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
