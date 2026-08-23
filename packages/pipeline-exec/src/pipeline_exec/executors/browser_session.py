# Extracted from pipeline-framework src/executors/browser_session.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

from .types import ToolResult

# Raw JS string for page.evaluate — must not be a Python function
# to avoid esbuild/tsx injecting __name helper into browser context.
_PAGE_SNAPSHOT_JS = """(() => {
    const TAGS = [
        'button','a','input','textarea','select',
        'h1','h2','h3','h4','label','nav','main','header','footer'
    ];
    const describe = (el, depth) => {
        if (depth > 6) return '';
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return '';
        const tag = el.tagName.toLowerCase();
        const indent = '  '.repeat(depth);
        const role = el.getAttribute('role');
        const ariaLabel = el.getAttribute('aria-label');
        const hasText = el.childNodes.length === 1
            && el.childNodes[0].nodeType === Node.TEXT_NODE;
        const text = hasText
            ? (el.textContent || '').trim().slice(0, 100) : '';
        if (TAGS.includes(tag) || role || ariaLabel) {
            let line = indent + '<' + tag;
            if (role) line += ' role="' + role + '"';
            if (ariaLabel) line += ' aria-label="' + ariaLabel + '"';
            if (tag === 'a')
                line += ' href="' + (el.getAttribute('href') || '') + '"';
            if (tag === 'input' || tag === 'textarea') {
                line += ' type="' + el.type + '" name="' + el.name + '"';
                if (el.placeholder)
                    line += ' placeholder="' + el.placeholder + '"';
            }
            if (text) line += '\\n' + indent + '    ' + text;
            let children = '';
            for (const child of el.children)
                children += describe(child, depth + 1);
            return line + '\\n' + children;
        }
        let children = '';
        for (const child of el.children)
            children += describe(child, depth + 1);
        return children;
    };
    return '=== Page Snapshot ===\\nURL: ' + location.href
        + '\\nTitle: ' + document.title
        + '\\n\\n--- Page Structure ---\\n'
        + describe(document.body, 0);
})()"""


