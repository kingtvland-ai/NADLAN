# tests/test_storage.py
import asyncio
import sqlite3
from pathlib import Path

from facebook_marketplace_scraper.models import MarketplaceListing, Watchlist
from facebook_marketplace_scraper.storage import LATEST_SCHEMA_VERSION, MarketplaceStore


def _listing(price: float) -> MarketplaceListing:
    return MarketplaceListing(
        listing_id="100",
        title="ThinkPad T480",
        normalized_title="thinkpad t480",
        fingerprint="f1",
        url="https://www.facebook.com/marketplace/item/100/",
        price_text=f"${price:g}",
        price_value=price,
        currency="CAD",
        source_query="thinkpad",
    )


async def test_store_deduplicates_and_only_records_price_changes(tmp_path: Path) -> None:
    store = MarketplaceStore(tmp_path / "market.sqlite3")
    await store.initialize()

    run1 = await store.start_search_run("thinkpad")
    assert await store.upsert_listing(_listing(300), run_id=run1) == (True, False)

    run2 = await store.start_search_run("thinkpad")
    assert await store.upsert_listing(_listing(300), run_id=run2) == (False, False)

    run3 = await store.start_search_run("thinkpad")
    assert await store.upsert_listing(_listing(250), run_id=run3) == (False, True)

    history = await store.listing_history("100")
    assert [row["price_value"] for row in history] == [250.0, 300.0]


async def test_watchlist_crud_and_status_metadata(tmp_path: Path) -> None:
    store = MarketplaceStore(tmp_path / "market.sqlite3")
    await store.initialize()
    watch_id = await store.create_watchlist(Watchlist(name="laptops", query="thinkpad"))

    updated = await store.update_watchlist(
        watch_id,
        {"max_price": 350.0, "enabled": False},
    )
    assert updated is not None
    assert updated.max_price == 350.0
    assert updated.enabled is False

    await store.mark_watchlist_run(watch_id, success=False, error="temporary failure")
    failed = await store.get_watchlist(watch_id)
    assert failed is not None
    assert failed.last_error == "temporary failure"
    assert failed.last_error_at is not None

    await store.mark_watchlist_run(watch_id, success=True)
    succeeded = await store.get_watchlist(watch_id)
    assert succeeded is not None
    assert succeeded.last_success_at is not None
    assert succeeded.last_error is None
    assert await store.delete_watchlist(watch_id)


async def test_parallel_writes_use_isolated_connections(tmp_path: Path) -> None:
    store = MarketplaceStore(tmp_path / "market.sqlite3")
    await store.initialize()
    run_id = await store.start_search_run("thinkpad")

    first = _listing(300)
    second = _listing(275).model_copy(
        update={
            "listing_id": "101",
            "fingerprint": "f2",
            "url": "https://www.facebook.com/marketplace/item/101/",
        }
    )
    results = await asyncio.gather(
        store.upsert_listing(first, run_id=run_id),
        store.upsert_listing(second, run_id=run_id),
    )
    assert results == [(True, False), (True, False)]


async def test_existing_v02_database_migrates_in_place(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE watchlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                query TEXT NOT NULL,
                min_price REAL,
                max_price REAL,
                target_price REAL,
                max_items INTEGER NOT NULL DEFAULT 50,
                default_currency TEXT NOT NULL DEFAULT 'CAD',
                interval_seconds INTEGER NOT NULL DEFAULT 1800,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO watchlists(name,query,created_at,updated_at)
            VALUES ('legacy','thinkpad','2026-08-08T00:00:00+00:00','2026-08-08T00:00:00+00:00');
            """
        )
        db.commit()

    store = MarketplaceStore(path)
    await store.initialize()

    assert await store.schema_version() == LATEST_SCHEMA_VERSION
    legacy = await store.list_watchlists()
    assert legacy[0].name == "legacy"
    assert legacy[0].last_success_at is None
    status = await store.daemon_status()
    assert status["state"] == "stopped"


async def test_daemon_health_state_tracks_heartbeat_and_errors(tmp_path: Path) -> None:
    store = MarketplaceStore(tmp_path / "market.sqlite3")
    await store.initialize()

    await store.daemon_started(1234)
    running = await store.daemon_status()
    assert running["effective_state"] == "running"
    assert running["pid"] == 1234

    await store.daemon_heartbeat(active_watchlist="laptops")
    active = await store.daemon_status()
    assert active["active_watchlist"] == "laptops"

    await store.daemon_cycle_completed(success=False, error="browser failed")
    failed = await store.daemon_status()
    assert failed["effective_state"] == "error"
    assert failed["last_error"] == "browser failed"

    await store.daemon_stopped()
    stopped = await store.daemon_status()
    assert stopped["effective_state"] == "stopped"


async def test_price_stats_use_fuzzy_title_comparables(tmp_path: Path) -> None:
    store = MarketplaceStore(tmp_path / "market.sqlite3")
    await store.initialize()
    run_id = await store.start_search_run("thinkpad")

    first = _listing(300).model_copy(
        update={
            "listing_id": "201",
            "title": "Lenovo ThinkPad T480 i5 16GB",
            "normalized_title": "lenovo thinkpad t480 i5 16gb",
            "fingerprint": "f201",
            "url": "https://www.facebook.com/marketplace/item/201/",
        }
    )
    second = _listing(280).model_copy(
        update={
            "listing_id": "202",
            "title": "ThinkPad T480 laptop",
            "normalized_title": "thinkpad t480 laptop",
            "fingerprint": "f202",
            "url": "https://www.facebook.com/marketplace/item/202/",
        }
    )
    different_model = _listing(200).model_copy(
        update={
            "listing_id": "203",
            "title": "ThinkPad T490 laptop",
            "normalized_title": "thinkpad t490 laptop",
            "fingerprint": "f203",
            "url": "https://www.facebook.com/marketplace/item/203/",
        }
    )
    for item in (first, second, different_model):
        await store.upsert_listing(item, run_id=run_id)

    stats = await store.price_stats(first)
    assert stats.sample_size == 1
    assert stats.median_price == 280.0
