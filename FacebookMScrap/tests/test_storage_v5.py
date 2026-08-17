# tests/test_storage_v5.py
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from facebook_marketplace_scraper.models import (
    MarketplaceListing,
    NotificationEvent,
    RetentionPolicy,
)
from facebook_marketplace_scraper.storage import LATEST_SCHEMA_VERSION, MarketplaceStore


def _listing(listing_id: str, title: str, price: float, *, category: str = "computers") -> MarketplaceListing:
    return MarketplaceListing(
        listing_id=listing_id,
        title=title,
        normalized_title=title.casefold(),
        fingerprint=f"f-{listing_id}",
        url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
        price_text=f"${price:g}",
        price_value=price,
        currency="CAD",
        category=category,
        condition="good",
        classification_confidence=0.8,
        source_query="test",
    )


async def test_schema_v3_persists_metadata_and_score_reasons(tmp_path: Path) -> None:
    store = MarketplaceStore(tmp_path / "market.sqlite3")
    await store.initialize()
    assert await store.schema_version() == LATEST_SCHEMA_VERSION == 3
    run = await store.start_search_run("thinkpad")
    listing = _listing("100", "ThinkPad T480", 250)
    await store.upsert_listing(listing, run_id=run)
    await store.update_score("100", 88.5, 0.75, ["below median", "good condition"])
    detail = await store.listing_detail("100")
    assert detail is not None
    assert detail["category"] == "computers"
    assert detail["condition"] == "good"
    assert detail["score_reasons"] == ["below median", "good condition"]


async def test_price_stats_are_category_aware(tmp_path: Path) -> None:
    store = MarketplaceStore(tmp_path / "market.sqlite3")
    await store.initialize()
    run = await store.start_search_run("test")
    target = _listing("100", "Lenovo ThinkPad T480 laptop", 200)
    peer = _listing("101", "ThinkPad T480 computer", 300)
    wrong_category = _listing("102", "ThinkPad T480 solar kit", 999, category="solar")
    for item in (target, peer, wrong_category):
        await store.upsert_listing(item, run_id=run)
    stats = await store.price_stats(target)
    assert stats.sample_size == 1
    assert stats.median_price == 300
    assert stats.category == "computers"


async def test_notification_dedupe_and_retention(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite3"
    store = MarketplaceStore(path)
    await store.initialize()
    run = await store.start_search_run("test")
    await store.upsert_listing(_listing("100", "ThinkPad T480", 250), run_id=run)
    event = NotificationEvent(
        listing_id="100",
        event_type="high_score",
        dedupe_key="100:none:high_score:250.00",
        score=80,
        payload={"title": "ThinkPad T480"},
    )
    assert await store.record_notification(event)
    assert not await store.record_notification(event)
    assert await store.notification_exists(event.dedupe_key)

    old = (datetime.now(UTC) - timedelta(days=500)).isoformat()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE notification_events SET created_at=?", (old,))
        db.execute("UPDATE listing_prices SET captured_at=?", (old,))
        db.commit()
    deleted = await store.prune(
        RetentionPolicy(price_history_days=30, notification_days=30, search_run_days=30, listing_days=999)
    )
    assert deleted["notifications"] == 1
    history = await store.listing_history("100")
    assert len(history) == 1
