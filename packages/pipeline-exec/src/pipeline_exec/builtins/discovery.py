"""Record what a target API actually offers.

The version this replaces probed thirteen hardcoded endpoints — `/api/v1/policies`,
`/api/v1/identity_providers` and friends — and, when one answered 422, invented values for the
missing fields from a table of one application's domain model. That is a context compiled into a
binary: point it at any other service and it discovers nothing, slowly.

What survives is the mechanism. The surface comes from the pipeline, one of two ways:

- the target publishes an OpenAPI document, in which case there is nothing to guess; or
- the pipeline declares the paths it cares about, in a file that lives with its contexts.

Neither guesses, and both fail loudly when given nothing — an empty result that looks like a working
discovery is the outcome worth ruling out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..config import ExecConfig
from ..errors import ExecError
from ..logging import log


@dataclass
class Surface:
    """What this pipeline wants to know about, declared rather than assumed."""

    openapi: str = ""
    paths: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Surface:
        if not path.is_file():
            raise ExecError(f"no API surface declared at {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ExecError(f"{path} should be a mapping with `openapi:` and/or `paths:`")
        surface = cls(
            openapi=str(raw.get("openapi") or ""),
            paths=[str(entry) for entry in (raw.get("paths") or [])],
        )
        if not surface.openapi and not surface.paths:
            raise ExecError(f"{path} declares neither `openapi:` nor `paths:`; one is required")
        return surface


def _auth_headers(config: ExecConfig, token: str) -> dict[str, str]:
    if token:
        return {"Authorization": f"Bearer {token}"}
    if config.profile_auth_method in ("", "none"):
        return {}
    if config.profile_auth_method == "basic":
        import base64

        pair = f"{config.profile_username}:{config.profile_password}".encode()
        return {"Authorization": f"Basic {base64.b64encode(pair).decode()}"}
    raise ExecError(
        f"auth method {config.profile_auth_method!r} needs a token; pass --token, or set the "
        "profile's auth_method to `none` or `basic`"
    )


async def _fetch_openapi(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> dict[str, Any]:
    """A published document beats any amount of probing, so try it first and take it whole."""
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    document: dict[str, Any] = response.json()
    paths = document.get("paths") or {}
    log.info(f"  Read {len(paths)} path(s) from {url}")
    return document


async def _probe(
    client: httpx.AsyncClient, base: str, paths: list[str], headers: dict[str, str]
) -> dict[str, Any]:
    """GET each declared path and record the shape of what came back.

    Reads only. The version this replaces POSTed invented payloads at the target to reverse-engineer
    its required fields, which is a mutation of somebody's environment performed by a step named
    `discover`. A pipeline that wants that behaviour can write an extension that says so.
    """
    found: dict[str, Any] = {}
    for path in paths:
        url = f"{base.rstrip('/')}/{path.lstrip('/')}"
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as error:
            log.warning(f"  {path}: {type(error).__name__}")
            continue
        entry: dict[str, Any] = {"path": path, "status": response.status_code}
        if response.is_success:
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    entry["sample"] = response.json()
                except ValueError:
                    entry["sample"] = None
            entry["content_type"] = content_type
        found[path] = entry
    reachable = sum(1 for entry in found.values() if 200 <= int(entry["status"]) < 300)
    log.info(f"  Probed {len(found)} path(s); {reachable} answered")
    return found


async def discover_api(
    config: ExecConfig,
    surface: Surface,
    *,
    token: str = "",
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Record the target's API surface as declared by the pipeline."""
    base = config.profile_api_url
    if not base:
        raise ExecError("no api_url in the profile; discovery needs to know what to look at")

    headers = _auth_headers(config, token)
    result: dict[str, Any] = {"api_url": base}

    async with httpx.AsyncClient(verify=verify_tls, timeout=30, follow_redirects=True) as client:
        if surface.openapi:
            url = surface.openapi
            if not url.startswith(("http://", "https://")):
                url = f"{base.rstrip('/')}/{url.lstrip('/')}"
            result["openapi"] = await _fetch_openapi(client, url, headers)
        if surface.paths:
            result["paths"] = await _probe(client, base, surface.paths, headers)

    return result


def write_context(result: dict[str, Any], path: Path) -> None:
    """Write what was discovered as a context fragment.

    Discovery output is knowledge about one deployment of one application, which is what a context
    is. Writing it in that shape is what lets an agent import it through the layer that owns subject
    knowledge, rather than through a builtin that had opinions about somebody's API.
    """
    lines = [
        "---",
        "name: discovered-api",
        f"description: The API surface of {result.get('api_url', 'the target')}, as observed",
        "---",
        "",
        f"Observed at `{result.get('api_url', '')}`.",
        "",
    ]

    document = result.get("openapi") or {}
    if document:
        info = document.get("info") or {}
        title = info.get("title") or "the API"
        lines.append(f"`{title}` publishes an OpenAPI document describing these paths:")
        lines.append("")
        for route, methods in sorted((document.get("paths") or {}).items()):
            verbs = ", ".join(sorted(verb.upper() for verb in methods if verb.islower()))
            lines.append(f"- `{route}` — {verbs}")
        lines.append("")

    probed = result.get("paths") or {}
    if probed:
        lines.append("Paths this pipeline probed, and what answered:")
        lines.append("")
        for route, entry in sorted(probed.items()):
            lines.append(f"- `{route}` — HTTP {entry.get('status')}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
