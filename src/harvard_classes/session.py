"""HarvardKey session shared by the scrapers that need a login.

The session is kept in .auth/state.json (Playwright storage state, which
includes HarvardKey's session-only cookies). When it is missing or expired, a
visible Chrome window opens for the user to sign in; everything else runs
headless.
"""

import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import BrowserContext
from playwright.async_api import Error as PWError

STATE = Path(".auth/state.json")
LOGIN_TIMEOUT_MS = 15 * 60 * 1000


class SessionExpired(Exception):
    pass


def log(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


async def launch(p, headless: bool):
    try:
        return await p.chromium.launch(channel="chrome", headless=headless)
    except PWError:
        return await p.chromium.launch(headless=headless)  # Playwright's bundled Chromium


async def save_state(ctx: BrowserContext) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    await ctx.storage_state(path=STATE)
    STATE.chmod(0o600)  # holds session cookies


async def new_context(p, headless: bool = True):
    browser = await launch(p, headless=headless)
    ctx = await browser.new_context(storage_state=STATE if STATE.exists() else None)
    return browser, ctx


async def sign_in(p, start_url: str, *, done_selector: str | None = None, done_url: re.Pattern | None = None) -> None:
    """Open a visible window at start_url for HarvardKey sign-in; save the session once
    done_selector appears (or the URL matches done_url)."""
    browser, ctx = await new_context(p, headless=False)
    page = await ctx.new_page()
    await page.goto(start_url)
    log("Sign in to HarvardKey in the Chrome window that just opened; waiting up to 15 minutes...")
    try:
        if done_selector:
            await page.wait_for_selector(done_selector, timeout=LOGIN_TIMEOUT_MS)
        else:
            await page.wait_for_url(done_url, timeout=LOGIN_TIMEOUT_MS)
    except PWError:
        await browser.close()
        sys.exit("No sign-in completed; run again to resume.")
    await save_state(ctx)
    await browser.close()
    log("Signed in; continuing headless.")


async def get_html(ctx: BrowserContext, url: str) -> str:
    """GET with the session's cookies (no rendering). Raises SessionExpired on a HarvardKey redirect."""
    for attempt in range(3):
        try:
            r = await ctx.request.get(url, timeout=60000)
            break
        except PWError:
            if attempt == 2:
                raise
            await asyncio.sleep(5 * (attempt + 1))
    if "login.harvard.edu" in r.url:
        raise SessionExpired()
    return await r.text()