class BrowserSession:
    """Playwright browser session with page-ready waiting, timeout retry, and loading detection."""

    def __init__(self, session_id: str, output_dir: str, default_wait_timeout: int = 30000) -> None:
        self._session_id = session_id
        self._screenshots_dir = Path(output_dir) / "screenshots" / session_id
        self._default_wait_timeout = default_wait_timeout
        self._screenshot_counter = 0
        self._browser: Any = None
        self._page: Any = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
        )
        self._page = await context.new_page()

    async def stop(self) -> None:
        if self._browser:
            try:
                await asyncio.wait_for(self._browser.close(), timeout=5)
            except (TimeoutError, Exception):
                pass
            self._browser = None
            self._page = None
        if hasattr(self, "_pw"):
            try:
                await asyncio.wait_for(self._pw.stop(), timeout=5)
            except (TimeoutError, Exception):
                pass

    def _get_page(self) -> Any:
        if not self._page:
            raise RuntimeError("Browser session not started")
        return self._page

    def _is_timeout_error(self, e: Exception) -> bool:
        return "Timeout" in str(e) and "exceeded" in str(e)

    async def _wait_for_page_ready(self) -> bool:
        page = self._get_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            loading_selector = (
                "[class*=loading], [class*=spinner], [class*=Spinner], "
                "[class*=skeleton], [class*=Skeleton], [aria-busy=true], "
                "[class*=pf-c-spinner], [class*=pf-v5-c-spinner], .pf-m-loading"
            )
            await page.wait_for_function(
                f'() => !document.querySelector("{loading_selector}")',
                timeout=15000,
            )
            return True
        except Exception:
            return False

    async def _retry_on_timeout(self, action: Any, max_retries: int = 2) -> Any:
        """Retry an async action on Playwright timeout errors."""
        page = self._get_page()
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return await action()
            except Exception as e:
                last_error = e
                if not self._is_timeout_error(e) or attempt == max_retries:
                    raise
                delay = random.uniform(3, 5)
                await page.wait_for_timeout(int(delay * 1000))
                await self._wait_for_page_ready()
        raise last_error  # type: ignore[misc]

    async def execute_tool(self, name: str, params: dict[str, object]) -> ToolResult:
        page = self._get_page()

        if name == "navigate":
            url = str(params.get("url", ""))
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1000)
            return ToolResult(text=f"Navigated to {page.url}")

        if name == "click":
            selector = str(params.get("selector", ""))
            try:
                await self._wait_for_page_ready()
                await self._retry_on_timeout(lambda: page.click(selector, timeout=5000))
                await page.wait_for_timeout(500)
                return ToolResult(text=f"Clicked element: {selector}")
            except Exception as e:
                return ToolResult(text=f'Failed to click "{selector}": {e}')

        if name == "click_text":
            text = str(params.get("text", ""))
            exact = bool(params.get("exact", False))
            try:
                await self._wait_for_page_ready()
                if exact:
                    await self._retry_on_timeout(
                        lambda: page.get_by_text(text, exact=True).click(timeout=5000)
                    )
                else:
                    await self._retry_on_timeout(lambda: page.get_by_text(text).first.click(timeout=5000))
                await page.wait_for_timeout(500)
                return ToolResult(text=f'Clicked element with text: "{text}"')
            except Exception:
                # Fallback chain — try alternative selectors regardless of error type
                fallbacks = [
                    lambda: page.locator(f'[aria-label*="{text}" i]').first.click(timeout=2000),
                    lambda: page.locator(f'button:has-text("{text}")').first.click(timeout=2000),
                    lambda: page.locator(f'a:has-text("{text}")').first.click(timeout=2000),
                    lambda: page.get_by_role("button", name=text).first.click(timeout=2000),
                    lambda: page.get_by_role("link", name=text).first.click(timeout=2000),
                    lambda: page.get_by_role("tab", name=text).first.click(timeout=2000),
                    lambda: page.get_by_role("menuitem", name=text).first.click(timeout=2000),
                ]
                for fb in fallbacks:
                    try:
                        await fb()
                        await page.wait_for_timeout(500)
                        return ToolResult(text=f'Clicked element with text: "{text}" (fallback match)')
                    except Exception:
                        continue
                return ToolResult(
                    text=f'Failed to click text "{text}": no matching element found after fallback attempts'
                )

        if name == "fill":
            selector = str(params.get("selector", ""))
            value = str(params.get("value", ""))
            try:
                await self._wait_for_page_ready()
                await self._retry_on_timeout(lambda: page.fill(selector, value, timeout=10000))
                return ToolResult(text=f'Filled "{selector}" with "{value}"')
            except Exception as e:
                return ToolResult(text=f'Failed to fill "{selector}": {e}')

        if name == "select_option":
            selector = str(params.get("selector", ""))
            value = str(params.get("value", ""))
            try:
                await self._wait_for_page_ready()
                await self._retry_on_timeout(lambda: page.select_option(selector, value, timeout=10000))
                return ToolResult(text=f'Selected "{value}" in "{selector}"')
            except Exception as e:
                return ToolResult(text=f"Failed to select option: {e}")

        if name == "wait_for":
            selector_val = params.get("selector")
            timeout = int(str(params.get("timeout", self._default_wait_timeout)))
            state = str(params.get("state", "visible"))
            if not selector_val:
                await page.wait_for_timeout(timeout)
                return ToolResult(text=f"Waited {timeout}ms")
            sel = str(selector_val)
            for attempt in range(3):
                try:
                    await page.wait_for_selector(sel, timeout=timeout, state=state)
                    return ToolResult(text=f'Element "{sel}" is now {state}')
                except Exception as e:
                    if attempt < 2:
                        await asyncio.sleep(2)
                        await self._wait_for_page_ready()
                        if attempt == 1:
                            await page.reload(wait_until="domcontentloaded")
                            await self._wait_for_page_ready()
                        continue
                    return ToolResult(text=f'Timeout waiting for "{sel}" to be {state}: {e}')
            return ToolResult(text=f'Timeout waiting for "{sel}" to be {state}')

        if name == "screenshot":
            for attempt in range(3):
                ready = await self._wait_for_page_ready()
                if ready:
                    break
                delay = random.uniform(3, 5)
                await page.wait_for_timeout(int(delay * 1000))
                await page.reload(wait_until="domcontentloaded")

            full_page = bool(params.get("full_page", False))
            buffer = await page.screenshot(full_page=full_page)
            self._screenshot_counter += 1
            filename = f"screenshot-{self._screenshot_counter}.png"
            filepath = self._screenshots_dir / filename
            filepath.write_bytes(buffer)

            return ToolResult(
                text=f"Screenshot saved: {filepath}",
                image=buffer,
                screenshot_path=str(filepath),
            )

        if name == "get_page_snapshot":
            for attempt in range(3):
                ready = await self._wait_for_page_ready()
                if ready:
                    break
                delay = random.uniform(3, 5)
                await page.wait_for_timeout(int(delay * 1000))
                await page.reload(wait_until="domcontentloaded")

            try:
                snapshot = await page.evaluate(_PAGE_SNAPSHOT_JS)
                return ToolResult(text=snapshot)
            except Exception as e:
                return ToolResult(text=f"Failed to get page snapshot: {e}")

        if name == "get_elements":
            selector = str(params.get("selector", ""))
            try:
                elements = await page.query_selector_all(selector)
                texts = []
                for el in elements[:20]:
                    text = await el.text_content()
                    texts.append((text or "").strip()[:100])
                return ToolResult(text=f"Found {len(elements)} elements:\n{texts}")
            except Exception as e:
                return ToolResult(text=f"Failed to get elements: {e}")

        if name == "get_url":
            return ToolResult(text=page.url)

        if name == "press_key":
            key = str(params.get("key", ""))
            await page.keyboard.press(key)
            await page.wait_for_timeout(300)
            return ToolResult(text=f"Pressed key: {key}")

        if name == "hover":
            selector = str(params.get("selector", ""))
            try:
                await page.hover(selector, timeout=5000)
                return ToolResult(text=f"Hovered over: {selector}")
            except Exception as e:
                return ToolResult(text=f"Failed to hover: {e}")

        if name == "scroll":
            direction = str(params.get("direction", "down"))
            amount = int(str(params.get("amount", 300)))
            delta = amount if direction == "down" else -amount
            await page.mouse.wheel(0, delta)
            await page.wait_for_timeout(300)
            return ToolResult(text=f"Scrolled {direction} by {amount}px")

        return ToolResult(text=f"Unknown browser tool: {name}")
