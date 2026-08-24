# Extracted from pipeline-framework src/executors/api_session.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

import asyncio
import json
import random
import ssl

import httpx

from ..logging import log
from .types import ToolResult


class ApiSession:
    """HTTP/REST test execution with auth, rate limit retry, and method fallback."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        auth_method: str = "jwt",
        login_path: str = "",
        api_key_header: str = "",
        insecure_tls: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._auth_method = auth_method
        self._login_path = login_path
        self._api_key_header = api_key_header
        self._auth_headers: dict[str, str] = {}
        self._authenticated = False
        self._rate_limit_ms: float = 0
        self._last_request_time = 0.0
        self._request_log: list[str] = []

        # Certificates are verified unless a profile has explicitly asked otherwise.
        #
        # This was unconditional — `check_hostname = False`, `verify_mode = CERT_NONE`, and
        # `verify=False` on every request. It arrived with code extracted from a harness pointed at
        # one staging environment behind a self-signed certificate, where it was a reasonable local
        # convenience. In a runtime that holds a profile's credentials and talks to whatever host a
        # pipeline names, it is that convenience made permanent for everybody: anything able to
        # intercept the connection reads the credentials and rewrites the responses the pipeline
        # then reports on as test results.
        #
        # The staging case is real, so it is still reachable — declared per profile, visible in the
        # spec, and never inherited by a profile that did not ask for it.
        self._verify: ssl.SSLContext | bool = True
        if insecure_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self._verify = context
            log.warning(
                "    API: TLS verification is OFF for %s - the profile declares insecure_tls",
                self._base_url,
            )

    async def _ensure_authenticated(self) -> None:
        if self._authenticated:
            return

        if self._auth_method == "none":
            self._authenticated = True
            return

        if self._auth_method == "basic":
            import base64

            creds = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
            self._auth_headers["Authorization"] = f"Basic {creds}"
            self._authenticated = True
            return

        if self._auth_method == "api-key":
            header = self._api_key_header or "X-API-Key"
            self._auth_headers[header] = self._password
            self._authenticated = True
            return

        # JWT auth with rate limit retry
        for attempt in range(10):
            try:
                async with httpx.AsyncClient(verify=self._verify, timeout=30) as client:
                    response = await client.post(
                        f"{self._base_url}{self._login_path}",
                        json={"username": self._username, "password": self._password},
                        headers={"Content-Type": "application/json"},
                    )

                if response.status_code == 429:
                    delay = random.uniform(10, 30)
                    log.debug(f"    API: Login rate-limited, retry {attempt + 1}/10 in {delay:.0f}s...")
                    await asyncio.sleep(delay)
                    continue

                if response.is_success:
                    data = response.json()
                    token = (
                        data.get("access_token") or data.get("token") or data.get("jwt") or data.get("key")
                    )
                    token_type = data.get("token_type", "Bearer")
                    if token:
                        self._auth_headers["Authorization"] = f"{token_type} {token}"
                        self._authenticated = True
                        log.debug(f"    API: Authenticated via {self._auth_method}")
                        return
                break
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
                break

    async def execute_tool(self, name: str, params: dict[str, object]) -> ToolResult:
        if name != "http_request":
            return ToolResult(text=f"Unknown API tool: {name}")

        await self._ensure_authenticated()

        method = str(params.get("method", "GET")).upper()
        url = str(params.get("url", ""))
        if not url.startswith("http"):
            url = self._base_url + url

        body = params.get("body")
        extra_headers = params.get("headers", {})
        if isinstance(extra_headers, str):
            extra_headers = json.loads(extra_headers)

        # Rate throttle
        if self._rate_limit_ms > 0:
            import time

            elapsed = (time.time() - self._last_request_time) * 1000
            if elapsed < self._rate_limit_ms:
                await asyncio.sleep((self._rate_limit_ms - elapsed) / 1000)

        log.debug(f"    API: {method} {url}")

        body_str: str | None = None
        if body is not None:
            body_str = body if isinstance(body, str) else json.dumps(body)
        elif method in ("POST", "PUT"):
            body_str = "{}"

        # Support no_auth flag to skip auto-injected auth (for testing unauthenticated access)
        no_auth = bool(params.get("no_auth", False))
        headers: dict[str, str] = {} if no_auth else {**self._auth_headers}
        if body_str:
            headers.setdefault("Content-Type", "application/json")
        # Step-level headers override auto-injected headers (enables multi-user auth testing)
        if isinstance(extra_headers, dict) and extra_headers:
            headers.update(extra_headers)

        async with httpx.AsyncClient(verify=self._verify, timeout=60) as client:
            # Rate limit retry: up to 10 times with random 10-30s backoff
            response: httpx.Response | None = None
            for rl_attempt in range(11):
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    content=body_str,
                )

                if response.status_code == 429:
                    if rl_attempt < 10:
                        delay = random.uniform(10, 30)
                        log.debug(
                            f"    API: {method} {url} rate-limited, retry {rl_attempt + 1}/10 in {delay:.0f}s..."
                        )
                        await asyncio.sleep(delay)
                        continue
                break

            assert response is not None

            # Auto-renew token on 401
            if response.status_code == 401 and self._auth_method == "jwt":
                log.debug(f"    API: {method} {url} got 401, re-authenticating...")
                self._authenticated = False
                await self._ensure_authenticated()
                if self._authenticated:
                    headers = {**self._auth_headers}
                    if body_str:
                        headers.setdefault("Content-Type", "application/json")
                    response = await client.request(method, url, headers=headers, content=body_str)

            # PATCH↔PUT fallback on 405
            if response.status_code == 405:
                alt = "PUT" if method == "PATCH" else "PATCH" if method == "PUT" else None
                if alt:
                    log.debug(f"    API: {method} returned 405, retrying as {alt}")
                    response = await client.request(alt, url, headers=headers, content=body_str)

            # Adapt throttle from X-RateLimit-Limit header
            limit_header = response.headers.get("x-ratelimit-limit")
            if limit_header and self._rate_limit_ms == 0:
                try:
                    req_per_min = int(limit_header)
                    if req_per_min > 0:
                        self._rate_limit_ms = (60000 / req_per_min) + 100
                        log.debug(
                            f"    API: Rate limit detected: {req_per_min}/min → {self._rate_limit_ms:.0f}ms throttle"
                        )
                except ValueError:
                    pass

            import time

            self._last_request_time = time.time()

            # Build result
            resp_headers = dict(response.headers)
            try:
                resp_body = json.dumps(response.json(), indent=2)[:8000]
            except Exception:
                resp_body = response.text[:4000]

            result_text = (
                f"Status: {response.status_code} {response.reason_phrase}\n"
                f"\nResponse Headers:\n{json.dumps(resp_headers, indent=2)[:1000]}"
                f"\nResponse Body:\n{resp_body}"
            )

            self._request_log.append(f"{method} {url} → {response.status_code}")
            return ToolResult(text=result_text)
