"""Facebook Marketplace source adapter for NADLANFIX.

Wraps facebook_feed.py to implement the unified BaseSourceAdapter interface.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sources.base.adapter import BaseSourceAdapter, SourceStatus
from sources.base.exceptions import SourceFetchError, SourcePersistError


class FacebookSourceAdapter(BaseSourceAdapter):
    """Facebook Marketplace source adapter implementing unified interface."""

    @property
    def source_name(self) -> str:
        return "facebook"

    @property
    def display_name(self) -> str:
        return "Facebook Marketplace"

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create Facebook tables if they don't exist."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS facebook_listings (
                listing_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                url TEXT NOT NULL,
                price_text TEXT,
                price_value REAL,
                currency TEXT,
                location TEXT,
                image_url TEXT,
                seller_name TEXT,
                description TEXT,
                category TEXT DEFAULT 'other',
                condition TEXT DEFAULT 'unknown',
                classification_source TEXT DEFAULT 'heuristic',
                classification_confidence REAL NOT NULL DEFAULT 0,
                restricted INTEGER NOT NULL DEFAULT 0,
                city_id TEXT,
                city_name TEXT,
                source_query TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1,
                delisted_at TEXT,
                raw_json TEXT,
                captured_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS facebook_price_history (
                listing_id TEXT NOT NULL,
                price_text TEXT,
                price_value REAL,
                currency TEXT,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (listing_id, observed_at)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS facebook_harvest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                pages_fetched INTEGER NOT NULL DEFAULT 0,
                pages_failed INTEGER NOT NULL DEFAULT 0,
                rows_seen INTEGER NOT NULL DEFAULT 0,
                rows_new INTEGER NOT NULL DEFAULT 0,
                target INTEGER,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS facebook_cities (
                city_name TEXT PRIMARY KEY,
                city_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_facebook_city ON facebook_listings (city_name);
            CREATE INDEX IF NOT EXISTS idx_facebook_price ON facebook_listings (price_value);
            CREATE INDEX IF NOT EXISTS idx_facebook_last_seen ON facebook_listings (last_seen_at);
        """)
        conn.commit()

    def fetch(self, conn: sqlite3.Connection, target: int, **kwargs) -> list[dict]:
        """Fetch listings from Facebook Marketplace."""
        from ingestion.feeds import facebook_feed
        return facebook_feed.harvest(target=target, **kwargs)

    def normalize(self, raw: dict) -> dict:
        """Normalize Facebook listing to canonical format."""
        return {
            "listing_id": raw.get("listing_id", ""),
            "source": "facebook",
            "external_id": raw.get("listing_id", ""),
            "title": raw.get("title", ""),
            "description": raw.get("description"),
            "normalized_title": raw.get("normalized_title", ""),
            "price": raw.get("price_value"),
            "price_text": raw.get("price_text"),
            "currency": raw.get("currency", "ILS"),
            "previous_price": None,
            "property_type": raw.get("category", "other"),
            "condition": raw.get("condition", "unknown"),
            "rooms": None,
            "area_sqm": None,
            "floor": None,
            "bathrooms": None,
            "parking": None,
            "city": raw.get("city_name", ""),
            "neighborhood": raw.get("location", ""),
            "street": "",
            "address_text": raw.get("location", ""),
            "lat": None,
            "lon": None,
            "gush": None,
            "helka": None,
            "image_url": raw.get("image_url"),
            "images": [raw.get("image_url")] if raw.get("image_url") else [],
            "url": raw.get("url"),
            "source_query": raw.get("source_query", ""),
            "category": raw.get("category", "other"),
            "classification_source": raw.get("classification_source", "heuristic"),
            "classification_confidence": raw.get("classification_confidence", 0.0),
            "restricted": bool(raw.get("restricted", False)),
            "deal_score": raw.get("deal_score", 0.0),
            "score_confidence": raw.get("score_confidence", 0.0),
            "score_reasons": raw.get("score_reasons", []),
            "seller_name": raw.get("seller_name"),
            "agency": None,
            "phone": None,
            "phone_source": None,
            "first_seen_at": raw.get("first_seen_at", ""),
            "last_seen_at": raw.get("last_seen_at", ""),
            "captured_at": raw.get("last_seen_at", ""),
            "is_active": True,
            "delisted_at": raw.get("delisted_at"),
            "seen_count": raw.get("seen_count", 1),
        }

    def count(self, conn: sqlite3.Connection) -> dict:
        """Return count and freshness info."""
        from ingestion.feeds import facebook_feed
        return facebook_feed.counts()

    def _persist_listing(self, conn: sqlite3.Connection, listing: dict) -> bool:
        """Persist a normalized listing. Returns True if inserted."""
        existing = conn.execute(
            "SELECT listing_id FROM facebook_listings WHERE listing_id = ?",
            (listing["listing_id"],)
        ).fetchone()

        now = self._now()

        if existing is None:
            conn.execute(
                """INSERT INTO facebook_listings
                   (listing_id, title, normalized_title, fingerprint, url, price_text,
                    price_value, currency, location, image_url, seller_name, description,
                    category, condition, classification_source, classification_confidence,
                    restricted, city_id, city_name, source_query, first_seen_at,
                    last_seen_at, seen_count, captured_at, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    listing["listing_id"], listing["title"], listing["normalized_title"],
                    "", listing["url"], listing["price_text"],
                    listing["price"], listing["currency"], listing["neighborhood"],
                    listing["image_url"], listing["seller_name"], listing["description"],
                    listing["category"], listing["condition"],
                    listing["classification_source"], listing["classification_confidence"],
                    listing["restricted"], "", listing["city"],
                    listing["source_query"], listing["first_seen_at"],
                    now, 1, now, 1,
                )
            )
            return True
        else:
            conn.execute(
                """UPDATE facebook_listings
                   SET last_seen_at = ?, seen_count = seen_count + 1,
                       price_value = ?, price_text = ?, is_active = 1
                   WHERE listing_id = ?""",
                (now, listing["price"], listing["price_text"], listing["listing_id"])
            )
            return False
