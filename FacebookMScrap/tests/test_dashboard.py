# tests/test_dashboard.py
from pathlib import Path

from fastapi.testclient import TestClient

from facebook_marketplace_scraper.dashboard import create_dashboard_app
from facebook_marketplace_scraper.storage import LATEST_SCHEMA_VERSION


def test_dashboard_health_and_watchlist_crud(tmp_path: Path) -> None:
    app = create_dashboard_app(tmp_path / "market.sqlite3")
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["schema_version"] == LATEST_SCHEMA_VERSION
        assert health.json()["daemon"]["effective_state"] == "stopped"

        created = client.post(
            "/api/watchlists",
            json={
                "name": "laptops",
                "query": "thinkpad",
                "target_price": 250,
                "interval_seconds": 600,
            },
        )
        assert created.status_code == 201
        watchlist_id = created.json()["id"]

        updated = client.patch(
            f"/api/watchlists/{watchlist_id}",
            json={"enabled": False, "max_price": 400},
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False
        assert updated.json()["max_price"] == 400

        invalid = client.post(
            "/api/watchlists",
            json={"name": "bad", "query": "x", "min_price": 500, "max_price": 100},
        )
        assert invalid.status_code == 422

        deleted = client.delete(f"/api/watchlists/{watchlist_id}")
        assert deleted.status_code == 204
        assert client.delete(f"/api/watchlists/{watchlist_id}").status_code == 404
