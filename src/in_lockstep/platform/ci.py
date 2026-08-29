"""Which CI system this is, if any.

A default rather than magic: detection is overridable, and constructing the environment
explicitly is always available — which is what tests do.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CiEnvironment:
    host: str  # "github" | "gitlab" | ""
    repo: str = ""
    ref: str = ""
    base_ref: str = ""
    pr_number: int | None = None
    actor: str = ""
    run_id: str = ""
    oidc_available: bool = False
    event: str = ""

    @property
    def reviewing(self) -> bool:
        """Whether the checked-out ref is the thing under review.

        Which decides where configuration is loaded from — the change under review must not be
        allowed to supply the file that constrains reviewing it.
        """
        return self.event in ("pull_request", "pull_request_target", "merge_request_event")


def detect() -> CiEnvironment | None:
    if os.environ.get("GITHUB_ACTIONS"):
        number = os.environ.get("GITHUB_REF_NAME", "").split("/")[0]
        return CiEnvironment(
            host="github",
            repo=os.environ.get("GITHUB_REPOSITORY", ""),
            ref=os.environ.get("GITHUB_SHA", ""),
            base_ref=os.environ.get("GITHUB_BASE_REF", ""),
            pr_number=int(number) if number.isdigit() else None,
            actor=os.environ.get("GITHUB_ACTOR", ""),
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            # Federated credentials are preferred over long-lived ones, and this is how a run
            # knows the option exists.
            oidc_available=bool(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")),
            event=os.environ.get("GITHUB_EVENT_NAME", ""),
        )
    if os.environ.get("GITLAB_CI"):
        # The merge-request iid, so `review --comment` can find its thread without --pr — the
        # GitHub branch derives the same number from GITHUB_REF_NAME. Absent outside MR pipelines.
        iid = os.environ.get("CI_MERGE_REQUEST_IID", "")
        return CiEnvironment(
            host="gitlab",
            repo=os.environ.get("CI_PROJECT_PATH", ""),
            ref=os.environ.get("CI_COMMIT_SHA", ""),
            base_ref=os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", ""),
            pr_number=int(iid) if iid.isdigit() else None,
            actor=os.environ.get("GITLAB_USER_LOGIN", ""),
            run_id=os.environ.get("CI_PIPELINE_ID", ""),
            oidc_available=bool(os.environ.get("CI_JOB_JWT_V2")),
            event=os.environ.get("CI_PIPELINE_SOURCE", ""),
        )
    return None
