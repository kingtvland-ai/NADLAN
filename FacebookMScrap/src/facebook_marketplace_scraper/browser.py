# src/facebook_marketplace_scraper/browser.py
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import quote_plus

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)


def _fmt_price(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


class MarketplaceBrowser:
    """Async browser lifecycle wrapper with optional user-created session state."""

    def __init__(
        self,
        *,
        headless: bool = True,
        storage_state_path: Path | None = None,
    ) -> None:
        self._headless = headless
        self._storage_state_path = storage_state_path
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> MarketplaceBrowser:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        context_options: dict[str, object] = {
            "locale": "he-IL",
            "viewport": {"width": 1440, "height": 1000},
        }
        state_exists = bool(
            self._storage_state_path
            and await asyncio.to_thread(self._storage_state_path.exists)
        )
        if state_exists:
            context_options["storage_state"] = str(self._storage_state_path)
        self._context = await self._browser.new_context(**context_options)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

<<<<<<< HEAD
    @property
    def has_session(self) -> bool:
        """True if a storage-state file was loaded (i.e. the browser is logged in)."""
        return bool(
            self._storage_state_path and self._storage_state_path.exists()
        )

    async def open_search_page(
        self,
        query: str,
        city_id: str,
        min_price: float | None = None,
        max_price: float | None = None,
        category_id: str | None = None,
    ) -> Page:
        if self._context is None:
            raise RuntimeError("MarketplaceBrowser must be used as an async context manager")
        page = await self._context.new_page()
        params: list[str] = []
        if min_price is not None:
            params.append(f"minPrice={_fmt_price(min_price)}")
        if max_price is not None:
            params.append(f"maxPrice={_fmt_price(max_price)}")
        params.append(f"query={quote_plus(query)}")
        if category_id:
            params.append(f"category_id={quote_plus(category_id)}")
        params.append("exact=false")
        url = f"https://www.facebook.com/marketplace/{city_id}/search?" + "&".join(params)
=======
    async def open_search_page(self, query: str, city_id: str, radius_km: float) -> Page:
        if self._context is None:
            raise RuntimeError("MarketplaceBrowser must be used as an async context manager")
        page = await self._context.new_page()
        url = (
            f"https://www.facebook.com/marketplace/{city_id}/search/"
            f"?query={quote_plus(query)}&radius={radius_km}&exact=false"
        )
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        return page

    async def open_marketplace(self) -> Page:
        if self._context is None:
            raise RuntimeError("MarketplaceBrowser must be used as an async context manager")
        page = await self._context.new_page()
        await page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
        return page

    async def open_listing_page(self, url: str) -> Page:
        """Open a single Marketplace item page for detail enrichment.

        The page is returned open — the caller is responsible for closing it.
        Navigates with ``networkidle`` wait so React has time to render the
        description block; falls back to ``domcontentloaded`` on timeout.

        This method requires an active browser session (logged-in state).
        If no session was loaded the page will still open but Facebook may
        show a login wall instead of the listing, resulting in no data being
        extracted — the caller should check ``self.has_session`` before
        calling this method and skip enrichment when it returns False.
        """
        if self._context is None:
            raise RuntimeError("MarketplaceBrowser must be used as an async context manager")
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=25_000)
        except PlaywrightTimeoutError:
            # networkidle can time out on heavy pages; domcontentloaded is enough
            # for the description text to be present in the DOM.
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            except PlaywrightTimeoutError:
                logger.warning("listing page timed out: %s", url)
                await page.close()
                raise
        # Give React a moment to hydrate the description section
        await page.wait_for_timeout(1_500)
        return page

    async def search_location_candidates(
        self,
        city_name: str,
        *,
        max_candidates: int = 6,
    ) -> list[dict[str, str]]:
        """Use Marketplace's own location picker to discover the real numeric city_id
        behind a Hebrew city name. This id is not published anywhere else, so this is
        the only reliable source (see israel_cities.py docstring). Best-effort: Facebook's
        DOM/labels can change, so failures are reported to the caller rather than raised,
        letting the discovery run keep the previously known (static or DB) id.
        """
        if self._context is None:
            raise RuntimeError("MarketplaceBrowser must be used as an async context manager")
        page = await self._context.new_page()
        candidates: list[dict[str, str]] = []
        try:
            await page.goto(
                "https://www.facebook.com/marketplace/", wait_until="domcontentloaded", timeout=30_000
            )
            await page.wait_for_timeout(4000)  # תן ל-React להציג את סרגל הכלים לפני שמנסים ללחוץ
            location_button = page.get_by_role("button", name=re.compile("location|מיקום", re.I)).first
            search_box = page.get_by_role("combobox").first
            for attempt in range(max_candidates):
                try:
                    await location_button.click(timeout=8_000)
                except PlaywrightTimeoutError:
                    logger.warning(
                        "location button not found for %r (url=%s, title=%s)",
                        city_name, page.url, await page.title(),
                    )
                    break
                try:
                    await search_box.fill(city_name, timeout=8_000)
                except PlaywrightTimeoutError:
                    logger.warning("search combobox not found for %r after opening location picker", city_name)
                    break
                await page.wait_for_timeout(900)  # let Facebook's typeahead settle
                options = page.get_by_role("option")
                count = await options.count()
                if count <= attempt:
                    logger.warning(
                        "no autocomplete options for %r after fill (attempt=%d, count=%d)",
                        city_name, attempt, count,
                    )
                    break
                option = options.nth(attempt)
                try:
                    label = (await option.inner_text(timeout=4_000)).strip()
                    await option.click(timeout=6_000)
                except PlaywrightTimeoutError:
                    logger.warning("could not read/click option %d for %r", attempt, city_name)
                    break
                await page.wait_for_timeout(700)
                match = re.search(r"/marketplace/(\d+)", page.url)
                if match:
                    candidates.append({"label": label, "city_id": match.group(1), "url": page.url})
                else:
                    logger.warning("clicked option for %r but url has no city id: %s", city_name, page.url)
        finally:
            await page.close()
        return candidates

    async def save_storage_state(self, path: Path | None = None) -> Path:
        if self._context is None:
            raise RuntimeError("MarketplaceBrowser must be used as an async context manager")
        target = path or self._storage_state_path
        if target is None:
            raise ValueError("storage state path is required")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await self._context.storage_state(path=str(target))
        return target
