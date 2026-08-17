# src/facebook_marketplace_scraper/notifications.py
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .models import NotificationEvent, ScoredListing, Watchlist

if TYPE_CHECKING:
    from .storage import MarketplaceStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationSettings:
    min_score: float = 75.0
    jsonl_path: Path | None = None
    webhook_url: str | None = None

    @classmethod
    def from_env(cls) -> NotificationSettings:
        path = os.getenv("FBMS_NOTIFY_JSONL")
        return cls(
            min_score=float(os.getenv("FBMS_NOTIFY_MIN_SCORE", "75")),
            jsonl_path=Path(path) if path else None,
            webhook_url=os.getenv("FBMS_NOTIFY_WEBHOOK_URL") or None,
        )


class NotificationManager:
    def __init__(
        self,
        store: MarketplaceStore,
        settings: NotificationSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._file_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def maybe_notify(self, item: ScoredListing, watchlist: Watchlist | None) -> bool:
        listing = item.listing
        if listing.restricted:
            return False
        target_hit = bool(
            watchlist
            and watchlist.target_price is not None
            and listing.price_value is not None
            and listing.price_value <= watchlist.target_price
        )
        if item.deal_score < self.settings.min_score and not target_hit:
            return False

        event_type = "target_price" if target_hit else "high_score"
        price_key = "none" if listing.price_value is None else f"{listing.price_value:.2f}"
        watch_id = watchlist.id if watchlist else None
        dedupe_key = f"{listing.listing_id}:{watch_id}:{event_type}:{price_key}"
        if await self.store.notification_exists(dedupe_key):
            return False

        payload: dict[str, object] = {
            "event_type": event_type,
            "listing_id": listing.listing_id,
            "title": listing.title,
            "price": listing.price_value,
            "price_text": listing.price_text,
            "currency": listing.currency,
            "category": listing.category,
            "condition": listing.condition,
            "score": item.deal_score,
            "confidence": item.confidence,
            "url": str(listing.url),
            "watchlist": watchlist.name if watchlist else None,
            "reasons": item.reasons,
        }
        await self._deliver(payload)
        event = NotificationEvent(
            listing_id=listing.listing_id,
            watchlist_id=watch_id,
            event_type=event_type,
            dedupe_key=dedupe_key,
            score=item.deal_score,
            payload=payload,
        )
        await self.store.record_notification(event)
        return True

    async def _deliver(self, payload: dict[str, object]) -> None:
        logger.info(
            "Marketplace notification: %s score=%s price=%s",
            payload["title"],
            payload["score"],
            payload["price_text"],
            extra={"listing_id": payload["listing_id"], "event": payload["event_type"]},
        )
        if self.settings.jsonl_path is not None:
            await self._append_jsonl(self.settings.jsonl_path, payload)
        if self.settings.webhook_url:
            response = await self._client.post(self.settings.webhook_url, json=payload)
            response.raise_for_status()

    async def _append_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._file_lock:
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(_append_text, path, line)


def _append_text(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
