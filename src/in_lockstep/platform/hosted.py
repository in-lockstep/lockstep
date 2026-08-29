"""The host adapters a repository implies, so a scaffold does not hardcode GitHub.

The same drop-in rule `adapters/detected.py` applies to test runners: what detection found decides
the default, and an explicit `lockstep.bind(...)` wins over it. Before this existed the scaffold
module blocks bound `GitHubScm()` by name, which made every scaffolded GitLab repository talk to
the wrong API until someone edited the file — the exact wrong-default-that-runs the detection
work exists to prevent.

Three signals, strongest first: the CI environment actually running (its env vars name the host
outright), then the CI files in the tree, then the origin remote's own URL. A repository none of
them can place gets GitHub — the shipped default since the first release — rather than a refusal,
because a laptop clone with no remote still deserves a working scaffold.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import ci


def detect_host(root: str | Path = ".") -> str:
    """`"github"`, `"gitlab"`, or `""` when nothing places the repository."""
    env = ci.detect()
    if env is not None and env.host:
        return env.host
    path = Path(root)
    github_files = (path / ".github" / "workflows").is_dir()
    gitlab_files = (path / ".gitlab-ci.yml").exists()
    # One host's CI files alone are decisive. Both present — a migrated repository with a
    # vestigial workflows directory — is ambiguous, and the origin remote breaks the tie: where
    # the code is pushed is where the change requests go.
    if github_files != gitlab_files:
        return "github" if github_files else "gitlab"
    url = _origin(path).lower()
    if "gitlab" in url:
        return "gitlab"
    if "github" in url:
        return "github"
    return ""


def _origin(path: Path) -> str:
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def hosted_scm(root: str | Path = ".", *, guard: Any = None) -> Any:
    """The `Scm` for the detected host. GitHub when nothing places the repository — the shipped
    default — so a scaffold module written on one host runs unedited on the other."""
    from .scm import GitHubScm, GitLabScm

    if detect_host(root) == "gitlab":
        return GitLabScm(root, guard=guard)
    return GitHubScm(root, guard=guard)


def hosted_tickets(root: str | Path = ".") -> Any:
    """The `TicketSource` for the detected host.

    On GitLab CI the ambient `CI_SERVER_URL`/`CI_PROJECT_PATH` win — they are the documented
    zero-argument defaults, and a runner's origin remote carries a `gitlab-ci-token:` credential
    that must not become an API base URL. Only a laptop clone, where neither variable exists,
    falls back to reading the origin remote the way `GitLabScm` does.
    """
    import os

    from .scm.gitlab import project_from_remote, server_from_remote
    from .tickets import GitHubIssues, GitLabIssues

    if detect_host(root) == "gitlab":
        server = os.environ.get("CI_SERVER_URL", "")
        project = os.environ.get("CI_PROJECT_PATH", "")
        if not (server and project):
            url = _origin(Path(root))
            server = server or server_from_remote(url)
            project = project or project_from_remote(url)
        return GitLabIssues(base_url=server, project=project)
    return GitHubIssues(root=Path(root))
