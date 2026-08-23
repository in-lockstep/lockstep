# Extracted from pipeline-framework src/builtins/discovery.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ..config import ExecConfig
from ..logging import log


async def discover_api(config: ExecConfig) -> dict[str, Any]:
    """Discover API schemas by probing live endpoints."""
    cache_path = Path(config.output_dir) / "api-schemas.json"

    # Use cache if less than 24 hours old
    if cache_path.exists():
        import time

        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours < 24:
            log.info(f"  Using cached API schema discovery ({age_hours:.1f}h old)")
            return json.loads(cache_path.read_text(encoding="utf-8"))

    api_url = config.profile_api_url
    if not api_url:
        log.warning("  No API URL configured — skipping API discovery")
        return {}

    log.info(f"  Discovering API schemas from {api_url}")

    # Probe endpoints
    schemas: dict[str, Any] = {}
    endpoints = [
        "/api/v1/projects",
        "/api/v1/workflows",
        "/api/v1/users",
        "/api/v1/groups",
        "/api/v1/roles",
        "/api/v1/policies",
        "/api/v1/integrations",
        "/api/v1/credentials",
        "/api/v1/executions",
        "/api/v1/role_assignments",
        "/api/v1/service_accounts",
        "/api/v1/identity_providers",
        "/api/v1/settings",
    ]

    # Authenticate first
    auth_headers = await _get_auth_headers(config)

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        for endpoint in endpoints:
            url = f"{api_url}{endpoint}"
            schema: dict[str, Any] = {"endpoint": endpoint, "methods": {}}

            # GET to discover response structure
            try:
                resp = await client.get(f"{url}?limit=1", headers=auth_headers)
                if resp.is_success:
                    schema["methods"]["GET"] = {
                        "status": resp.status_code,
                        "sample": resp.json(),
                    }
            except Exception:
                pass

            # Iterative POST probing: send progressively more fields
            post_headers = {**auth_headers, "Content-Type": "application/json"}
            body: dict[str, Any] = {}
            all_errors: list[dict[str, Any]] = []

            for probe_round in range(3):
                try:
                    resp = await client.post(url, headers=post_headers, json=body)
                    error_data = resp.json() if resp.status_code in (400, 422) else {}
                    all_errors.append({"round": probe_round, "status": resp.status_code, "body": error_data})

                    if resp.status_code == 422:
                        # Extract required fields from error and add defaults for next probe
                        import re as re_mod

                        for m in re_mod.finditer(r"(\w+): Field required", json.dumps(error_data)):
                            field_name = m.group(1)
                            defaults: dict[str, Any] = {
                                "name": "probe-test",
                                "project_id": "probe",
                                "schema_version": "2.0.0",
                                "effect": "allow",
                                "resource_type": "organization",
                                "scope": "organization",
                            }
                            if field_name not in body:
                                body[field_name] = defaults.get(field_name, "probe")
                    elif resp.status_code in (201, 200, 409):
                        break
                    else:
                        break
                except Exception:
                    break

            if all_errors:
                schema["methods"]["POST"] = {
                    "probes": all_errors,
                    "discovered_fields": list(body.keys()),
                }

            if schema["methods"]:
                schemas[endpoint] = schema

    # Save cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(schemas, indent=2, default=str), encoding="utf-8")
    log.info(f"  Discovered {len(schemas)} API endpoints")

    # Also save endpoints list
    endpoints_path = Path(config.output_dir) / "api-endpoints.txt"
    endpoints_path.write_text("\n".join(sorted(schemas.keys())), encoding="utf-8")

    return schemas


async def _get_auth_headers(config: ExecConfig) -> dict[str, str]:
    """Get authentication headers for API probing."""
    if config.profile_auth_method == "none":
        return {}

    if config.profile_auth_method == "basic":
        import base64

        creds = base64.b64encode(f"{config.profile_username}:{config.profile_password}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}

    # JWT login
    try:
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            resp = await client.post(
                f"{config.profile_api_url}{config.profile_auth_login_path}",
                json={"username": config.profile_username, "password": config.profile_password},
            )
            if resp.is_success:
                data = resp.json()
                token = data.get("access_token") or data.get("token") or data.get("jwt")
                if token:
                    return {"Authorization": f"Bearer {token}"}
    except Exception as e:
        log.warning(f"  Auth failed during discovery: {e}")

    return {}
