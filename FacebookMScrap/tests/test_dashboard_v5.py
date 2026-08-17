# tests/test_dashboard_v5.py
from pathlib import Path

from fastapi.testclient import TestClient

from facebook_marketplace_scraper.dashboard import create_dashboard_app
from facebook_marketplace_scraper.models import MarketplaceListing
from facebook_marketplace_scraper.storage import MarketplaceStore


async def _seed(path: Path) -> None:
    store = MarketplaceStore(path)
    await store.initialize()
    run = await store.start_search_run("thinkpad")
    listing = MarketplaceListing(
        listing_id="100",
        title="ThinkPad T480",
        normalized_title="thinkpad t480",
        fingerprint="f",
        url="https://www.facebook.com/marketplace/item/100/",
        price_text="$250",
        price_value=250,
        currency="CAD",
        category="computers",
        condition="good",
        source_query="thinkpad",
    )
    await store.upsert_listing(listing, run_id=run)
    await store.update_score("100", 82, 0.7, ["test reason"])
    await store.finish_search_run(run, extracted=1, normalized=1, inserted=1, duration_ms=123.4)


def test_detail_notifications_and_run_endpoints(tmp_path: Path) -> None:
    import asyncio

    path = tmp_path / "market.sqlite3"
    asyncio.run(_seed(path))
    with TestClient(create_dashboard_app(path)) as client:
        detail = client.get("/api/listings/100")
        assert detail.status_code == 200
        assert detail.json()["category"] == "computers"
        assert detail.json()["score_reasons"] == ["test reason"]
        history = client.get("/api/listings/100/history")
        assert history.status_code == 200
        assert history.json()[0]["price_value"] == 250
        page = client.get("/listing/100")
        assert page.status_code == 200
        assert "Price history" in page.text
        runs = client.get("/api/runs")
        assert runs.status_code == 200
        assert runs.json()[0]["duration_ms"] == 123.4
        notifications = client.get("/api/notifications")
        assert notifications.status_code == 200
