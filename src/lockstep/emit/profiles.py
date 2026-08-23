"""Profile lowering: values become environment variables, `${REF}`s become secrets or vars."""

from __future__ import annotations

import re

from ..errors import EmitError
from ..spec.model import Profile
from ..util.text import env_key

ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def prefix_for(profile: Profile) -> str:
    """`ao-local` exports as `AO_*`, matching the local runtime's contract."""
    return env_key(profile.name.split("-")[0])


def secret_ref(name: str) -> str:
    return "${{ secrets." + name + " }}"


def var_ref(name: str) -> str:
    return "${{ vars." + name + " }}"


def resolve_value(value: str, profile: Profile, *, location: str) -> str:
    """A profile value is either a literal or a `${NAME}` reference to a declared secret or var."""
    match = ENV_REF.match(value.strip())
    if not match:
        return value
    name = match.group(1)
    if name in profile.github.secrets:
        return secret_ref(name)
    if name in profile.github.vars:
        return var_ref(name)
    raise EmitError(
        f"profile value references {name!r}, which is not declared as a secret or var",
        location=location,
        hint=(
            f"add `{name}` to github.secrets or github.vars in profiles/{profile.name}.md — "
            "the compiler will not guess where a credential lives"
        ),
    )


def env_block(profile: Profile) -> dict[str, str]:
    """Every profile key under both prefixes, so unmodified scripts keep working in CI.

    The local runtime exports `{PREFIX}_{KEY}` and `PROFILE_{KEY}` to script subprocesses; jobs that
    run user scripts get the same contract here.
    """
    prefix = prefix_for(profile)
    location = profile.src.rel if profile.src else profile.name
    env: dict[str, str] = {}
    for key, raw in profile.values.items():
        resolved = resolve_value(raw, profile, location=location)
        env[f"{prefix}_{env_key(key)}"] = resolved
        env[f"PROFILE_{env_key(key)}"] = resolved
    return env


def named_secrets(profile: Profile, env: dict[str, str]) -> list[str]:
    """Which declared secrets an env block actually consumes — the never-inherit rule needs this."""
    used = [name for name in profile.github.secrets if secret_ref(name) in env.values()]
    return sorted(set(used))
