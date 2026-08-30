"""GitLab, over the merge-request REST API. Contains a GitLocal rather than replacing it.

The second host implementation is what proves the `Scm` protocol host-neutral: the branch
discipline, the trailers, the Conventional-Commit subject and the draft-until-green flow are all
inherited unchanged, and only the host API differs. Where GitHub speaks through the `gh` CLI,
this speaks HTTP through `httpx` — already a core dependency — because there is no `glab`
equivalent an adopter is guaranteed to have installed, and the client is injectable the same way
`JiraSource`'s is, so tests exercise the adapter without a network.

GitLab spells "draft" as a title prefix rather than a field: a merge request whose title starts
with `Draft:` is not yet asking for review, and removing the prefix is what `gh pr ready` is to
GitHub. `mark_ready` therefore rewrites the title back to the Conventional-Commit subject the
change request carries — which is also why `ChangeRequest.title` never includes the prefix.

The token travels in the `PRIVATE-TOKEN` header, never in a URL or an error string; errors carry
GitLab's own JSON body, which names the field or permission that was wrong, not the credential.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ...core.changes import ChangeGuard
from ...core.types import ChangeSet
from .base import ChangeRequest, Diff, GitLocal, Ref, branch_for, change_body, conventional_subject

#: One sticky comment lives among at most this many pages of notes. A bound, so a misbehaving
#: server that repeats a next-page header cannot loop this forever; 20 pages of 100 is far past
#: any thread a human still reads.
_MAX_NOTE_PAGES = 20


def project_from_remote(url: str) -> str:
    """The `group/project` path in a git remote URL, or empty when the shape is unfamiliar.

    Both spellings git actually uses: `git@gitlab.example.com:group/project.git` (scp-like) and
    `https://gitlab.example.com/group/project.git`. Empty rather than guessed wrong, the same rule
    Jira's `_browse_url` follows.
    """
    url = url.strip()
    if not url:
        return ""
    if url.endswith(".git"):
        url = url[: -len(".git")]
    if "://" in url:
        rest = url.split("://", 1)[1]
        _host, _, path = rest.partition("/")
        return path.strip("/")
    if ":" in url:
        return url.split(":", 1)[1].strip("/")
    return ""


def server_from_remote(url: str) -> str:
    """The https origin of a remote URL — `https://gitlab.example.com` — or empty.

    Userinfo is always stripped: a CI checkout's origin is
    `https://gitlab-ci-token:<token>@host/...`, and a credential must never ride into the API
    base URL — the token this adapter sends travels in the `PRIVATE-TOKEN` header, or not at
    all. An `ssh://` remote maps to https with its port dropped (an ssh port is not an API
    port), the same translation the scp-like `git@host:` spelling gets; an http(s) remote keeps
    its scheme and port, which a self-hosted instance may genuinely need.
    """
    url = url.strip()
    if "://" in url:
        scheme, _, rest = url.partition("://")
        host = rest.split("/", 1)[0]
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        if not host:
            return ""
        if scheme in ("http", "https"):
            return f"{scheme}://{host}"
        return f"https://{host.split(':', 1)[0]}"
    if url.startswith("git@") and ":" in url:
        host = url[len("git@") :].split(":", 1)[0]
        return f"https://{host}" if host else ""
    return ""


class GitLabScm:
    def __init__(
        self,
        root: str | Path = ".",
        *,
        guard: ChangeGuard | None = None,
        token: str = "",
        base_url: str = "",
        project: str = "",
        timeout: float = 30.0,
        client: Any = None,
    ) -> None:
        # Host features layer over plain git; they do not replace it.
        self.local = GitLocal(root, guard=guard)
        self.root = Path(root)
        self.token = token or os.environ.get("GITLAB_TOKEN", "")
        # On GitLab CI both of these are ambient; on a laptop they come from the origin remote —
        # resolved lazily, so constructing the adapter never runs git.
        self._base_url = base_url or os.environ.get("CI_SERVER_URL", "")
        self._project = project or os.environ.get("CI_PROJECT_PATH", "")
        self.timeout = timeout
        #: Injectable for tests, so the adapter is exercised without a network.
        self.client = client

    def diff(self, base: Ref, head: Ref = "HEAD") -> Diff:
        return self.local.diff(base, head)

    # -- transport --------------------------------------------------------------------

    def _remote(self) -> str:
        return self.local.git("config", "--get", "remote.origin.url").strip()

    def _http(self) -> Any:
        if self.client is not None:
            return self.client
        import httpx

        base = self._base_url or server_from_remote(self._remote())
        if not base:
            raise RuntimeError(
                "no GitLab server to talk to: pass base_url=, set CI_SERVER_URL, "
                "or add an origin remote that names the host"
            )
        headers = {"PRIVATE-TOKEN": self.token} if self.token else {}
        self.client = httpx.Client(
            base_url=f"{base.rstrip('/')}/api/v4", headers=headers, timeout=self.timeout
        )
        return self.client

    def _request(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        import httpx

        try:
            response = self._http().request(method, path, json=json, params=params)
        except httpx.HTTPError as e:
            # A transport failure is not a status error and would otherwise escape as a raw
            # traceback; RuntimeError is what every caller already handles.
            raise RuntimeError(f"gitlab {method} {path}: {type(e).__name__}: {e}") from e
        if response.status_code >= 400:
            # GitLab's body names the field or permission that failed; the token is in the request
            # header, not here, so this is safe to surface.
            raise RuntimeError(f"gitlab {method} {path} -> {response.status_code}: {response.text[:500]}")
        return response.json() if response.content else None

    def _project_path(self) -> str:
        project = self._project or project_from_remote(self._remote())
        if not project:
            raise RuntimeError(
                "no GitLab project to address: pass project=, set CI_PROJECT_PATH, "
                "or add an origin remote that names one"
            )
        # URL-encoded, because the API addresses a project by its path with the slashes escaped.
        return quote(project, safe="")

    def _default_branch(self) -> str:
        data = self._request("GET", f"/projects/{self._project_path()}") or {}
        branch = str(data.get("default_branch") or "")
        if not branch:
            raise RuntimeError("gitlab did not report a default branch; pass base= explicitly")
        return branch

    # -- the Scm protocol -------------------------------------------------------------

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
        # ambient and can write any branch — same rule as every other host.
        self.local.assert_run_scoped(branch)

        # Conventional Commits: this commit and the merge-request title it becomes are created by
        # a workflow, so both must be one.
        title = conventional_subject(title, workflow=workflow)

        # Two spellings of `base`, deliberately: the git start-point may need `origin/<base>` (a
        # CI checkout has the release line only as a remote-tracking ref), while `target_branch`
        # must be the bare name the API knows.
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

        data = (
            self._request(
                "POST",
                f"/projects/{self._project_path()}/merge_requests",
                json={
                    "source_branch": branch,
                    # A GitLab merge request must name its target; GitHub defaults one. Empty
                    # `base` therefore asks the project which branch is its default.
                    "target_branch": base or self._default_branch(),
                    # Draft is a title prefix here, not a field: opened not-yet-asking-for-review,
                    # and `mark_ready` strips it once the tests pass.
                    "title": f"Draft: {title}" if draft else title,
                    "description": change_body(body, trailers),
                },
            )
            or {}
        )
        url = str(data.get("web_url") or "")
        iid = data.get("iid")
        return ChangeRequest(
            id=url or branch,
            url=url,
            branch=branch,
            title=title,
            number=int(iid) if isinstance(iid, int) else None,
            trailers=trailers,
            draft=draft,
        )

    async def mark_ready(self, change: ChangeRequest) -> None:
        """Strip the `Draft:` prefix — the merge request is asking for human review now. Keyed on
        the iid the request carries; a request with none is left as it is rather than guessed at.
        The title written back is the Conventional-Commit subject the request holds, which is the
        title without the prefix by construction."""
        if change.number is None:
            return None
        self._request(
            "PUT",
            f"/projects/{self._project_path()}/merge_requests/{change.number}",
            json={"title": change.title},
        )

    # -- comments (the same duck-typed extras GitHubScm carries) ----------------------

    async def comment(self, target: int, body: str) -> None:
        try:
            self._request(
                "POST", f"/projects/{self._project_path()}/merge_requests/{target}/notes", json={"body": body}
            )
        except RuntimeError as e:
            # Best-effort, like the GitHub adapter: a failed comment must not sink a run that
            # produced a result, but it is logged rather than swallowed whole.
            print(f"comment    could not post to !{target}: {e}")

    async def upsert_comment(self, target: int, body: str, marker: str) -> None:
        """One sticky comment per marker: edit the framework's own prior note in place rather than
        adding one per run, so a re-review updates the thread instead of burying it. Paginated for
        the same reason `gh api --paginate` is on GitHub: the framework's note may not be on the
        first page of a long thread, and missing it would post the duplicate this exists to
        prevent."""
        marked = f"{body}\n\n{marker}" if marker not in body else body
        path = f"/projects/{self._project_path()}/merge_requests/{target}/notes"
        for note in self._notes(path):
            if marker in str(note.get("body", "")):
                self._request("PUT", f"{path}/{note.get('id')}", json={"body": marked})
                return
        self._request("POST", path, json={"body": marked})

    def _notes(self, path: str) -> list[dict[str, Any]]:
        """Every note on the thread, following GitLab's `x-next-page` header."""
        import httpx

        out: list[dict[str, Any]] = []
        page = "1"
        for _ in range(_MAX_NOTE_PAGES):
            try:
                response = self._http().request("GET", path, params={"per_page": 100, "page": page})
            except httpx.HTTPError as e:
                raise RuntimeError(f"gitlab GET {path}: {type(e).__name__}: {e}") from e
            if response.status_code >= 400:
                raise RuntimeError(f"gitlab GET {path} -> {response.status_code}: {response.text[:500]}")
            rows = response.json() if response.content else []
            out.extend(row for row in rows if isinstance(row, dict))
            page = str(response.headers.get("x-next-page", "") or "")
            if not page:
                break
        return out
