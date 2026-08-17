# src/facebook_marketplace_scraper/fixtures.py
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from .browser import MarketplaceBrowser
from .extractor import CardRecord, MarketplaceDomExtractor

_ITEM_RE = re.compile(r"(/marketplace/item/)\d+")


def sanitize_fixture_records(records: list[CardRecord]) -> list[CardRecord]:
    """Keep real DOM card structure while removing unstable IDs and remote image URLs."""
    sanitized: list[CardRecord] = []
    for index, record in enumerate(records, start=1):
        item = dict(record)
        href = str(item.get("href") or "")
        href = href.split("?", 1)[0]
        href = _ITEM_RE.sub(rf"\g<1>{900000000000 + index}", href)
        item["href"] = href
        if item.get("image_url"):
            item["image_url"] = f"https://example.invalid/marketplace/{index}.jpg"
        sanitized.append(item)
    return sanitized


async def capture_search_fixture(
    *,
    query: str,
    city_id: str,
    output: Path,
    storage_state_path: Path | None,
    max_items: int = 30,
) -> Path:
    extractor = MarketplaceDomExtractor()
    async with MarketplaceBrowser(headless=False, storage_state_path=storage_state_path) as browser:
        page = await browser.open_search_page(query, city_id)
        try:
            records = await extractor.snapshot(page, max_items=max_items)
        finally:
            await page.close()
    payload = {
        "fixture_version": 1,
        "source": "facebook-marketplace-browser-snapshot",
        "query": query,
        "records": sanitize_fixture_records(records),
    }
    await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(output.write_text, json.dumps(payload, indent=2), "utf-8")
    return output


def load_fixture_records(path: Path) -> list[CardRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    if not isinstance(records, list):
        raise ValueError("fixture records must be a list")
    return [dict(item) for item in records if isinstance(item, dict)]
