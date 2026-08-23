"""Signing a browser session in, according to a recipe the pipeline declared.

This used to be a function containing one application's login page: `input[id*="login-username"]`,
a link reading "Sign in using local account", and a check for the words "Log in to". Point it at any
other application and it silently failed to authenticate, then reported every subsequent step as a
selector that did not match.

The algorithm was never the application-specific part — navigate, notice whether you are already in,
fill two fields, submit, verify — so the algorithm stays here and the strings move out to a file the
pipeline writes. A pipeline that declares no recipe gets no automatic login, which is the right
default: its scripts sign in through their own setup steps, in the open, where a reader can see it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from ..errors import ExecError
from ..logging import log

# Named so tests can drive the algorithm without waiting out a real page load.
RETRY_DELAY = 2.0
SETTLE_DELAY = 1.0


class Session(Protocol):
    """The part of a browser session logging in needs."""

    async def execute_tool(self, tool: str, params: dict[str, Any]) -> Any: ...


@dataclass
class LoginRecipe:
    """How one application's sign-in page works. Every field comes from the pipeline; none is
    guessed, because a guessed selector fails at the target rather than here."""

    username_selectors: list[str] = field(default_factory=list)
    password_selectors: list[str] = field(default_factory=list)
    submit_selectors: list[str] = field(default_factory=list)
    # Optional. Some sign-in pages ask which provider first; these are the link texts to try.
    provider_links: list[str] = field(default_factory=list)
    # Text that appears only when signed out. Absent from a loaded page means we are already in.
    signed_out_markers: list[str] = field(default_factory=list)
    # Text that means the session lapsed and the run should sign in again.
    expired_markers: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> LoginRecipe:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ExecError(f"{path} should be a mapping describing the sign-in page")
        recipe = cls(
            username_selectors=[str(s) for s in raw.get("username_selectors") or []],
            password_selectors=[str(s) for s in raw.get("password_selectors") or []],
            submit_selectors=[str(s) for s in raw.get("submit_selectors") or []],
            provider_links=[str(s) for s in raw.get("provider_links") or []],
            signed_out_markers=[str(s) for s in raw.get("signed_out_markers") or []],
            expired_markers=[str(s) for s in raw.get("expired_markers") or []],
        )
        missing = [
            name
            for name, value in (
                ("username_selectors", recipe.username_selectors),
                ("password_selectors", recipe.password_selectors),
                ("submit_selectors", recipe.submit_selectors),
            )
            if not value
        ]
        if missing:
            raise ExecError(f"{path} is missing {', '.join(missing)}")
        return recipe


async def _first_that_works(session: Session, tool: str, attempts: list[dict[str, Any]]) -> bool:
    """Selectors are listed most-specific first; take the first that does not report a failure."""
    for params in attempts:
        result = await session.execute_tool(tool, params)
        text = getattr(result, "text", "") or ""
        if "Failed" not in text and "no matching" not in text:
            return True
    return False


async def sign_in(
    session: Session, recipe: LoginRecipe, *, url: str, username: str, password: str
) -> bool:
    """Drive the declared sign-in page. Two attempts, because the first can land mid-redirect."""
    for attempt in range(2):
        try:
            if attempt > 0:
                await asyncio.sleep(RETRY_DELAY)

            await session.execute_tool("navigate", {"url": url})
            await session.execute_tool("wait_for", {"selector": "body", "timeout": 20000})
            await asyncio.sleep(SETTLE_DELAY)

            if recipe.signed_out_markers:
                snapshot = await session.execute_tool("get_page_snapshot", {})
                page = getattr(snapshot, "text", "") or ""
                if len(page.strip()) > 50 and not any(m in page for m in recipe.signed_out_markers):
                    log.debug("    Already signed in")
                    return True

            for text in recipe.provider_links:
                clicked = await _first_that_works(
                    session, "click_text", [{"text": text, "timeout": 3000}]
                )
                if clicked:
                    log.debug("    Chose a sign-in provider")
                    break

            filled_user = await _first_that_works(
                session, "fill", [{"selector": s, "value": username} for s in recipe.username_selectors]
            )
            filled_password = await _first_that_works(
                session, "fill", [{"selector": s, "value": password} for s in recipe.password_selectors]
            )
            if not (filled_user and filled_password):
                continue

            submitted = await _first_that_works(
                session, "click", [{"selector": s} for s in recipe.submit_selectors]
            )
            if not submitted:
                continue

            await session.execute_tool("wait_for", {"selector": "body", "timeout": 5000})
            if not recipe.signed_out_markers:
                return True
            verify = await session.execute_tool("get_page_snapshot", {})
            page = getattr(verify, "text", "") or ""
            if not any(marker in page for marker in recipe.signed_out_markers):
                log.debug("    Signed in")
                return True
        except Exception:  # noqa: BLE001 - a failed sign-in is a reported result, not a crash
            continue
    return False


def looks_expired(recipe: LoginRecipe, text: str) -> bool:
    """Whether a page or a tool result says the session lapsed."""
    return bool(recipe.expired_markers) and any(m in text for m in recipe.expired_markers)
