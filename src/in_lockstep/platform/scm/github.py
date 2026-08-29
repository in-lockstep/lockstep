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
    ) -> ChangeRequest:
        branch = branch_for(workflow or "change", run_id or "run")
        # Refused at the framework rather than relying on the token's scope, because the token is
        # ambient and can write any branch.
        self.local.assert_run_scoped(branch)

        self.local.git("checkout", "-B", branch)
        self.local.apply(cs, workflow_id=workflow)

        trailers = {"In-Lockstep-Run": run_id}
        if ticket:
            trailers["Ticket"] = ticket
        self.local.commit(title, trailers=trailers)
        self.local.git("push", "-u", "origin", branch, check=True)

        rendered = _body(body, trailers)
        code, out, err = self._gh("pr", "create", "--title", title, "--body", rendered, "--head", branch)
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
        )

    async def comment(self, target: int, body: str) -> None:
        self._gh("pr", "comment", str(target), "--body", body)


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
