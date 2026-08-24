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
    # Opt out of TLS verification for this profile only. A staging environment behind a self-signed
    # certificate is a real case; making it the unconditional default was not. Declared per profile
    # so a production profile cannot inherit a convenience somebody needed once.
    "insecure_tls",
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

    # Two things the runtime cannot know about your application, declared rather than guessed: how
    # its sign-in page works, and what to send when it rejects a field as required. Unset means the
    # behaviour is off, which is the honest default. See docs/layers.md.
    login_recipe: str = ""
    recovery_rules: str = ""

    profile_url: str = ""
    profile_api_url: str = ""
    profile_api_prefix: str = ""
    profile_username: str = ""
    profile_password: str = ""
    # No default scheme and no default login path. The runtime cannot know how your application
    # authenticates, and a guess that happens to be one application's answer is worse than an error:
    # it fails at the target, not here. The profile declares both. See docs/layers.md.
    profile_auth_method: str = "none"
    profile_auth_login_path: str = ""
    # Read as a string like every other profile value; "1"/"true"/"yes" turn verification off.
    profile_insecure_tls: str = ""

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
            "login_recipe": os.environ.get("LOGIN_RECIPE", ""),
            "recovery_rules": os.environ.get("RECOVERY_RULES", ""),
        }
        for key in PROFILE_KEYS:
            raw = os.environ.get(f"PROFILE_{key.upper()}")
            if raw:
                values[f"profile_{key}"] = raw
        values.update({key: value for key, value in overrides.items() if value not in (None, "")})
        return cls(**values)  # type: ignore[arg-type]
