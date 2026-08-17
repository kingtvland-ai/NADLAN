# src/facebook_marketplace_scraper/extractor.py
from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .models import RawListing

logger = logging.getLogger(__name__)

CardRecord = dict[str, Any]


def records_to_raw_listings(records: list[CardRecord], *, max_items: int) -> list[RawListing]:
    """Convert browser snapshots or JSON fixtures through the same extraction contract."""
    found: dict[str, RawListing] = {}
    for record in records:
        href = str(record.get("href") or "")
        if "/marketplace/item/" not in href:
            continue
        key = href.split("?", 1)[0]
        aria_label = str(record.get("aria_label") or "").strip() or None
        image_alt = str(record.get("image_alt") or "").strip() or None
        found.setdefault(
            key,
            RawListing(
                url=key,
                text=str(record.get("text") or "").strip(),
                title_hint=aria_label or image_alt,
                image_url=str(record.get("image_url") or "").strip() or None,
            ),
        )
        if len(found) >= max_items:
            break
    return list(found.values())[:max_items]


class MarketplaceDomExtractor:
    """Extract listing candidates using item-link semantics instead of generated CSS classes."""

    ITEM_LINK = "a[href*='/marketplace/item/']"

    async def extract(self, page: Page, *, max_items: int) -> list[RawListing]:
        records = await self.snapshot(page, max_items=max_items)
        return records_to_raw_listings(records, max_items=max_items)

    async def snapshot(self, page: Page, *, max_items: int) -> list[CardRecord]:
        try:
            await page.locator(self.ITEM_LINK).first.wait_for(state="attached", timeout=12_000)
        except PlaywrightTimeoutError:
            return []

        await self._scroll_until_stable(page, max_items=max_items)
        links = page.locator(self.ITEM_LINK)
        limit = max_items * 3
        return await links.evaluate_all(
            """(nodes, limit) => nodes.slice(0, limit).map(anchor => {
                const image = anchor.querySelector('img');
                return {
                    href: anchor.getAttribute('href'),
                    text: anchor.innerText || '',
                    aria_label: anchor.getAttribute('aria-label'),
                    image_url: image ? image.getAttribute('src') : null,
                    image_alt: image ? image.getAttribute('alt') : null
                };
            })""",
            limit,
        )

    async def _scroll_until_stable(self, page: Page, *, max_items: int) -> None:
        stable_rounds = 0
        previous_count = 0
        for _ in range(12):
            current_count = await page.locator(self.ITEM_LINK).count()
            if current_count >= max_items:
                break
            if current_count == previous_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 3:
                break
            previous_count = current_count
            await page.mouse.wheel(0, 2400)
            await asyncio.sleep(0.45)


class ListingDetailExtractor:
    """Open a single Marketplace item page and extract description + seller name.

    Facebook does not expose the listing description on search result cards —
    it is only available on the individual item page. This extractor is called
    once per *newly inserted* listing so that the DB is enriched without
    paying the per-page cost on every run.

    Resilience strategy
    -------------------
    * Facebook's DOM structure changes without notice, so we try several
      selector strategies in order and take the first one that yields text.
    * Any failure (timeout, blocked page, selector miss) is caught and logged
      at WARNING level; the caller receives ``None`` values and continues
      normally — enrichment is best-effort and must never break a collection run.
    * If no browser session exists (storage_state_path is None or the file is
      missing) the caller should skip enrichment entirely; this class does not
      enforce that — it is the caller's responsibility.
    """

    # Ordered list of CSS / aria selectors tried for the description block.
    # Facebook renders description text inside a div that sits below the price
    # row; these selectors reflect observed DOM patterns as of 2024-2025.
    _DESCRIPTION_SELECTORS = [
        # Most reliable: the aria-label on the description container
        "[data-testid='marketplace_pdp_description']",
        # Fallback: a div whose direct text child contains at least 30 chars
        # (avoids matching price/location lines)
        "div[dir='auto'] > span[dir='auto']",
        # Last resort: any span with substantial text below the image
        "div[aria-label] span[dir='auto']",
    ]

    _SELLER_SELECTORS = [
        "a[href*='/marketplace/profile/']",
        "a[href*='marketplace/seller']",
        # Facebook sometimes renders seller as a link to their profile page
        "a[href*='/user/']",
    ]

    # How long to wait for the page body before giving up (ms)
    _PAGE_TIMEOUT = 20_000
    # How long to wait for a specific selector before trying the next one (ms)
    _SELECTOR_TIMEOUT = 4_000

    async def extract_detail(self, page: Page) -> dict[str, str | None]:
        """Return ``{"description": ..., "seller_name": ...}``, either or both may be None."""
        description = await self._extract_description(page)
        seller_name = await self._extract_seller(page)
        return {"description": description, "seller_name": seller_name}

    async def _extract_description(self, page: Page) -> str | None:
        # Strategy 1: known test-id selectors
        for selector in self._DESCRIPTION_SELECTORS[:1]:
            try:
                loc = page.locator(selector).first
                await loc.wait_for(state="attached", timeout=self._SELECTOR_TIMEOUT)
                text = (await loc.inner_text(timeout=self._SELECTOR_TIMEOUT)).strip()
                if len(text) >= 10:
                    return text
            except PlaywrightTimeoutError:
                pass
            except Exception as exc:
                logger.debug("description selector %r failed: %s", selector, exc)

        # Strategy 2: evaluate JS to find the longest text block below the image
        try:
            result: str = await page.evaluate(
                """() => {
                    // Collect all visible text spans with meaningful length
                    const spans = Array.from(document.querySelectorAll('div[dir="auto"] span[dir="auto"]'));
                    const candidates = spans
                        .map(el => (el.innerText || '').trim())
                        .filter(t => t.length >= 30);
                    if (!candidates.length) return '';
                    // Return the longest block as the most likely description
                    return candidates.reduce((a, b) => a.length >= b.length ? a : b, '');
                }"""
            )
            if result and len(result) >= 30:
                return result
        except Exception as exc:
            logger.debug("JS description extraction failed: %s", exc)

        # Strategy 3: remaining css selectors
        for selector in self._DESCRIPTION_SELECTORS[1:]:
            try:
                loc = page.locator(selector).first
                await loc.wait_for(state="attached", timeout=self._SELECTOR_TIMEOUT)
                text = (await loc.inner_text(timeout=self._SELECTOR_TIMEOUT)).strip()
                if len(text) >= 10:
                    return text
            except PlaywrightTimeoutError:
                pass
            except Exception as exc:
                logger.debug("description selector %r failed: %s", selector, exc)

        return None

    async def _extract_seller(self, page: Page) -> str | None:
        for selector in self._SELLER_SELECTORS:
            try:
                loc = page.locator(selector).first
                await loc.wait_for(state="attached", timeout=self._SELECTOR_TIMEOUT)
                text = (await loc.inner_text(timeout=self._SELECTOR_TIMEOUT)).strip()
                if text:
                    return text
            except PlaywrightTimeoutError:
                pass
            except Exception as exc:
                logger.debug("seller selector %r failed: %s", selector, exc)
        return None
