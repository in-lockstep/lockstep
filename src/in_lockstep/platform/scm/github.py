"""GitHub. Contains a GitLocal rather than replacing it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ...core.changes import ChangeGuard
from ...core.types import ChangeSet
from .base import ChangeRequest, Diff, GitLocal, Ref, branch_for


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
        branch = branch_for(workflow or "change", run_id or "run")
        # Refused at the framework rather than relying on the token's scope, because the token is
        # ambient and can write any branch.
        self.local.assert_run_scoped(branch)

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

        rendered = _body(body, trailers)
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

    def _api_write(self, *args: str) -> None:
        code, _out, err = self._gh("api", *args)
        if code != 0:
            raise RuntimeError(f"could not post PR comment: {err.strip()}")


def _body(body: str, trailers: dict[str, str]) -> str:
    """The rendered half a human reads, plus a machine-readable block.

    Both, deliberately: a reviewer should not have to parse JSON, and a later run should not have
    to parse prose.
    """
    block = json.dumps(trailers, indent=2, sort_keys=True)
    return f"{body}\n\n<details><summary>in-lockstep</summary>\n\n```json\n{block}\n```\n\n</details>"


def _number_from(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None
