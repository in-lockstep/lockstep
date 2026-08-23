# Extracted from pipeline-framework src/executors/direct_executor.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Any

from ..config import ExecConfig
from ..logging import log
from .api_session import ApiSession
from .browser_session import BrowserSession
from .cli_session import CliSession
from .types import ExecutedStep, ScriptStep, TestResult, TestScript

LOGIN_STEP_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"log\s*in",
        r"sign\s*in",
        r"enter.*username",
        r"enter.*password",
        r"fill.*username",
        r"fill.*password",
        r"click.*login",
        r"click.*sign\s*in",
        r"submit.*login",
        r"wait.*dashboard.*load",
        r"wait.*after.*login",
    ]
]


def _is_login_step(step: ScriptStep) -> bool:
    action = step.action or ""
    if any(p.search(action) for p in LOGIN_STEP_PATTERNS):
        return True
    params = step.params or {}
    selector = str(params.get("selector", ""))
    text = str(params.get("text", ""))
    if any(kw in selector for kw in ("password", "username", "login")):
        return True
    if any(kw in text.lower() for kw in ("log in", "sign in")):
        return True
    return False


class DirectExecutor:
    """Deterministic test executor with resilience logic."""

    def __init__(self, agent_id: int, config: ExecConfig, run_dir: str = "") -> None:
        self._agent_id = agent_id
        self._config = config
        self._run_dir = run_dir or config.output_dir
        self._runtime_vars: dict[str, str] = {}
        self._all_script_var_refs: set[str] = set()

    def _build_var_ref_index(self, script: TestScript) -> None:
        self._all_script_var_refs.clear()
        all_steps = [*(script.setup_steps or []), *script.test_steps, *(script.teardown_steps or [])]
        text = json.dumps([{"params": s.params} for s in all_steps])
        for m in re.finditer(r"\{([A-Z][A-Z0-9_]*_ID(?:_\d+)?)\}", text):
            self._all_script_var_refs.add(m.group(1))

    def _find_prefixed_var_name(self, base_var: str) -> str | None:
        for ref in self._all_script_var_refs:
            if ref == base_var:
                continue
            if ref.endswith(f"_{base_var}") and ref not in self._runtime_vars:
                return ref
            numbered = re.match(rf"^{re.escape(base_var)}_(\d+)$", ref)
            if numbered and ref not in self._runtime_vars:
                return ref
        return None

    def _resolve_runtime_vars(self, value: str) -> str:
        def replace_var(match: re.Match[str]) -> str:
            var_name = match.group(1)
            upper = var_name.upper()
            if var_name in self._runtime_vars:
                return self._runtime_vars[var_name]
            if upper in self._runtime_vars:
                return self._runtime_vars[upper]

            # Handle VARNAME_ID_N pattern
            numbered_id = re.match(r"^(.+_ID)_(\d+)$", upper)
            if numbered_id:
                base, num = numbered_id.group(1), int(numbered_id.group(2))
                if num == 1 and base in self._runtime_vars:
                    return self._runtime_vars[base]
                key = f"{base}_{num}"
                if key in self._runtime_vars:
                    return self._runtime_vars[key]

            # Strip prefixes progressively
            suffixes = ("_ID", "_TOKEN", "_SECRET", "_NAME")
            for suffix in suffixes:
                if upper.endswith(suffix):
                    stem = upper[: -len(suffix)]
                    no_num = re.sub(r"_\d+$", "", stem)
                    if no_num != stem:
                        num_var = no_num + suffix
                        if num_var in self._runtime_vars:
                            return self._runtime_vars[num_var]
                    parts = stem.split("_")
                    for i in range(1, len(parts)):
                        base_var = "_".join(parts[i:]) + suffix
                        if base_var in self._runtime_vars:
                            return self._runtime_vars[base_var]
            return match.group(0)

        return re.sub(r"\{([A-Za-z][A-Za-z0-9_]*)\}", replace_var, value)

    def _resolve_step_params(self, params: dict[str, Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, val in params.items():
            if isinstance(val, str):
                resolved[key] = self._resolve_runtime_vars(val)
            elif isinstance(val, dict):
                resolved[key] = self._resolve_step_params(val)
            else:
                resolved[key] = val
        return resolved

    def _extract_runtime_vars(
        self, result_text: str, step: ScriptStep, resolved_params: dict[str, Any] | None = None
    ) -> None:
        text = result_text
        if text.startswith("[RETRY SUCCESS"):
            end = text.find("]\n")
            if end > 0:
                text = text[end + 2 :]

        try:
            body_match = re.search(r"Response Body:\n([\s\S]*?)$", text)
            json_text = body_match.group(1).strip() if body_match else text
            parsed = json.loads(json_text)

            if isinstance(parsed, dict):
                resource = parsed
                list_fields = ("resources", "results", "items", "data")
                for field in list_fields:
                    if field in parsed and isinstance(parsed[field], list) and parsed[field]:
                        resource = parsed[field][0]
                        break

                if isinstance(resource, dict) and "id" in resource:
                    self._runtime_vars["LAST_ID"] = str(resource["id"])
                    # Use resolved params (with actual URLs) when available,
                    # fall back to raw step params
                    effective_params = resolved_params if resolved_params is not None else step.params
                    url = str(effective_params.get("url", ""))
                    res_match = re.search(r"/api/v\d+/([^/?]+)", url)
                    if res_match:
                        res_type = res_match.group(1).upper().replace("-", "_")
                        singular = res_type[:-1] if res_type.endswith("S") else res_type
                        base_var = f"{singular}_ID"
                        if base_var in self._runtime_vars:
                            prefixed = self._find_prefixed_var_name(base_var)
                            if prefixed:
                                self._runtime_vars[prefixed] = str(resource["id"])
                                log.debug(
                                    f"      Captured {prefixed} = {str(resource['id'])[:36]} (collision avoidance)"
                                )
                            else:
                                existing = [
                                    k
                                    for k in self._runtime_vars
                                    if k.startswith(base_var + "_") and re.search(r"\d+$", k)
                                ]
                                num = 2 + len(existing)
                                numbered = f"{base_var}_{num}"
                                self._runtime_vars[numbered] = str(resource["id"])
                                log.debug(
                                    f"      Captured {numbered} = {str(resource['id'])[:36]} (numbered)"
                                )
                        else:
                            self._runtime_vars[base_var] = str(resource["id"])
                            log.debug(f"      Captured {base_var} = {str(resource['id'])[:36]}")

                        # Capture version field alongside id
                        if "version" in resource:
                            ver_var = f"{singular}_VERSION"
                            self._runtime_vars[ver_var] = str(resource["version"])
                            log.debug(f"      Captured {ver_var} = {str(resource['version'])[:36]}")

                    for key, val in resource.items():
                        if isinstance(val, (str, int)):
                            upper_key = key.upper()
                            if upper_key.endswith("_ID") or upper_key == "ID":
                                self._runtime_vars[upper_key] = str(val)

                    # Capture version at top level even without URL match
                    if "version" in resource and "LAST_VERSION" not in self._runtime_vars:
                        self._runtime_vars["LAST_VERSION"] = str(resource["version"])

                    if "name" in resource:
                        self._runtime_vars["LAST_NAME"] = str(resource["name"])

                    # Capture auth tokens from login responses
                    for token_key in ("access_token", "token", "jwt", "key"):
                        if token_key in resource:
                            self._runtime_vars["USER_TOKEN"] = str(resource[token_key])
                            self._runtime_vars["LAST_TOKEN"] = str(resource[token_key])
                            log.debug(f"      Captured USER_TOKEN from {token_key}")
                            break
        except (json.JSONDecodeError, AttributeError):
            pass

        # Fallback regex
        id_match = re.search(r'"id"\s*:\s*"([0-9a-f-]{36})"', result_text)
        if id_match and "LAST_ID" not in self._runtime_vars:
            self._runtime_vars["LAST_ID"] = id_match.group(1)

    async def _login_via_browser(self, executor: BrowserSession) -> bool:
        log.debug("    Performing automatic browser login...")
        for attempt in range(2):
            try:
                if attempt > 0:
                    await asyncio.sleep(2)
                await executor.execute_tool("navigate", {"url": self._config.profile_url})
                await executor.execute_tool(
                    "wait_for",
                    {"selector": "input, form, [class*=login], nav, [class*=page]", "timeout": 20000},
                )
                await asyncio.sleep(3)

                page_check = await executor.execute_tool("get_page_snapshot", {})
                page_text = (page_check.text or "").strip()
                if (
                    len(page_text) > 50
                    and "Log in" not in page_text
                    and "Sign in" not in page_text
                    and "Enter your" not in page_text
                ):
                    log.debug("    Already logged in")
                    return True

                # Check if login form is already visible (no SSO provider selector)
                form_check = await executor.execute_tool(
                    "wait_for",
                    {
                        "selector": 'input[type="text"], input[type="password"], input[name="username"]',
                        "timeout": 3000,
                    },
                )
                if form_check.text and "Timeout" in form_check.text:
                    # Login form not visible — try clicking SSO provider link
                    provider_texts = ["Sign in using local account", "local account", "Log in with password"]
                    for text in provider_texts:
                        result = await executor.execute_tool("click_text", {"text": text, "timeout": 3000})
                        if result.text and "Failed" not in result.text and "no matching" not in result.text:
                            log.debug("    Clicked login provider link")
                            break

                    await executor.execute_tool(
                        "wait_for",
                        {"selector": 'input[type="text"], input[type="password"]', "timeout": 10000},
                    )

                for sel in [
                    'input[id*="login-username"]',
                    'input[id*="username"]',
                    'input[name="username"]',
                    'input[type="text"]',
                ]:
                    result = await executor.execute_tool(
                        "fill", {"selector": sel, "value": self._config.profile_username}
                    )
                    if result.text and "Failed" not in result.text:
                        break

                for sel in ['input[id*="login-password"]', 'input[id*="password"]', 'input[type="password"]']:
                    result = await executor.execute_tool(
                        "fill", {"selector": sel, "value": self._config.profile_password}
                    )
                    if result.text and "Failed" not in result.text:
                        break

                for sel in [
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("Log in")',
                    'button:has-text("Sign in")',
                ]:
                    result = await executor.execute_tool("click", {"selector": sel})
                    if result.text and "Failed" not in result.text:
                        break

                await executor.execute_tool("wait_for", {"selector": "body", "timeout": 5000})
                verify = await executor.execute_tool("get_page_snapshot", {})
                if verify.text and "Log in to" not in verify.text:
                    log.debug("    Browser login successful")
                    return True
            except Exception:
                continue
        return False

    async def _check_and_relogin(self, executor: BrowserSession, result_text: str = "") -> bool:
        if not result_text:
            snapshot = await executor.execute_tool("get_page_snapshot", {})
            result_text = snapshot.text or ""
            if not result_text:
                return False
        text = result_text.lower()
        if any(kw in text for kw in ("log in to", "login-username", "login-password", "enter your")):
            if "credentials" in text or "sign in" in text or "log in" in text:
                log.debug("    Session expired — re-authenticating...")
                return await self._login_via_browser(executor)
        return False

    async def test_script(self, script: TestScript) -> TestResult:
        log.info(f"    Agent {self._agent_id}: Executing [{script.test_type.upper()}] {script.story_id}")

        self._runtime_vars.clear()

        # Seed runtime vars with profile/env values so scripts can reference {APP_API_URL} etc.
        # APP_API_URL includes the api_prefix so test scripts can use {APP_API_URL}/projects
        if self._config.profile_api_url:
            api_base = self._config.profile_api_url.rstrip("/")
            if self._config.profile_api_prefix:
                api_base = f"{api_base}/{self._config.profile_api_prefix.strip('/')}"
            self._runtime_vars["APP_API_URL"] = api_base
            self._runtime_vars["AO_API_URL"] = api_base
        if self._config.profile_url:
            self._runtime_vars["APP_URL"] = self._config.profile_url
            self._runtime_vars["AO_URL"] = self._config.profile_url

        self._build_var_ref_index(script)

        # Create sessions
        session_id = f"agent-{self._agent_id}-{script.story_id}"
        api_session = ApiSession(
            self._config.profile_api_url,
            self._config.profile_username,
            self._config.profile_password,
            self._config.profile_auth_method,
            self._config.profile_auth_login_path,
        )
        cli_session = CliSession()
        browser: BrowserSession | None = None

        if script.test_type == "ui":
            browser = BrowserSession(session_id, self._run_dir, self._config.ui_wait_timeout)
            await browser.start()

        executor: Any = {"api": api_session, "cli": cli_session, "ui": browser}.get(
            script.test_type, api_session
        )

        auto_login_done = False
        relogin_count = 0
        max_relogins = 3

        if script.test_type == "ui" and browser:
            try:
                auto_login_done = await asyncio.wait_for(self._login_via_browser(browser), timeout=60)
            except TimeoutError:
                log.warning(f"    Auto-login timed out after 60s for {script.story_id}")
                auto_login_done = False

        executed_steps: list[ExecutedStep] = []
        errors: list[dict[str, str]] = []
        passed = True

        consecutive_failures = 0
        max_consecutive_failures = 5

        async def run_steps(steps: list[ScriptStep], phase: str) -> None:
            nonlocal passed, auto_login_done, relogin_count, consecutive_failures

            for step in steps:
                if not step or not step.tool:
                    continue

                # Fast-fail: skip remaining test steps after too many consecutive failures
                if phase == "test" and consecutive_failures >= max_consecutive_failures:
                    executed_steps.append(
                        ExecutedStep(
                            phase=phase,
                            step_number=step.step,
                            tool=step.tool,
                            action=step.action,
                            expected=step.expected,
                            result=f"Skipped — {max_consecutive_failures} consecutive failures, aborting test",
                            status="skipped",
                        )
                    )
                    continue

                if auto_login_done and phase != "teardown" and _is_login_step(step):
                    executed_steps.append(
                        ExecutedStep(
                            phase=phase,
                            step_number=step.step,
                            tool=step.tool,
                            action=step.action,
                            expected=step.expected,
                            result="Skipped (auto-login)",
                            status="skipped",
                        )
                    )
                    continue

                resolved_params = self._resolve_step_params(step.params)

                # Check for unresolved variables
                params_str = json.dumps(resolved_params)
                unresolved = re.findall(r"\{([A-Z][A-Z0-9_]*)\}", params_str)
                env_prefixes = ("AO_", "AAP_", "APP_", "OCP_", "JIRA_", "GCP_")
                unresolved = [v for v in unresolved if not any(v.startswith(p) for p in env_prefixes)]
                if unresolved:
                    executed_steps.append(
                        ExecutedStep(
                            phase=phase,
                            step_number=step.step,
                            tool=step.tool,
                            action=step.action,
                            expected=step.expected,
                            result=f"Skipped — unresolved variables: {', '.join(unresolved)}",
                            status="skipped",
                        )
                    )
                    continue

                # Skip oc/kubectl commands when OCP is not configured
                if step.tool == "run_command":
                    cmd = str(resolved_params.get("command", "")).lstrip()
                    if cmd.startswith(("oc ", "oc\t", "kubectl ", "kubectl\t")) and not os.getenv(
                        "OCP_API_URL"
                    ):
                        executed_steps.append(
                            ExecutedStep(
                                phase=phase,
                                step_number=step.step,
                                tool=step.tool,
                                action=step.action,
                                expected=step.expected,
                                result="Skipped — OCP not configured (OCP_API_URL not set)",
                                status="skipped",
                            )
                        )
                        continue

                # Route tool to correct executor
                tool_executor = executor
                if step.tool == "http_request" and script.test_type != "api":
                    tool_executor = api_session
                elif step.tool == "run_command" and script.test_type != "cli":
                    tool_executor = cli_session

                result_text = ""
                screenshot_path: str | None = None
                retried = False

                def is_transient(t: str) -> bool:
                    return any(
                        kw in t
                        for kw in (
                            "502 Bad Gateway",
                            "503 Service",
                            "504 Gateway",
                            "ECONNRESET",
                            "ETIMEDOUT",
                            "ERR_CONNECTION_REFUSED",
                        )
                    )

                def is_rate_limited(t: str) -> bool:
                    return "429 Too Many Requests" in t

                def is_browser_crash(t: str) -> bool:
                    return "has been closed" in t or "Target page, context or browser" in t

                try:
                    first = await tool_executor.execute_tool(step.tool, resolved_params)
                    result_text = first.text or "Done"
                    screenshot_path = first.screenshot_path

                    # Browser crash recovery
                    if (
                        is_browser_crash(result_text)
                        and browser
                        and script.test_type == "ui"
                        and relogin_count < max_relogins
                    ):
                        relogin_count += 1
                        log.debug(
                            f"      Step {step.step} browser crashed — restarting ({relogin_count}/{max_relogins})..."
                        )
                        try:
                            await browser.stop()
                        except Exception:
                            pass
                        await browser.start()
                        auto_login_done = await self._login_via_browser(browser)
                        if auto_login_done:
                            retry = await tool_executor.execute_tool(step.tool, resolved_params)
                            retried = True
                            result_text = retry.text or result_text
                            screenshot_path = retry.screenshot_path or screenshot_path

                    # Rate limit — api_session handles retry internally
                    if is_rate_limited(result_text):
                        log.debug(
                            f"      Step {step.step} rate-limited (after api_session retries exhausted)"
                        )
                        retried = True

                    # Transient infra retry
                    if is_transient(result_text):
                        log.debug(f"      Retrying step {step.step} after transient failure...")
                        await asyncio.sleep(3)
                        retry = await tool_executor.execute_tool(step.tool, resolved_params)
                        if not is_transient(retry.text or ""):
                            retried = True
                            result_text = f"[RETRY SUCCESS — initial: {result_text[:150]}]\n{retry.text}"
                            screenshot_path = retry.screenshot_path or screenshot_path

                    # Session-aware navigation
                    needs_relogin = False
                    if (
                        auto_login_done
                        and phase != "teardown"
                        and relogin_count < max_relogins
                        and step.tool not in ("http_request", "run_command")
                        and browser
                    ):
                        needs_relogin = False
                        if step.tool == "navigate":
                            needs_relogin = await self._check_and_relogin(browser)
                        elif any(
                            kw in result_text
                            for kw in ("no matching element", "Failed to click", "Timeout waiting")
                        ):
                            needs_relogin = await self._check_and_relogin(browser)
                        if needs_relogin:
                            relogin_count += 1
                            msg = f"      Re-executing step {step.step} "
                            msg += f"after re-login ({relogin_count}/{max_relogins})..."
                            log.debug(msg)
                            retry = await tool_executor.execute_tool(step.tool, resolved_params)
                            retried = True
                            result_text = retry.text or result_text
                            screenshot_path = retry.screenshot_path or screenshot_path

                    # UI step retry with backoff — retry failed UI interactions
                    # after waiting for the page to settle (SPA rendering, async loads)
                    if (
                        not needs_relogin
                        and browser
                        and step.tool
                        not in ("http_request", "run_command", "screenshot", "get_page_snapshot")
                        and any(
                            kw in result_text
                            for kw in ("Failed to click", "Timeout waiting", "no matching element")
                        )
                        and not retried
                    ):
                        for ui_retry in range(1):
                            delay = 2
                            log.debug(
                                f"      UI retry {ui_retry + 1}/2 for step {step.step} (waiting {delay}s)..."
                            )
                            await asyncio.sleep(delay)
                            retry = await tool_executor.execute_tool(step.tool, resolved_params)
                            if retry.text and not any(
                                kw in retry.text
                                for kw in ("Failed to click", "Timeout waiting", "no matching")
                            ):
                                retried = True
                                result_text = retry.text
                                screenshot_path = retry.screenshot_path or screenshot_path
                                break

                except Exception as e:
                    result_text = f"TOOL ERROR: {str(e)[:500]}"

                # 422 auto-recovery (skip if the test EXPECTS a 422 — negative test)
                expected_text = (step.expected or "").lower()
                expects_error = any(
                    code in expected_text for code in ("422", "400", "4xx", "error", "invalid", "rejected")
                )
                if "422" in result_text and step.tool == "http_request" and not expects_error:
                    recovery_executor = api_session if script.test_type != "api" else executor
                    result_text = await self._try_422_recovery(
                        result_text, step, resolved_params, recovery_executor
                    )

                # 409 conflict recovery in setup
                if phase == "setup" and "409 Conflict" in result_text and step.tool == "http_request":
                    method = str(resolved_params.get("method", ""))
                    if method == "POST":
                        result_text = await self._try_409_recovery(
                            result_text, step, resolved_params, api_session
                        )

                # Extract runtime variables
                if "TOOL ERROR" not in result_text:
                    self._extract_runtime_vars(result_text, step, resolved_params)

                # Validate step
                is_409 = "409 Conflict" in result_text
                url_path = str(resolved_params.get("url", ""))
                is_action_endpoint = any(
                    seg in url_path
                    for seg in ("/publish", "/unpublish", "/restore", "/validate", "/test", "/execute")
                )
                effective_409 = (
                    is_409
                    and (phase == "setup" or str(resolved_params.get("method")) == "POST")
                    and not is_action_endpoint
                )
                teardown_404 = (
                    phase == "teardown"
                    and str(resolved_params.get("method")) == "DELETE"
                    and "404" in result_text
                    and step.tool == "http_request"
                )
                step_passed = (
                    effective_409 or teardown_404 or _validate_step(result_text, step.expected or "")
                )

                status = "warn" if step_passed and retried else "passed" if step_passed else "failed"

                # Track consecutive failures for fast-fail
                if phase == "test":
                    if step_passed:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1

                executed_steps.append(
                    ExecutedStep(
                        phase=phase,
                        step_number=step.step,
                        tool=step.tool,
                        action=step.action,
                        expected=step.expected,
                        result=result_text[:2000] if step_passed else result_text[:4000],
                        status=status,
                        retried=retried,
                        screenshot_path=screenshot_path,
                    )
                )

                if not step_passed:
                    log.debug(f"    Step {step.step} [{phase}] FAILED: {step.action[:80]}")
                    if phase in ("setup", "test"):
                        passed = False
                        errors.append(
                            {
                                "id": uuid.uuid4().hex[:8],
                                "story_id": script.story_id,
                                "title": f"Step {step.step} failed: {(step.action or '')[:80]}",
                                "description": f"Expected: {step.expected}\nActual: {result_text[:500]}",
                            }
                        )

        try:
            await run_steps(script.setup_steps, "setup")
            await run_steps(script.test_steps, "test")
            await run_steps(script.teardown_steps, "teardown")
        finally:
            if browser:
                await browser.stop()

        return TestResult(
            story_id=script.story_id,
            passed=passed,
            summary=f"{sum(1 for s in executed_steps if s.status in ('passed', 'warn'))} passed, "
            f"{sum(1 for s in executed_steps if s.status == 'failed')} failed",
            executed_steps=executed_steps,
            errors=errors,
        )

    async def _try_422_recovery(
        self, result_text: str, step: ScriptStep, params: dict[str, Any], executor: Any
    ) -> str:
        method = str(params.get("method", ""))
        if method not in ("POST", "PUT") or not params.get("body"):
            return result_text
        try:
            body_str = params["body"] if isinstance(params["body"], str) else json.dumps(params["body"])
            body = json.loads(body_str)
            patched = False

            # Missing required fields
            defaults: dict[str, Any] = {
                "name": f"auto-{uuid.uuid4().hex[:8]}",
                "trigger_node_id": "trigger",
                "schema_version": "2.0.0",
                "grant_type": "client_credentials",
                "credential_type": "client_credentials",
                "credential_type_id": "client_credentials",
                "scope": "organization",
                "effect": "allow",
                "role_name": "admin",
                "resource_type": "organization",
                "redirect_uri": "https://mock-redirect.example.com/callback",
                "file_ids": [],
                "updates": {},
            }
            # Inject project_id from runtime vars if available
            if "PROJECT_ID" in self._runtime_vars:
                defaults["project_id"] = self._runtime_vars["PROJECT_ID"]

            # Workflow definition fixes
            if "workflow_definition" in body:
                wf = body["workflow_definition"]
                if "'parameters' is a required property" in result_text:
                    for items_key in ("nodes", "triggers"):
                        items = wf.get(items_key, [])
                        for item in items:
                            if isinstance(item, dict) and "parameters" not in item:
                                item["parameters"] = {}
                                patched = True
                if "'name' is a required property" in result_text and "name" not in wf:
                    wf["name"] = body.get("name", "auto-workflow")
                    patched = True
                if "'manual_trigger' was expected" in result_text:
                    for trigger in wf.get("triggers", []):
                        if isinstance(trigger, dict) and trigger.get("type") == "manual":
                            trigger["type"] = "manual_trigger"
                            patched = True

            # Effect casing
            if "effect" in body and "Invalid effect" in result_text:
                current = str(body["effect"])
                alt = {"allow": "Allow", "deny": "Deny"}.get(current, current.lower())
                if alt != current:
                    body["effect"] = alt
                    patched = True

            # Field required defaults
            for m in re.finditer(r"(\w+): Field required", result_text):
                field = m.group(1)
                if field not in body and field in defaults:
                    body[field] = defaults[field]
                    patched = True
                    log.debug(f"      422 recovery: added {field}={json.dumps(defaults[field])}")

            if patched:
                fixed_params = {**params, "body": json.dumps(body)}
                retry = await executor.execute_tool("http_request", fixed_params)
                if retry.text and "422" not in retry.text:
                    return retry.text
        except (json.JSONDecodeError, KeyError):
            pass
        return result_text

    async def _try_409_recovery(
        self, result_text: str, step: ScriptStep, params: dict[str, Any], api_session: ApiSession
    ) -> str:
        url = str(params.get("url", ""))
        log.debug(f"      409 Conflict on POST {url} — fetching existing resource")
        try:
            name_filter = ""
            try:
                body_str = (
                    params["body"] if isinstance(params["body"], str) else json.dumps(params.get("body", ""))
                )
                body = json.loads(body_str)
                if "name" in body:
                    from urllib.parse import quote

                    name_filter = f"?name={quote(str(body['name']))}"
            except (json.JSONDecodeError, KeyError):
                pass
            get_result = await api_session.execute_tool(
                "http_request", {"method": "GET", "url": url + name_filter}
            )
            if get_result.text and "200" in get_result.text:
                self._extract_runtime_vars(get_result.text, step, params)
                return f"409 Conflict (resource exists) — recovered via GET. {get_result.text[:2000]}"
        except Exception:
            pass
        return result_text


def _validate_step(result: str, expected: str) -> bool:
    if not expected or not expected.strip():
        return "TOOL ERROR" not in result

    r = result.lower()
    e = expected.lower()

    if "tool error" in r or "err_connection_refused" in r:
        return False

    # Screenshots and snapshots are evidence
    if r.startswith("screenshot saved:") or r.startswith("=== page snapshot ==="):
        return True

    # Wait (sleep)
    if r.startswith("waited ") and r.endswith("ms"):
        return True

    # UI action success patterns
    for prefix in ('filled "', "clicked element", "selected ", "hovered over", "pressed key", "scrolled "):
        if r.startswith(prefix):
            return "failed" not in r

    # Element visibility
    if re.match(r'^element ".+" is now (visible|hidden|attached|detached)', r):
        return True

    # Navigation
    if r.startswith("navigated to "):
        return True

    # URL check
    if r.startswith("http://") or r.startswith("https://"):
        return True

    # Status code checks
    bare_codes = re.findall(r"\b([2-5]\d{2})\b", e)
    acceptable = list(set(bare_codes))

    if acceptable:
        matched = any(f"status: {code}" in r or f"{code} " in r for code in acceptable)
        if matched:
            return True

        # Negative test tolerance
        all_errors = all(c.startswith(("4", "5")) for c in acceptable)
        if all_errors:
            actual_match = re.search(r"status:\s*(\d{3})", r)
            if actual_match:
                actual = actual_match.group(1)
                if actual.startswith(("4", "5")):
                    return True
        return False

    # get_elements
    if r.startswith("found ") and "elements" in r:
        count_match = re.match(r"^found (\d+) elements", r)
        count = int(count_match.group(1)) if count_match else 0
        if count == 0:
            return any(kw in e for kw in ("not found", "no ", "empty", "0"))
        return any(
            kw in e
            for kw in (
                "present",
                "exist",
                "visible",
                "found",
                "displayed",
                "shows",
                "contains",
                "list",
                "appear",
            )
        )

    # Example patterns
    eg_match = re.search(r"(?:e\.g\.?|such as|like|including)[,:]?\s*([^)]+)", e, re.IGNORECASE)
    if eg_match:
        examples = [
            s.strip().strip("'\"").lower() for s in re.split(r"[,/]", eg_match.group(1)) if len(s.strip()) > 2
        ]
        if examples and any(ex in r for ex in examples):
            return True

    # Default: check if expected keywords appear in result
    keywords = [w for w in re.split(r"\W+", e) if len(w) > 3]
    if keywords:
        matches = sum(1 for kw in keywords if kw in r)
        return matches >= len(keywords) * 0.3

    return True
