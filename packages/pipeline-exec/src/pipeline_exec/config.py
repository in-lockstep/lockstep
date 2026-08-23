"""Runtime configuration for the executors.

The framework's `Config` reads a large surface of environment variables because it also configures
LLM providers, Jira, and the orchestrator. None of that belongs here: this package only runs tests
and renders reports. What it needs is the profile, and the compiler already exports exactly that to
every job it generates, under `PROFILE_*` — so that export is the contract this reads.
`tests/test_contract.py` in the repo root asserts the two agree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# The profile keys the compiler exports as `PROFILE_<KEY>` and that the executors consume.
PROFILE_KEYS = (
    "url",
    "api_url",
    "api_prefix",
    "username",
    "password",
    "auth_method",
    "auth_login_path",
)


@dataclass
class ExecConfig:
    """Everything the executors need, and nothing else."""

    output_dir: str = "outputs"
    agent_count: int = 3
    ui_wait_timeout: int = 30000

    # In a compiled pipeline the generated test scripts are reviewed and committed to the repo, so
    # these default to repo-root paths rather than to somewhere under outputs/.
    scripts_dir: str = "test-scripts"
    tags_file: str = ".env-tests"

    profile_url: str = ""
    profile_api_url: str = ""
    profile_api_prefix: str = ""
    profile_username: str = ""
    profile_password: str = ""
    profile_auth_method: str = "jwt"
    profile_auth_login_path: str = "/api/v1/auth/login"

    # Report templates ship inside this package rather than being located relative to a checkout.
    framework_dir: Path = field(default_factory=lambda: Path(__file__).parent)

    @classmethod
    def from_env(cls, **overrides: object) -> ExecConfig:
        """Build from the `PROFILE_*` environment the compiler emits, with CLI overrides on top."""
        values: dict[str, object] = {
            "output_dir": os.environ.get("OUTPUT_DIR", "outputs"),
            "ui_wait_timeout": int(os.environ.get("UI_WAIT_TIMEOUT", "30000")),
            "scripts_dir": os.environ.get("SCRIPTS_DIR", "test-scripts"),
            "tags_file": os.environ.get("TAGS_FILE", ".env-tests"),
        }
        for key in PROFILE_KEYS:
            raw = os.environ.get(f"PROFILE_{key.upper()}")
            if raw:
                values[f"profile_{key}"] = raw
        values.update({key: value for key, value in overrides.items() if value not in (None, "")})
        return cls(**values)  # type: ignore[arg-type]
